import asyncio
import base64
import errno
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jwt
import websockets
from flask import Flask, abort, g, jsonify, request, Response, send_file
from flask_compress import Compress

# ---------------------------------------------------------------------------
# Root directory + deployment mode
# ---------------------------------------------------------------------------

FEEDLING_DIR = Path(os.environ.get("FEEDLING_DATA_DIR", str(Path.home() / "feedling-data"))).expanduser()
FEEDLING_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Users registry (multi-tenant). Every request is auth'd by api_key.
# ---------------------------------------------------------------------------

USERS_FILE = FEEDLING_DIR / "users.json"
_users_lock = threading.Lock()
_users: list[dict] = []                    # [{user_id, api_key_hash, public_key, created_at}]
_key_to_user: dict[str, str] = {}          # api_key_hash → user_id (in-memory cache)

# API keys are 32 random bytes (high-entropy), so a fast collision-resistant
# hash is sufficient — bcrypt is designed for low-entropy passwords. Using
# SHA-256 over a per-server pepper keeps the hash table safe even if the file
# leaks, while avoiding per-request bcrypt cost (which would be dramatic given
# long-poll + screen-analyze are hit every few seconds).
def _server_pepper() -> bytes:
    """Stable secret for key hashing. Persisted under FEEDLING_DIR."""
    pepper_file = FEEDLING_DIR / ".pepper"
    if pepper_file.exists():
        try:
            return pepper_file.read_bytes()
        except Exception:
            pass
    pepper = secrets.token_bytes(32)
    try:
        pepper_file.write_bytes(pepper)
        os.chmod(pepper_file, 0o600)
    except Exception as e:
        print(f"[users] could not persist pepper: {e}")
    return pepper


_PEPPER = _server_pepper()


def _hash_api_key(api_key: str) -> str:
    return hmac.new(_PEPPER, api_key.encode("utf-8"), hashlib.sha256).hexdigest()


def _load_users():
    global _users, _key_to_user
    try:
        if USERS_FILE.exists():
            data = json.loads(USERS_FILE.read_text())
            _users = data if isinstance(data, list) else []
    except Exception as e:
        print(f"[users] failed to load: {e}")
        _users = []
    _key_to_user = {u["api_key_hash"]: u["user_id"] for u in _users if "api_key_hash" in u}
    print(f"[users] loaded {len(_users)} user(s)")


def _save_users():
    try:
        USERS_FILE.write_text(json.dumps(_users, indent=2))
        os.chmod(USERS_FILE, 0o600)
    except Exception as e:
        print(f"[users] failed to save: {e}")


def _resolve_user(api_key: str) -> str | None:
    if not api_key:
        return None
    h = _hash_api_key(api_key)
    uid = _key_to_user.get(h)
    if uid:
        return uid
    with _users_lock:
        for u in _users:
            if u.get("api_key_hash") == h:
                _key_to_user[h] = u["user_id"]
                return u["user_id"]
    return None


_USER_ID_RE = re.compile(r"^usr_[a-f0-9]{16}$")


def _register_user(public_key: str | None = None,
                   archive_language: str | None = None) -> dict:
    user_id = f"usr_{secrets.token_hex(8)}"
    api_key = secrets.token_hex(32)
    entry = {
        "user_id": user_id,
        "api_key_hash": _hash_api_key(api_key),
        "public_key": (public_key or "").strip(),
        "created_at": datetime.now().isoformat(),
    }
    # archive_language: the BCP-47-ish locale code the iOS app picked up
    # from Locale.preferredLanguages on the registering device (e.g. "en",
    # "zh-Hans", "ja"). Drives the second defense layer against agent
    # archive-language drift — see /v1/users/preferences for migration
    # path and the skill's "Lock the Memory Garden language" rule for
    # how the agent consumes it.
    if archive_language:
        entry["archive_language"] = archive_language.strip()
    with _users_lock:
        _users.append(entry)
        _save_users()
        _key_to_user[entry["api_key_hash"]] = user_id
    print(f"[users] registered {user_id} archive_language={entry.get('archive_language', 'unset')}")
    return {"user_id": user_id, "api_key": api_key}


def _get_user_archive_language(user_id: str) -> str | None:
    """Return the user's stored archive_language, or None if unset.
    Caller is the source of truth for fallback behavior; this is a thin
    read helper used by /v1/bootstrap, /v1/memory/verify, /v1/users/whoami.
    """
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                val = u.get("archive_language")
                return val if val else None
    return None


_load_users()

# ---------------------------------------------------------------------------
# Per-user state store
# ---------------------------------------------------------------------------

MAX_FRAMES = 200
# Chat history ring buffer per user. Bumped from 500 → 5000 on 2026-05-11
# to give users meaningful scroll-back across months of normal use without
# silently losing their oldest conversations. At ~800 bytes per text-only
# envelope this caps chat.json around 4 MB; image-heavy users will see it
# grow into the tens of MB because envelopes carry the encrypted JPEG
# inline. Each chat append rewrites the whole file (see _persist_chat),
# so the bigger the file, the slower the write — at 5000 the per-message
# write cost is roughly 100-500 ms depending on image density. If that
# starts mattering, the next step is switching chat persistence to an
# append-only JSONL log so writes become O(1) regardless of history depth.
MAX_CHAT_MESSAGES = 5000
PUSH_COOLDOWN_SECONDS = int(os.environ.get("FEEDLING_PUSH_COOLDOWN_SEC", 300))
LIVE_ACTIVITY_DEDUPE_SEC = int(os.environ.get("FEEDLING_LIVE_ACTIVITY_DEDUPE_SEC", 900))
LIVE_ACTIVITY_START_COOLDOWN_SEC = int(os.environ.get("FEEDLING_LIVE_ACTIVITY_START_COOLDOWN_SEC", 1800))
DEVICE_EVENT_RETENTION_DAYS = int(os.environ.get("FEEDLING_DEVICE_EVENT_RETENTION_DAYS", 30))
PROACTIVE_JOB_SOURCE = "agent_initiated_proactive"
PROACTIVE_JOB_MAX = int(os.environ.get("FEEDLING_PROACTIVE_JOB_MAX", 500))
PROACTIVE_DEBUG_DECISION_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_DECISION_READ_MAX", 1000))
PROACTIVE_DEBUG_JOB_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_JOB_READ_MAX", PROACTIVE_JOB_MAX))
PROACTIVE_DEBUG_EVENT_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_EVENT_READ_MAX", 500))
PROACTIVE_DEBUG_REVIEW_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_REVIEW_READ_MAX", 500))
PROACTIVE_DEBUG_MESSAGE_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_MESSAGE_READ_MAX", 500))
PROACTIVE_DEBUG_FRAME_READ_MAX = int(os.environ.get("FEEDLING_PROACTIVE_DEBUG_FRAME_READ_MAX", MAX_FRAMES))
PROACTIVE_GATE_PROVIDER = os.environ.get("FEEDLING_PROACTIVE_GATE_PROVIDER", "openrouter").strip().lower()
PROACTIVE_GATE_MODEL = os.environ.get("FEEDLING_PROACTIVE_GATE_MODEL", "google/gemini-3.1-flash-lite").strip()
PROACTIVE_GATE_TIMEOUT_SEC = float(os.environ.get("FEEDLING_PROACTIVE_GATE_TIMEOUT_SEC", "30"))
PROACTIVE_GATE_MAX_FRAMES = int(os.environ.get("FEEDLING_PROACTIVE_GATE_MAX_FRAMES", "5"))
PROACTIVE_GATE_FRAME_CANDIDATE_MAX = int(os.environ.get("FEEDLING_PROACTIVE_GATE_FRAME_CANDIDATE_MAX", "60"))
PROACTIVE_GATE_SCENE_HASH_THRESHOLD = int(os.environ.get("FEEDLING_PROACTIVE_GATE_SCENE_HASH_THRESHOLD", "5"))
PROACTIVE_GATE_INCLUDE_IMAGES = os.environ.get("FEEDLING_PROACTIVE_GATE_INCLUDE_IMAGES", "true").lower() not in {
    "0", "false", "no", "off",
}
PROACTIVE_DEFAULT_TIMEZONE = os.environ.get("FEEDLING_DEFAULT_TIMEZONE", "Asia/Shanghai").strip() or "UTC"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
PROACTIVE_DEBUG_TRANSLATION_MODEL = os.environ.get(
    "FEEDLING_PROACTIVE_DEBUG_TRANSLATION_MODEL",
    PROACTIVE_GATE_MODEL,
).strip()
PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC = float(
    os.environ.get("FEEDLING_PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC", "10")
)
_debug_translation_cache: dict[str, str] = {}
_debug_translation_lock = threading.Lock()


# Used from inside UserStore._load_tokens on boot; must be defined before
# the class that calls it. Other token helpers (_select_token,
# _update_token_lifecycle, etc.) stay below since they only run at request
# time, after the full module has loaded.
def _normalize_token_entry(entry: dict) -> dict:
    normalized = dict(entry)
    normalized.setdefault("status", "active")
    normalized.setdefault("last_error", "")
    normalized.setdefault("last_success_at", "")
    normalized.setdefault("expired_at", "")
    normalized.setdefault("apns_env", normalized.get("environment", ""))
    normalized.setdefault("updated_at", normalized.get("registered_at", datetime.now().isoformat()))
    return normalized


class UserStore:
    """All per-user state + file paths + locks. One instance per user_id."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.dir = FEEDLING_DIR / user_id
        self.dir.mkdir(parents=True, exist_ok=True)

        self.frames_dir = self.dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)

        # frames
        self.frames_meta: list[dict] = []
        self.frames_lock = threading.Lock()

        # chat
        self.chat_messages: list[dict] = []
        self.chat_lock = threading.Lock()
        self.chat_waiters: list[threading.Event] = []
        self.chat_waiters_lock = threading.Lock()

        # tokens
        self.tokens: list[dict] = []

        # push cooldown
        self.last_push_epoch: float = 0.0
        self.last_push_mono: float = 0.0
        self.push_lock = threading.Lock()

        # live activity dedupe
        self.live_activity_state = {
            "last_message": "",
            "last_top_app": "",
            "last_sent_epoch": 0.0,
            "last_start_epoch": 0.0,
        }
        self.live_activity_state_lock = threading.Lock()

        # identity / memory locks
        self.identity_lock = threading.Lock()
        self.memory_lock = threading.Lock()
        self.consumer_state_lock = threading.Lock()

        # proactive presence state
        self.proactive_lock = threading.Lock()
        self.proactive_job_waiters: list[threading.Event] = []
        self.proactive_job_waiters_lock = threading.Lock()

        # load persistent state
        self._load_tokens()
        self._load_push_state()
        self._load_live_activity_state()
        self._load_chat()
        self._load_frames_meta()

    # ------- file paths -------
    @property
    def push_state_file(self) -> Path:
        return self.dir / "push_state.json"

    @property
    def live_activity_state_file(self) -> Path:
        return self.dir / "live_activity_state.json"

    @property
    def tokens_file(self) -> Path:
        return self.dir / "tokens.json"

    @property
    def chat_file(self) -> Path:
        return self.dir / "chat.json"

    @property
    def identity_file(self) -> Path:
        return self.dir / "identity.json"

    @property
    def memory_file(self) -> Path:
        return self.dir / "memory.json"

    @property
    def bootstrap_file(self) -> Path:
        return self.dir / "bootstrap.json"

    @property
    def bootstrap_events_file(self) -> Path:
        return self.dir / "bootstrap_events.jsonl"

    @property
    def consumer_state_file(self) -> Path:
        return self.dir / "consumer_state.json"

    @property
    def identity_changes_file(self) -> Path:
        """Append-only audit log of identity changes (init / replace / nudge).
        Surfaced to iOS as the "最近的变化" feed and as local push triggers.
        See /v1/identity/changes endpoint."""
        return self.dir / "identity_changes.jsonl"

    @property
    def frames_meta_file(self) -> Path:
        return self.dir / "frames_meta.json"

    @property
    def proactive_settings_file(self) -> Path:
        return self.dir / "proactive_settings.json"

    @property
    def device_events_file(self) -> Path:
        return self.dir / "device_events.jsonl"

    @property
    def gate_decisions_file(self) -> Path:
        return self.dir / "gate_decisions.jsonl"

    @property
    def proactive_jobs_file(self) -> Path:
        return self.dir / "proactive_jobs.jsonl"

    @property
    def gate_reviews_file(self) -> Path:
        return self.dir / "gate_reviews.jsonl"

    # ------- frames index -------
    def _load_frames_meta(self):
        # Fast path: index file already persisted.
        if self.frames_meta_file.exists():
            try:
                data = json.loads(self.frames_meta_file.read_text())
                if isinstance(data, list):
                    self.frames_meta = data
                    print(f"[{self.user_id}/frames] loaded index n={len(self.frames_meta)}")
                    return
            except Exception as e:
                print(f"[{self.user_id}/frames] index load failed: {e} — rebuilding from disk")

        # Rebuild path: no index yet (first boot with this fix, or pre-fix restart
        # left orphan env.json files). Scan frames_dir and reconstruct meta from
        # envelope bodies + file mtime. ts fallback loses sub-second precision but
        # is good enough to un-orphan pre-fix frames.
        recovered: list[dict] = []
        try:
            for p in sorted(self.frames_dir.glob("*.env.json")):
                try:
                    env = json.loads(p.read_text())
                    if not isinstance(env, dict) or not env.get("body_ct"):
                        continue
                    recovered.append({
                        "filename": p.name,
                        "ts": p.stat().st_mtime,
                        "app": None,
                        "ocr_text": "",
                        "w": 0,
                        "h": 0,
                        "encrypted": True,
                        "id": env.get("id") or p.stem.split(".")[0],
                        "v": env.get("v", 1),
                        "owner_user_id": env.get("owner_user_id"),
                    })
                except Exception as e:
                    print(f"[{self.user_id}/frames] skip {p.name}: {e}")
            recovered.sort(key=lambda m: m["ts"])
            if len(recovered) > MAX_FRAMES:
                # Keep the newest MAX_FRAMES; prune orphan files for the rest.
                drop = recovered[:-MAX_FRAMES]
                recovered = recovered[-MAX_FRAMES:]
                for m in drop:
                    try:
                        (self.frames_dir / m["filename"]).unlink(missing_ok=True)
                    except Exception:
                        pass
            self.frames_meta = recovered
            self._persist_frames_meta()
            print(f"[{self.user_id}/frames] rebuilt index from disk n={len(recovered)}")
        except Exception as e:
            print(f"[{self.user_id}/frames] rebuild failed: {e}")
            self.frames_meta = []

    def _persist_frames_meta(self):
        try:
            tmp = self.frames_meta_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.frames_meta))
            tmp.replace(self.frames_meta_file)
        except Exception as e:
            print(f"[{self.user_id}/frames] index save failed: {e}")

    # ------- tokens -------
    def _load_tokens(self):
        try:
            if self.tokens_file.exists():
                data = json.loads(self.tokens_file.read_text())
                self.tokens = data if isinstance(data, list) else []
        except Exception as e:
            print(f"[{self.user_id}/tokens] load failed: {e}")
            self.tokens = []
        self.tokens[:] = [_normalize_token_entry(t) for t in self.tokens]
        self._save_tokens()

    def _save_tokens(self):
        try:
            self.tokens_file.write_text(json.dumps(self.tokens))
        except Exception as e:
            print(f"[{self.user_id}/tokens] save failed: {e}")

    # ------- push cooldown -------
    def _load_push_state(self):
        try:
            if self.push_state_file.exists():
                data = json.loads(self.push_state_file.read_text())
                epoch = float(data.get("last_push_epoch", 0.0))
                elapsed = time.time() - epoch
                if 0 <= elapsed < PUSH_COOLDOWN_SECONDS:
                    self.last_push_epoch = epoch
                    self.last_push_mono = time.monotonic() - elapsed
        except Exception as e:
            print(f"[{self.user_id}/push_state] load failed: {e}")

    def record_successful_push(self):
        with self.push_lock:
            self.last_push_epoch = time.time()
            self.last_push_mono = time.monotonic()
        try:
            self.push_state_file.write_text(json.dumps({"last_push_epoch": self.last_push_epoch}))
        except Exception as e:
            print(f"[{self.user_id}/push_state] save failed: {e}")

    def cooldown_remaining_seconds(self) -> float:
        with self.push_lock:
            elapsed = time.monotonic() - self.last_push_mono
        return max(0.0, PUSH_COOLDOWN_SECONDS - elapsed)

    # ------- live activity dedupe -------
    def _load_live_activity_state(self):
        try:
            if self.live_activity_state_file.exists():
                data = json.loads(self.live_activity_state_file.read_text())
                if isinstance(data, dict):
                    self.live_activity_state = {
                        "last_message": str(data.get("last_message", "")),
                        "last_top_app": str(data.get("last_top_app", "")),
                        "last_sent_epoch": float(data.get("last_sent_epoch", 0.0)),
                        "last_start_epoch": float(data.get("last_start_epoch", 0.0)),
                    }
        except Exception as e:
            print(f"[{self.user_id}/live-activity] load failed: {e}")

    def _save_live_activity_state(self):
        try:
            self.live_activity_state_file.write_text(json.dumps(self.live_activity_state))
        except Exception as e:
            print(f"[{self.user_id}/live-activity] save failed: {e}")

    def should_suppress_live_activity(self, message: str, top_app: str) -> tuple[bool, str]:
        normalized_message = " ".join((message or "").strip().split())
        normalized_app = (top_app or "").strip().lower()
        if not normalized_message:
            return True, "empty_message"

        with self.live_activity_state_lock:
            last_message = " ".join((self.live_activity_state.get("last_message") or "").strip().split())
            last_app = (self.live_activity_state.get("last_top_app") or "").strip().lower()
            last_sent = float(self.live_activity_state.get("last_sent_epoch", 0.0))

        elapsed = max(0.0, time.time() - last_sent)

        if normalized_message == last_message and elapsed < 1800:
            return True, f"duplicate_message_within_30m:{int(1800 - elapsed)}s"

        if (
            normalized_message == last_message
            and normalized_app == last_app
            and elapsed < LIVE_ACTIVITY_DEDUPE_SEC
        ):
            return True, f"same_app_duplicate:{int(LIVE_ACTIVITY_DEDUPE_SEC - elapsed)}s"

        return False, "ok"

    def record_live_activity_sent(self, message: str, top_app: str):
        with self.live_activity_state_lock:
            self.live_activity_state["last_message"] = " ".join((message or "").strip().split())
            self.live_activity_state["last_top_app"] = (top_app or "").strip().lower()
            self.live_activity_state["last_sent_epoch"] = time.time()
        self._save_live_activity_state()

    def live_activity_start_cooldown_remaining_seconds(self) -> float:
        with self.live_activity_state_lock:
            last_start = float(self.live_activity_state.get("last_start_epoch", 0.0))
        if last_start <= 0:
            return 0.0
        elapsed = max(0.0, time.time() - last_start)
        return max(0.0, LIVE_ACTIVITY_START_COOLDOWN_SEC - elapsed)

    def should_start_live_activity(self) -> tuple[bool, str]:
        remaining = self.live_activity_start_cooldown_remaining_seconds()
        if remaining <= 0:
            return True, "start_window_open"
        return False, f"start_cooldown:{int(remaining)}s"

    def record_live_activity_started(self, message: str, top_app: str):
        with self.live_activity_state_lock:
            self.live_activity_state["last_start_epoch"] = time.time()
        self.record_live_activity_sent(message=message, top_app=top_app)

    # ------- chat -------
    def _load_chat(self):
        try:
            if self.chat_file.exists():
                data = json.loads(self.chat_file.read_text())
                self.chat_messages = data if isinstance(data, list) else []
        except Exception as e:
            print(f"[{self.user_id}/chat] load failed: {e}")
            self.chat_messages = []

    def _persist_chat(self):
        try:
            self.chat_file.write_text(json.dumps(self.chat_messages))
        except Exception as e:
            print(f"[{self.user_id}/chat] save failed: {e}")

    def append_chat(
        self,
        role: str,
        source: str,
        envelope: dict,
        content_type: str = "text",
        extra: dict | None = None,
    ) -> dict:
        """Append a v1 ciphertext chat message. `envelope` holds the AEAD
        payload. See docs/DESIGN_E2E.md §3.2 for field definitions. Server
        never decrypts — the envelope is stored verbatim.

        The client supplies the envelope's `id`, which becomes the stored
        message id so the AEAD additional-data the client baked in
        (owner||v||id) stays verifiable by the enclave on read-back.

        `content_type` is plaintext metadata: "text" (default) or "image".
        Used by clients/enclave to render the decrypted bytes correctly —
        the envelope itself only carries opaque bytes; the type tag tells
        the renderer to show a string vs decode JPEG.
        """
        msg_id = envelope.get("id") if isinstance(envelope.get("id"), str) and envelope["id"] else uuid.uuid4().hex
        ct = content_type if content_type in ("text", "image") else "text"

        msg: dict = {
            "id": msg_id,
            "role": role,
            "ts": time.time(),
            "source": source,
            "v": envelope.get("v", 1),
            "body_ct": envelope["body_ct"],
            "nonce": envelope["nonce"],
            "K_user": envelope["K_user"],
            "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
            "visibility": envelope.get("visibility", "shared"),
            "owner_user_id": envelope.get("owner_user_id", self.user_id),
            "content_type": ct,
        }
        # Synthetic verify pings are not real user content and are removed
        # after /v1/chat/verify_loop completes. They still need plaintext
        # while resident consumers are polling, because local_only synthetic
        # envelopes intentionally do not carry K_enclave and therefore cannot
        # be decrypted through the normal enclave/MCP history path.
        if source == "verify_ping" and envelope.get("synthetic_marker"):
            msg["content"] = envelope["synthetic_marker"]
        if envelope.get("K_enclave") is not None:
            msg["K_enclave"] = envelope["K_enclave"]
        if extra:
            for key in (
                "gate_decision_id",
                "proactive_job_id",
                "alert_preview",
                "push_body_preview",
                "push_live_activity_requested",
                "live_activity_status",
                "live_activity_reason",
                "live_activity_activity_id",
                "live_activity_mode",
                "alert_status",
                "alert_reason",
            ):
                value = extra.get(key)
                if isinstance(value, str) and value.strip():
                    msg[key] = value.strip()
                elif isinstance(value, bool):
                    msg[key] = value

        with self.chat_lock:
            self.chat_messages.append(msg)
            if len(self.chat_messages) > MAX_CHAT_MESSAGES:
                self.chat_messages[:] = self.chat_messages[-MAX_CHAT_MESSAGES:]
            self._persist_chat()
        return msg

    def update_chat_message_metadata(self, msg_id: str, fields: dict) -> dict | None:
        allowed = {
            "live_activity_status",
            "live_activity_reason",
            "live_activity_activity_id",
            "live_activity_mode",
            "alert_status",
            "alert_reason",
        }
        clean: dict = {}
        for key, value in (fields or {}).items():
            if key not in allowed:
                continue
            if value is None:
                continue
            clean[key] = str(value)[:500]
        if not clean:
            return None
        with self.chat_lock:
            for msg in self.chat_messages:
                if msg.get("id") == msg_id:
                    msg.update(clean)
                    self._persist_chat()
                    return msg
        return None

    def notify_chat_waiters(self):
        with self.chat_waiters_lock:
            for ev in self.chat_waiters:
                ev.set()
            self.chat_waiters.clear()

    # ------- proactive presence -------
    def load_proactive_settings(self) -> dict:
        default = {
            "enabled": True,
            "dnd": False,
            "timezone": PROACTIVE_DEFAULT_TIMEZONE,
            "permission_states": {},
            "updated_at": datetime.now().isoformat(),
        }
        try:
            if self.proactive_settings_file.exists():
                data = json.loads(self.proactive_settings_file.read_text())
                if isinstance(data, dict):
                    merged = dict(default)
                    merged.update(data)
                    if not isinstance(merged.get("permission_states"), dict):
                        merged["permission_states"] = {}
                    return merged
        except Exception as e:
            print(f"[{self.user_id}/proactive] settings load failed: {e}")
        return default

    def save_proactive_settings(self, patch: dict) -> dict:
        allowed = {"enabled", "dnd", "timezone", "permission_states"}
        cur = self.load_proactive_settings()
        for key, value in (patch or {}).items():
            if key not in allowed:
                continue
            if key in {"enabled", "dnd"}:
                cur[key] = bool(value)
            elif key == "timezone":
                tz_name = str(value or "").strip()
                try:
                    ZoneInfo(tz_name)
                except ZoneInfoNotFoundError:
                    continue
                cur[key] = tz_name
            elif key == "permission_states" and isinstance(value, dict):
                states = dict(cur.get("permission_states") or {})
                for pname, pstate in value.items():
                    states[str(pname)] = str(pstate)
                cur["permission_states"] = states
        cur["updated_at"] = datetime.now().isoformat()
        with self.proactive_lock:
            self.proactive_settings_file.write_text(json.dumps(cur, ensure_ascii=False, indent=2))
        return cur

    def _append_jsonl(self, path: Path, entry: dict) -> None:
        with self.proactive_lock:
            with open(path, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_jsonl(self, path: Path, limit: int = 100, since_epoch: float = 0.0) -> list[dict]:
        rows: list[dict] = []
        if not path.exists():
            return rows
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if since_epoch:
                        ts = float(item.get("ts", item.get("ts_epoch", 0)) or 0)
                        if ts <= since_epoch:
                            continue
                    rows.append(item)
        except Exception as e:
            print(f"[{self.user_id}/proactive] jsonl read failed path={path.name}: {e}")
            return []
        if limit > 0:
            return rows[-limit:]
        return rows

    def append_device_event(self, event: dict) -> dict:
        self._append_jsonl(self.device_events_file, event)
        self._prune_device_events()
        return event

    def list_device_events(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return self._read_jsonl(self.device_events_file, limit=limit, since_epoch=since_epoch)

    def _prune_device_events(self) -> None:
        cutoff = time.time() - DEVICE_EVENT_RETENTION_DAYS * 86400
        rows = self._read_jsonl(self.device_events_file, limit=0, since_epoch=cutoff)
        try:
            tmp = self.device_events_file.with_suffix(".jsonl.tmp")
            with open(tmp, "w") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(self.device_events_file)
        except Exception as e:
            print(f"[{self.user_id}/device-events] prune failed: {e}")

    def append_gate_decision(self, decision: dict) -> dict:
        self._append_jsonl(self.gate_decisions_file, decision)
        return decision

    def list_gate_decisions(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return self._read_jsonl(self.gate_decisions_file, limit=limit, since_epoch=since_epoch)

    def append_gate_review(self, review: dict) -> dict:
        self._append_jsonl(self.gate_reviews_file, review)
        return review

    def list_gate_reviews(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return self._read_jsonl(self.gate_reviews_file, limit=limit, since_epoch=since_epoch)

    def append_proactive_job(self, job: dict) -> dict:
        self._append_jsonl(self.proactive_jobs_file, job)
        self._trim_proactive_jobs()
        self.notify_proactive_job_waiters()
        return job

    def list_proactive_jobs(self, since_epoch: float = 0.0, limit: int = 100) -> list[dict]:
        return self._read_jsonl(self.proactive_jobs_file, limit=limit, since_epoch=since_epoch)

    def update_proactive_job(
        self,
        job_id: str,
        fields: dict,
        *,
        only_if_status: str | None = None,
    ) -> dict | None:
        """Patch one hidden proactive job in-place.

        Jobs are kept as JSONL for append/read simplicity, but status needs a
        real lifecycle so the debug dashboard can distinguish "not consumed"
        from "agent failed" from "chat write delivered".
        """
        job_id = str(job_id or "").strip()
        if not job_id:
            return None
        allowed = {
            "status",
            "status_reason",
            "consumer_id",
            "claimed_at",
            "realizing_at",
            "posted_at",
            "failed_at",
            "updated_at",
            "chat_message_id",
        }
        patch = {k: v for k, v in (fields or {}).items() if k in allowed}
        if not patch:
            return None
        patch["updated_at"] = datetime.now().isoformat()
        with self.proactive_lock:
            rows = self._read_jsonl(self.proactive_jobs_file, limit=0, since_epoch=0.0)
            changed: dict | None = None
            for row in rows:
                if str(row.get("job_id") or "") != job_id:
                    continue
                cur_status = str(row.get("status") or "pending")
                if only_if_status is not None and cur_status != only_if_status:
                    return None
                row.update(patch)
                changed = row
                break
            if changed is None:
                return None
            tmp = self.proactive_jobs_file.with_suffix(".jsonl.tmp")
            with open(tmp, "w") as f:
                for row in rows[-PROACTIVE_JOB_MAX:]:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(self.proactive_jobs_file)
        self.notify_proactive_job_waiters()
        return changed

    def _trim_proactive_jobs(self) -> None:
        rows = self._read_jsonl(self.proactive_jobs_file, limit=PROACTIVE_JOB_MAX, since_epoch=0.0)
        try:
            tmp = self.proactive_jobs_file.with_suffix(".jsonl.tmp")
            with open(tmp, "w") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(self.proactive_jobs_file)
        except Exception as e:
            print(f"[{self.user_id}/proactive-jobs] trim failed: {e}")

    def notify_proactive_job_waiters(self):
        with self.proactive_job_waiters_lock:
            for ev in self.proactive_job_waiters:
                ev.set()
            self.proactive_job_waiters.clear()


# Registry of per-user stores
_stores: dict[str, UserStore] = {}
_stores_lock = threading.Lock()


def get_store(user_id: str) -> UserStore:
    with _stores_lock:
        store = _stores.get(user_id)
        if store is None:
            store = UserStore(user_id)
            _stores[user_id] = store
        return store


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


def _extract_api_key() -> str | None:
    key = request.headers.get("X-API-Key", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    qkey = request.args.get("key", "").strip()
    if qkey:
        return qkey
    return None


def require_user() -> UserStore:
    """Return the UserStore for the current request. Aborts 401 on bad auth."""
    key = _extract_api_key()
    if not key:
        abort(401)
    user_id = _resolve_user(key)
    if not user_id:
        abort(401)
    g.user_id = user_id
    return get_store(user_id)


# ---------------------------------------------------------------------------
# Frames helpers
# ---------------------------------------------------------------------------


def _frame_url(store: UserStore, filename: str) -> str:
    base = os.environ.get("FEEDLING_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        try:
            base = request.host_url.rstrip("/")
        except RuntimeError:
            base = ""
    return f"{base}/v1/screen/frames/{filename}?user={store.user_id}"


def _save_frame(store: UserStore, payload: dict):
    """Save a v1 frame envelope. See docs/DESIGN_E2E.md §3.2.

    Wire format:
      {"type":"frame","ts":..., "envelope":{
          "v":1,"id":...,"body_ct":...,"nonce":...,
          "K_user":...,"K_enclave":...,
          "visibility":"shared","owner_user_id":...}}

    The JPEG + OCR are inside `body_ct` (ChaCha20-Poly1305 AEAD bound to
    owner|v|id). Server never decrypts — it writes the envelope to
    <frames_dir>/<id>.env.json and appends the item to frames_meta with
    `encrypted=True` so the UI + enclave path can find it.
    """
    env = payload.get("envelope")
    if not (isinstance(env, dict) and env.get("v") and env.get("body_ct")):
        print(f"[ingest:{store.user_id}] rejecting frame without v1 envelope")
        return
    _save_frame_envelope(store, payload, env)


def _save_frame_envelope(store: UserStore, payload: dict, env: dict):
    """Persist a v1 frame envelope. The ciphertext blob is big (>150KB for
    typical screen frames) so we keep it on disk as a separate .env.json
    instead of inlining into frames_meta. frames_meta gets a lightweight
    index entry with `encrypted=True`.
    """
    item_id = env.get("id") or uuid.uuid4().hex
    ts = payload.get("ts") or time.time()
    env_path = store.frames_dir / f"{item_id}.env.json"
    try:
        env_path.write_text(json.dumps(env))
    except Exception as e:
        print(f"[ingest:{store.user_id}] envelope write failed id={item_id}: {e}")
        return

    meta = {
        "filename": f"{item_id}.env.json",
        "ts": ts,
        "app": None,         # unknown — inside ciphertext
        "ocr_text": "",      # unknown — inside ciphertext
        "w": payload.get("w", 0),
        "h": payload.get("h", 0),
        "encrypted": True,
        "id": item_id,
        "v": env.get("v", 1),
        "owner_user_id": env.get("owner_user_id"),
    }

    with store.frames_lock:
        store.frames_meta.append(meta)
        if len(store.frames_meta) > MAX_FRAMES:
            removed = store.frames_meta.pop(0)
            old = store.frames_dir / removed["filename"]
            if old.exists():
                old.unlink()
        store._persist_frames_meta()

    body_len = len(env.get("body_ct") or "")
    print(f"[ingest:{store.user_id}] saved v1 frame id={item_id} body_ct_len={body_len}")


# ---------------------------------------------------------------------------
# Token entry helpers (pure functions over the per-user list)
# ---------------------------------------------------------------------------


def _is_live_activity_token(entry: dict) -> bool:
    return entry.get("type") in ("live-activity", "live_activity")


def _is_push_to_start_token(entry: dict) -> bool:
    return entry.get("type") == "push_to_start"


def _is_device_token(entry: dict) -> bool:
    return entry.get("type") in ("device", "apns")


def _entry_is_active(entry: dict) -> bool:
    return (entry.get("status") or "active") == "active"


def _select_token(store: UserStore, predicate, activity_id: str | None = None, active_only: bool = True):
    candidates = _select_tokens(store, predicate, activity_id=activity_id, active_only=active_only)
    return candidates[0] if candidates else None


def _select_tokens(store: UserStore, predicate, activity_id: str | None = None, active_only: bool = True) -> list[dict]:
    candidates = []
    for raw in store.tokens:
        entry = _normalize_token_entry(raw)
        if not predicate(entry):
            continue
        if activity_id and entry.get("activity_id") != activity_id:
            continue
        if active_only and not _entry_is_active(entry):
            continue
        if not entry.get("token"):
            continue
        candidates.append(entry)

    candidates.sort(key=lambda x: x.get("registered_at", ""), reverse=True)
    return candidates


def _update_token_lifecycle(
    store: UserStore,
    entry: dict,
    *,
    status: str | None = None,
    last_error: str | None = None,
    success: bool = False,
    apns_env: str | None = None,
):
    token = entry.get("token")
    token_type = entry.get("type")
    activity_id = entry.get("activity_id")
    now_iso = datetime.now().isoformat()

    changed = False
    for idx, raw in enumerate(store.tokens):
        cur = _normalize_token_entry(raw)
        if cur.get("token") != token or cur.get("type") != token_type or cur.get("activity_id") != activity_id:
            continue
        if status is not None:
            cur["status"] = status
            if status == "expired":
                cur["expired_at"] = now_iso
        if last_error is not None:
            cur["last_error"] = last_error
            cur["last_error_at"] = now_iso
        if success:
            cur["last_success_at"] = now_iso
            cur["status"] = "active"
            cur["expired_at"] = ""
            cur["last_error"] = ""
            cur["last_error_at"] = ""
        if apns_env:
            cur["apns_env"] = apns_env
        cur["updated_at"] = now_iso
        store.tokens[idx] = cur
        changed = True
        break

    if changed:
        store._save_tokens()


def _mark_expired_token(store: UserStore, entry: dict, reason: str):
    _update_token_lifecycle(store, entry, status="expired", last_error=reason)


def _mark_active_token_success(store: UserStore, entry: dict, apns_env: str | None = None):
    _update_token_lifecycle(store, entry, success=True, apns_env=apns_env)


# ---------------------------------------------------------------------------
# Semantic screen classifier — imported from a portable module so the iOS
# port can translate 1:1. See backend/semantic_analysis.py and
# docs/DESIGN_E2E.md §4 for the "classification on iOS" plan.
# ---------------------------------------------------------------------------

from semantic_analysis import analyze as _semantic_analysis  # noqa: E402


# ---------------------------------------------------------------------------
# Proactive Gate helpers
# ---------------------------------------------------------------------------

_DEVICE_EVENT_ALLOWED_KEYS = {
    "permission",
    "status",
    "source",
    "type",
    "category",
    "place_type",
    "workout_type",
    "duration_min",
    "distance_bucket",
    "starts_in_min",
    "ended_min_ago",
    "is_busy",
    "has_location",
    "motion",
    "time_of_day",
    "scene_tags",
}

_DEVICE_EVENT_DROP_RE = re.compile(
    r"(raw|text|content|title|name|address|photo|image|lat|lng|lon|coordinate|phone|email)",
    re.IGNORECASE,
)


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _redact_device_payload(payload: dict) -> dict:
    """Persist only coarse device-event facts.

    Raw location/photo/calendar text belongs in the user device or encrypted
    frame store, not in server-side proactive logs. The Gate needs enough
    signal to decide whether to create a job; the real agent can ask for richer
    context later through the normal encrypted path.
    """
    if not isinstance(payload, dict):
        return {}

    redacted: dict = {}
    for key, value in payload.items():
        skey = str(key)
        if _DEVICE_EVENT_DROP_RE.search(skey):
            continue
        if skey not in _DEVICE_EVENT_ALLOWED_KEYS and not skey.startswith("safe_"):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            redacted[skey] = value
        elif isinstance(value, str):
            redacted[skey] = value[:120]
        elif isinstance(value, list):
            safe_items = []
            for item in value[:12]:
                if isinstance(item, (str, int, float, bool)):
                    safe_items.append(item if not isinstance(item, str) else item[:80])
            redacted[skey] = safe_items
    return redacted


def _make_device_event(source: str, event_type: str, payload: dict) -> dict:
    now = time.time()
    return {
        "event_id": _new_public_id("evt"),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "source": (source or "ios").strip()[:80],
        "type": (event_type or "unknown").strip()[:80],
        "payload": _redact_device_payload(payload),
    }


def _recent_user_chat_active(store: UserStore, now: float, window_sec: float = 600.0) -> bool:
    with store.chat_lock:
        for msg in reversed(store.chat_messages):
            ts = float(msg.get("ts", 0) or 0)
            if now - ts > window_sec:
                return False
            if msg.get("role") == "user":
                return True
    return False


def _recent_frame_meta(store: UserStore, now: float, window_sec: float) -> list[dict]:
    with store.frames_lock:
        frames = [f for f in store.frames_meta if now - float(f.get("ts", 0) or 0) <= window_sec]
    return frames[-PROACTIVE_GATE_FRAME_CANDIDATE_MAX:]


def _sample_frames_for_gate(frames: list[dict], max_frames: int = 5) -> list[dict]:
    """Metadata fallback sampler for Gate frames.

    The preferred path samples after decryption with visual hashes. This
    fallback is used when the Gate is blocked before decrypting or in tests
    that only provide frame metadata.
    """
    clean = [f for f in frames if isinstance(f, dict) and str(f.get("id") or "").strip()]
    clean.sort(key=lambda f: float(f.get("ts", 0) or 0))
    if len(clean) <= max_frames:
        return clean
    if max_frames <= 2:
        return [clean[-1]]
    picks = [clean[0], clean[-1]]
    middle = clean[1:-1]
    slots = max_frames - len(picks)
    if middle and slots > 0:
        if slots == 1:
            picks.append(middle[len(middle) // 2])
        else:
            for i in range(slots):
                idx = round(i * (len(middle) - 1) / max(1, slots - 1))
                picks.append(middle[idx])
    by_id: dict[str, dict] = {}
    for frame in picks:
        by_id[str(frame.get("id"))] = frame
    return sorted(by_id.values(), key=lambda f: float(f.get("ts", 0) or 0))


def _frame_ids(frames: list[dict]) -> list[str]:
    out: list[str] = []
    for frame in frames:
        frame_id = str(frame.get("id") or frame.get("frame_id") or "").strip()
        if frame_id:
            out.append(frame_id)
    return out


def _base64_payload(data_url_or_b64: str) -> str:
    raw = str(data_url_or_b64 or "").strip()
    if "," in raw and raw.lower().startswith("data:"):
        return raw.split(",", 1)[1]
    return raw


def _visual_hash_for_gate(image_b64: str) -> str | None:
    """Return a lightweight perceptual hash for decrypted screen frames.

    This uses an 8x8 grayscale average hash. It is intentionally small and
    deterministic: enough to collapse near-duplicate screen frames before the
    LLM call without adding a second model to the pipeline.
    """
    payload = _base64_payload(image_b64)
    if not payload:
        return None
    try:
        from PIL import Image  # type: ignore

        image = Image.open(io.BytesIO(base64.b64decode(payload))).convert("L").resize((8, 8))
        pixels = list(image.getdata())
        avg = sum(pixels) / max(1, len(pixels))
        bits = "".join("1" if px >= avg else "0" for px in pixels)
        return f"{int(bits, 2):016x}"
    except Exception:
        return None


def _hash_distance(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except Exception:
        return None


def _same_scene_fallback(a: dict, b: dict) -> bool:
    app_a = str(a.get("app") or "").strip().lower()
    app_b = str(b.get("app") or "").strip().lower()
    if app_a and app_b and app_a != "unknown" and app_a == app_b:
        text_a = " ".join(str(a.get("ocr_text") or "").split())[:240]
        text_b = " ".join(str(b.get("ocr_text") or "").split())[:240]
        return bool(text_a and text_a == text_b)
    return False


def _sample_frame_contexts_for_gate(frame_contexts: list[dict], max_frames: int = 5) -> list[dict]:
    """Cluster decrypted frames by scene and keep 3-5 representatives.

    Mirrors the product spec's "cluster by scene, cap at 5" behavior:
    consecutive near-duplicates collapse, the last frame is always retained,
    and active app switches get represented.
    """
    clean = [
        f for f in frame_contexts
        if isinstance(f, dict) and str(f.get("frame_id") or f.get("id") or "").strip()
    ]
    clean.sort(key=lambda f: float(f.get("ts", 0) or 0))
    if len(clean) <= max_frames:
        return clean

    hashes = [_visual_hash_for_gate(str(f.get("image_b64") or "")) for f in clean]
    clusters: list[list[int]] = [[0]]
    for i in range(1, len(clean)):
        dist = _hash_distance(hashes[i], hashes[i - 1])
        same_scene = (
            dist is not None and dist < PROACTIVE_GATE_SCENE_HASH_THRESHOLD
        ) or _same_scene_fallback(clean[i], clean[i - 1])
        if same_scene:
            clusters[-1].append(i)
        else:
            clusters.append([i])

    representatives = [clean[c[-1]] for c in clusters]
    if len(representatives) <= max_frames:
        return representatives

    rep_hashes = [_visual_hash_for_gate(str(f.get("image_b64") or "")) for f in representatives]

    def change_score(idx: int) -> int:
        left = _hash_distance(rep_hashes[idx], rep_hashes[idx - 1]) if idx > 0 else 0
        right = _hash_distance(rep_hashes[idx], rep_hashes[idx + 1]) if idx < len(rep_hashes) - 1 else 0
        if left is None and right is None:
            return 0
        return (left or 0) + (right or 0)

    keep_indexes = {0, len(representatives) - 1}
    middle = list(range(1, len(representatives) - 1))
    middle.sort(key=change_score, reverse=True)
    for idx in middle[:max(0, max_frames - len(keep_indexes))]:
        keep_indexes.add(idx)
    return [representatives[i] for i in sorted(keep_indexes)]


def _decrypt_frame_metadata_for_gate(
    store: UserStore,
    frame_id: str,
    api_key: str | None,
    include_image: bool = False,
) -> dict:
    """Best-effort decrypt of a frame for the automatic Gate.

    The Flask backend does not store raw API keys, so only request-scoped
    callers (iOS / resident consumer / manual curl) can run this path.
    """
    fid = str(frame_id or "").strip()
    if not fid:
        return {"frame_id": "", "error": "missing_frame_id"}
    if _find_envelope_path(store, fid) is None:
        return {"frame_id": fid, "error": "frame_not_found"}
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return {"frame_id": fid, "error": "enclave_unavailable"}
    if not api_key:
        return {"frame_id": fid, "error": "api_key_unavailable"}
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}/v1/screen/frames/{fid}/decrypt",
                headers={"X-API-Key": api_key},
                params={"include_image": "true" if include_image else "false"},
            )
        if resp.status_code >= 400:
            return {
                "frame_id": fid,
                "error": f"decrypt_http_{resp.status_code}",
                "body": resp.text[:240],
            }
        data = resp.json()
        if not isinstance(data, dict):
            return {"frame_id": fid, "error": "decrypt_non_object"}
        result = {
            "frame_id": fid,
            "id": fid,
            "ts": data.get("ts"),
            "app": data.get("app") or "unknown",
            "ocr_text": str(data.get("ocr_text") or "")[:1200],
            "w": data.get("w"),
            "h": data.get("h"),
            "image_mime": data.get("image_mime") or "image/jpeg",
        }
        if include_image and data.get("image_b64"):
            result["image_b64"] = str(data.get("image_b64") or "")
        return result
    except Exception as e:
        return {"frame_id": fid, "error": f"decrypt_error:{type(e).__name__}:{str(e)[:160]}"}


def _gate_ocr_summary(frame_contexts: list[dict], fallback_frames: list[dict]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for frame in reversed(frame_contexts):
        text = str(frame.get("ocr_text") or "").strip()
        if text and text not in seen:
            seen.add(text)
            parts.append(text[:320])
            if len(parts) >= 4:
                break
    if parts:
        return " | ".join(reversed(parts))[:1000]
    return _ocr_summary(fallback_frames)


def _gate_current_app(frame_contexts: list[dict], fallback_frames: list[dict]) -> str:
    for frame in reversed(frame_contexts):
        app_name = str(frame.get("app") or "").strip()
        if app_name and app_name != "unknown":
            return app_name[:120]
    for frame in reversed(fallback_frames):
        app_name = str(frame.get("app") or "").strip()
        if app_name:
            return app_name[:120]
    return "unknown"


def _explicit_help_signal(ocr: str) -> bool:
    text = (ocr or "").lower()
    if len(text.strip()) < 40:
        return False
    cues = (
        "帮我", "要不要", "怎么", "如何", "哪一个", "选哪个", "压成", "总结",
        "review", "compare", "which", "should i", "help me", "summarize",
    )
    return any(cue in text for cue in cues) or "?" in text or "？" in text


def _recent_proactive_fire_active(store: UserStore, now: float, window_sec: float = 600.0) -> bool:
    with store.chat_lock:
        for msg in reversed(store.chat_messages):
            if msg.get("source") != PROACTIVE_JOB_SOURCE:
                continue
            ts = float(msg.get("ts", 0) or 0)
            if now - ts <= window_sec:
                return True
            return False
    return False


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _now_local_context(payload: dict, settings: dict, now: float) -> dict:
    tz_name = str(
        payload.get("timezone")
        or payload.get("tz")
        or settings.get("timezone")
        or PROACTIVE_DEFAULT_TIMEZONE
    ).strip() or "UTC"
    tz = _safe_zoneinfo(tz_name)
    local_dt = datetime.fromtimestamp(now, tz)
    return {
        "iso": local_dt.isoformat(),
        "date": local_dt.date().isoformat(),
        "time": local_dt.strftime("%H:%M"),
        "weekday": local_dt.strftime("%A"),
        "timezone": tz_name,
    }


def _enclave_get_json_for_gate(path: str, api_key: str | None, params: dict | None = None) -> tuple[dict | None, str]:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return None, "enclave_unavailable"
    if not api_key:
        return None, "api_key_unavailable"
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.get(
                f"{enclave_url}{path}",
                headers={"X-API-Key": api_key},
                params=params or {},
            )
        if resp.status_code >= 400:
            return None, f"enclave_http_{resp.status_code}:{resp.text[:160]}"
        data = resp.json()
        if not isinstance(data, dict):
            return None, "enclave_non_object"
        return data, ""
    except Exception as e:
        return None, f"enclave_error:{type(e).__name__}:{str(e)[:120]}"


def _summarize_identity_for_gate(identity: dict | None) -> tuple[dict, list[dict]]:
    if not isinstance(identity, dict):
        return {}, []
    agent_name = str(identity.get("agent_name") or "").strip()
    summary = {
        "agent_name": agent_name,
        "self_introduction": str(identity.get("self_introduction") or "")[:800],
        "days_with_user": identity.get("days_with_user"),
        "category": str(identity.get("category") or "")[:160],
        "signature": identity.get("signature") if isinstance(identity.get("signature"), list) else [],
        "interruption_preferences": identity.get("interruption_preferences")
        or identity.get("push_preferences")
        or identity.get("proactive_preferences")
        or {},
    }
    dimensions = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    clean_dims: list[dict] = []
    connection_rows: list[dict] = []
    for idx, dim in enumerate(dimensions[:12]):
        if not isinstance(dim, dict):
            continue
        name = str(dim.get("name") or dim.get("label") or f"dimension_{idx + 1}").strip()
        desc = str(dim.get("description") or dim.get("evidence") or "").strip()
        row = {
            "id": f"identity.dimension.{idx + 1}",
            "name": name[:120],
            "score": dim.get("score"),
            "description": desc[:400],
        }
        clean_dims.append(row)
        if name or desc:
            connection_rows.append({
                "source_type": "identity_card",
                "source_id": row["id"],
                "quote": f"{name}: {desc}".strip(": ")[:500],
            })
    summary["dimensions"] = clean_dims
    if agent_name:
        connection_rows.append({
            "source_type": "identity_card",
            "source_id": "identity.agent_name",
            "quote": f"agent_name={agent_name}",
        })
    return summary, connection_rows


def _moment_is_passive_observation(moment: dict) -> bool:
    fields = [
        moment.get("type"),
        moment.get("source"),
        moment.get("category"),
        *(moment.get("tags") if isinstance(moment.get("tags"), list) else []),
    ]
    text = " ".join(str(x or "") for x in fields).lower()
    return "agent_passive_observation" in text or "passive_observation" in text


def _summarize_moments_for_gate(moments: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    memory_set: list[dict] = []
    passive_observations: list[dict] = []
    connection_rows: list[dict] = []
    for moment in moments:
        if not isinstance(moment, dict):
            continue
        if moment.get("decrypt_status") and moment.get("decrypt_status") != "ok":
            continue
        mid = str(moment.get("id") or "").strip()
        if not mid:
            continue
        title = str(moment.get("title") or "").strip()
        desc = str(moment.get("description") or "").strip()
        row = {
            "id": mid,
            "type": str(moment.get("type") or "")[:80],
            "source": str(moment.get("source") or "")[:80],
            "occurred_at": moment.get("occurred_at"),
            "title": title[:220],
            "description": desc[:700],
        }
        if _moment_is_passive_observation(moment):
            passive_observations.append(row)
            source_type = "passive_observation"
        else:
            memory_set.append(row)
            source_type = "memory_set"
        quote = " — ".join(part for part in [title, desc] if part)[:700]
        if quote:
            connection_rows.append({
                "source_type": source_type,
                "source_id": mid,
                "quote": quote,
            })
    return memory_set[:80], passive_observations[:10], connection_rows[:120]


def _recent_fires_for_gate(store: UserStore, now: float) -> list[dict]:
    fires: list[dict] = []
    since = now - 86400
    decisions = store.list_gate_decisions(since_epoch=since, limit=80)
    for row in decisions:
        if not row.get("should_reach_out"):
            continue
        fires.append({
            "decision_id": row.get("decision_id"),
            "ts": row.get("ts"),
            "intent_label": row.get("intent_label", ""),
            "context_hint": row.get("context_hint", "")[:500],
            "reason": row.get("reason", ""),
            "connection": row.get("connection") or {},
            "frame_ids": row.get("frame_ids", [])[:5],
        })
    return fires[-20:]


def _build_gate_memory_context(
    store: UserStore,
    api_key: str | None,
    payload: dict,
    settings: dict,
    now: float,
) -> dict:
    identity_data, identity_error = _enclave_get_json_for_gate("/v1/identity/get", api_key)
    memory_data, memory_error = _enclave_get_json_for_gate("/v1/memory/list", api_key, {"limit": "200"})

    identity, identity_connections = _summarize_identity_for_gate(
        (identity_data or {}).get("identity") if isinstance(identity_data, dict) else None
    )
    memory_set, passive_observations, memory_connections = _summarize_moments_for_gate(
        (memory_data or {}).get("moments") if isinstance((memory_data or {}).get("moments"), list) else []
    )
    connection_candidates = identity_connections + memory_connections
    return {
        "identity_card": identity,
        "memory_set": memory_set,
        "passive_observations": passive_observations,
        "recent_fires": _recent_fires_for_gate(store, now),
        "now_local": _now_local_context(payload, settings, now),
        "connection_candidates": connection_candidates,
        "context_errors": {
            "identity": identity_error,
            "memory": memory_error,
        },
    }


def _gate_context_connection_ids(gate_context: dict) -> set[str]:
    return {
        str(row.get("source_id") or "").strip()
        for row in gate_context.get("connection_candidates", [])
        if isinstance(row, dict) and str(row.get("source_id") or "").strip()
    }


def _strip_json_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


PROACTIVE_GATE_SYSTEM_PROMPT = (
    "You are the proactive gate for the user's personal AI companion. "
    "Your job is to decide whether the companion would naturally think of the user in this moment. "
    "Naturally think of has exactly one criterion: a concrete connection between the current screen/context "
    "and something specific in identity_card, memory_set, or passive_observations. "
    "Return JSON only. Do not reveal chain-of-thought. Do not write the final user-facing message."
)


def _normalize_connection(raw_connection) -> dict:
    if not isinstance(raw_connection, dict):
        return {}
    return {
        "source_type": str(raw_connection.get("source_type") or "")[:80],
        "source_id": str(raw_connection.get("source_id") or "")[:160],
        "quote": str(raw_connection.get("quote") or "")[:700],
        "why_concrete": str(raw_connection.get("why_concrete") or "")[:700],
    }


def _coerce_llm_gate_payload(
    raw: dict,
    selected_frame_ids: list[str],
    allowed_connection_ids: set[str] | None = None,
) -> dict:
    if not isinstance(raw, dict):
        return {
            "should_reach_out": False,
            "confidence": 0.0,
            "intent_label": "invalid_gate_response",
            "context_hint": "",
            "reason": "llm_non_object",
            "abstention_reason": "llm_non_object",
            "connection": {},
            "frame_ids": [],
        }

    allowed_ids = set(selected_frame_ids)
    requested_ids = raw.get("frame_ids")
    if not isinstance(requested_ids, list):
        requested_ids = selected_frame_ids if raw.get("should_reach_out") else []
    frame_ids = [
        str(fid) for fid in requested_ids
        if str(fid) in allowed_ids
    ][:PROACTIVE_GATE_MAX_FRAMES]

    connections = raw.get("connections")
    if not isinstance(connections, list):
        connections = []

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    connection = _normalize_connection(raw.get("connection"))
    connections = raw.get("connections")
    if isinstance(connections, list) and not connection:
        for item in connections:
            connection = _normalize_connection(item)
            if connection:
                break

    should = bool(raw.get("should_reach_out"))
    context_hint = str(raw.get("context_hint") or "").strip()[:2000]
    abstention_reason = str(raw.get("abstention_reason") or raw.get("reason") or "").strip()[:500]
    reason = str(raw.get("reason") or ("llm_true" if should else abstention_reason or "llm_false"))[:240]

    allowed_connection_ids = allowed_connection_ids or set()
    if should:
        if not context_hint:
            should = False
            reason = "llm_missing_context_hint"
            abstention_reason = "llm_missing_context_hint"
        elif not connection.get("source_id"):
            should = False
            reason = "llm_missing_concrete_connection"
            abstention_reason = "llm_missing_concrete_connection"
        elif allowed_connection_ids and connection.get("source_id") not in allowed_connection_ids:
            should = False
            reason = "llm_unrecognized_connection"
            abstention_reason = "llm_unrecognized_connection"

    return {
        "should_reach_out": should,
        "confidence": max(0.0, min(1.0, confidence)),
        "intent_label": str(raw.get("intent_label") or raw.get("intent") or "proactive_screen_context")[:120],
        "context_hint": context_hint if should else "",
        "reason": reason,
        "abstention_reason": "" if should else (abstention_reason or reason),
        "connection": connection if should else {},
        "frame_ids": frame_ids,
    }


def _call_openrouter_proactive_gate(
    *,
    frame_contexts: list[dict],
    device_events: list[dict],
    ocr_summary: str,
    gate_context: dict,
) -> dict:
    """Call the model that decides whether Feedling should proactively speak.

    The model is a Gate only. It writes a hidden job context, not the final
    user-facing message; the resident consumer still hands that job to the
    user's real agent entry so persona stays owned by the user's agent.
    """
    if PROACTIVE_GATE_PROVIDER != "openrouter":
        return {"ok": False, "error": f"unsupported_provider:{PROACTIVE_GATE_PROVIDER}"}
    if not OPENROUTER_API_KEY:
        return {"ok": False, "error": "model_not_configured"}
    if not PROACTIVE_GATE_MODEL:
        return {"ok": False, "error": "model_not_configured"}

    selected_frame_ids = [str(f.get("frame_id") or "") for f in frame_contexts if f.get("frame_id")]
    allowed_connection_ids = _gate_context_connection_ids(gate_context)
    metadata_frames = []
    content: list[dict] = []
    for idx, frame in enumerate(frame_contexts, start=1):
        frame_id = str(frame.get("frame_id") or "")
        image_b64 = str(frame.get("image_b64") or "")
        metadata_frames.append({
            "index": idx,
            "frame_id": frame_id,
            "ts": frame.get("ts"),
            "app": frame.get("app") or "unknown",
            "w": frame.get("w"),
            "h": frame.get("h"),
            "ocr_text": str(frame.get("ocr_text") or "")[:1200],
            "has_image": bool(image_b64),
            "decrypt_error": frame.get("error", ""),
        })
    gate_payload = {
        "task": "Decide whether the user's own AI companion should proactively send one message now.",
        "gate_role": (
            "You are deciding whether the companion would naturally think of the user now. "
            "This product is for high-recall AI companionship, not a low-interruption work assistant."
        ),
        "core_criterion": (
            "should_reach_out=true requires a concrete connection between visible/current context "
            "and a specific source_id in connection_candidates."
        ),
        "valid_connections": [
            "A named topic/person/place/object on screen directly matches a memory, identity dimension, or passive observation.",
            "The user is doing something that matches an established preference, emotional pattern, project, relationship, or repeated concern in memory.",
            "The local time/context makes a memory-linked ritual or recurring situation relevant now.",
            "The screen suggests a moment the companion has specifically helped with before, grounded in a named memory or observation.",
        ],
        "invalid_connections": [
            "Generic claims like the user might be overwhelmed, might want help, or perhaps needs a summary.",
            "A useful work-assistant opportunity with no memory/identity match.",
            "Idle browsing, repeated content already fired in recent_fires, or vague similarity to the user's interests.",
            "Any decision based only on OCR keywords without a concrete source_id.",
        ],
        "decision_policy": [
            "Prefer high recall when the connection is concrete and emotionally/contextually natural for a companion.",
            "Use interruption_preferences from identity_card when present; otherwise keep messages short and easy to ignore.",
            "Use recent_fires to avoid repeating the same idea within 24 hours.",
            "Use images as primary evidence. OCR is unreliable auxiliary metadata.",
            "Do not reveal chain-of-thought. Put only short audit text in reason or abstention_reason.",
            "Do not write the final user-facing message. Write only hidden context_hint for the user's resident agent.",
        ],
        "output_schema": {
            "should_reach_out": "boolean",
            "confidence": "number 0..1",
            "intent_label": "short snake_case label",
            "context_hint": "hidden context for the user's resident agent, 1-3 sentences",
            "reason": "short positive audit reason when true",
            "abstention_reason": "required short reason when false",
            "connection": {
                "source_type": "identity_card | memory_set | passive_observation",
                "source_id": "must match one source_id from connection_candidates",
                "quote": "short supporting text from that source",
                "why_concrete": "why the current screen connects to that source",
            },
            "frame_ids": "array of frame ids used",
        },
        "gate_context": {
            "identity_card": gate_context.get("identity_card") or {},
            "memory_set": gate_context.get("memory_set") or [],
            "passive_observations": gate_context.get("passive_observations") or [],
            "recent_fires": gate_context.get("recent_fires") or [],
            "now_local": gate_context.get("now_local") or {},
            "connection_candidates": gate_context.get("connection_candidates") or [],
        },
        "ocr_summary": ocr_summary[:1200],
        "frames": metadata_frames,
        "device_events": device_events[-10:],
    }

    content.append({
        "type": "text",
        "text": (
            "You are Feedling Proactive Gate. Return JSON only, no markdown.\n"
            + json.dumps(gate_payload, ensure_ascii=False)
        ),
    })
    for frame in frame_contexts:
        image_b64 = str(frame.get("image_b64") or "")
        if not image_b64:
            continue
        mime = str(frame.get("image_mime") or "image/jpeg")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
        })

    body = {
        "model": PROACTIVE_GATE_MODEL,
        "messages": [
            {"role": "system", "content": PROACTIVE_GATE_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 600,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("FEEDLING_PUBLIC_URL", "https://feedling.app"),
        "X-Title": "Feedling Proactive Gate",
    }
    try:
        with httpx.Client(timeout=PROACTIVE_GATE_TIMEOUT_SEC) as client:
            resp = client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body)
        if resp.status_code >= 400:
            return {"ok": False, "error": f"llm_http_{resp.status_code}:{resp.text[:240]}"}
        data = resp.json()
        content_obj = (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
        if isinstance(content_obj, list):
            text = "\n".join(str(part.get("text") or "") for part in content_obj if isinstance(part, dict))
        else:
            text = str(content_obj or "")
        parsed = json.loads(_strip_json_code_fence(text))
        return {
            "ok": True,
            "raw": _coerce_llm_gate_payload(parsed, selected_frame_ids, allowed_connection_ids),
            "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
        }
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"llm_json_parse:{str(e)[:160]}"}
    except Exception as e:
        return {"ok": False, "error": f"llm_error:{type(e).__name__}:{str(e)[:160]}"}


def _ocr_summary(frames: list[dict]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for frame in reversed(frames):
        text = (frame.get("ocr_text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text[:240])
        if len(parts) >= 3:
            break
    return " | ".join(reversed(parts))[:700]


def _recent_device_events_for_gate(store: UserStore, now: float, window_sec: float) -> list[dict]:
    since = max(0.0, now - window_sec)
    return store.list_device_events(since_epoch=since, limit=25)


def _payload_float(payload: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _build_proactive_gate_decision(store: UserStore, payload: dict, api_key: str | None = None) -> dict:
    now = time.time()
    payload = payload if isinstance(payload, dict) else {}
    force = bool(payload.get("force", False))
    settings = store.load_proactive_settings()
    is_manual_hint = bool(str(payload.get("context_hint") or "").strip())

    payload_frames = payload.get("frames")
    if isinstance(payload_frames, list) and payload_frames:
        frames = [
            f for f in payload_frames
            if isinstance(f, dict) and str(f.get("id") or f.get("frame_id") or "").strip()
        ]
        for f in frames:
            if not f.get("id") and f.get("frame_id"):
                f["id"] = f.get("frame_id")
    else:
        frames = _recent_frame_meta(
            store,
            now,
            _payload_float(payload, "frame_window_sec", 300.0, 30.0, 3600.0),
        )
    candidate_frames = _sample_frames_for_gate(
        frames,
        max_frames=PROACTIVE_GATE_FRAME_CANDIDATE_MAX,
    )
    frame_ids = _frame_ids(candidate_frames)
    device_events = _recent_device_events_for_gate(
        store,
        now,
        _payload_float(payload, "device_event_window_sec", 900.0, 30.0, 86400.0),
    )

    block_reason = ""
    if not settings.get("enabled", True) and not force:
        block_reason = "proactive_disabled"
    elif settings.get("dnd", False) and not force:
        block_reason = "dnd_enabled"
    elif not frames and not force:
        block_reason = "no_recent_frames"
    elif _recent_proactive_fire_active(store, now) and not force:
        block_reason = "recent_proactive_fire"

    context_hint = str(payload.get("context_hint") or "").strip()
    connections = payload.get("connections")
    if not isinstance(connections, list):
        connections = []
    connections = [str(c).strip()[:240] for c in connections if str(c).strip()][:5]

    gate_context = (
        _build_gate_memory_context(store, api_key, payload, settings, now)
        if not is_manual_hint and not block_reason else {}
    )
    allowed_connection_ids = _gate_context_connection_ids(gate_context)

    candidate_frame_contexts = [
        _decrypt_frame_metadata_for_gate(
            store,
            fid,
            api_key,
            include_image=bool(PROACTIVE_GATE_INCLUDE_IMAGES and not is_manual_hint and not block_reason),
        )
        for fid in frame_ids
    ] if frame_ids and not is_manual_hint else []
    frame_contexts = _sample_frame_contexts_for_gate(
        candidate_frame_contexts,
        max_frames=PROACTIVE_GATE_MAX_FRAMES,
    ) if candidate_frame_contexts else []
    selected_frames = frame_contexts or _sample_frames_for_gate(candidate_frames, max_frames=PROACTIVE_GATE_MAX_FRAMES)
    frame_ids = _frame_ids(selected_frames)
    current_app = str(payload.get("current_app") or "").strip()
    if not current_app:
        current_app = _gate_current_app(frame_contexts, candidate_frames)
    ocr = str(payload.get("ocr_summary") or "").strip()
    if not ocr:
        ocr = _gate_ocr_summary(frame_contexts, candidate_frames)

    decrypt_errors = [f.get("error") for f in frame_contexts if f.get("error")]
    decrypt_ok = any(
        str(f.get("image_b64") or "").strip()
        or str(f.get("ocr_text") or "").strip()
        or str(f.get("app") or "").strip() not in {"", "unknown"}
        for f in frame_contexts
    )
    image_count = sum(1 for f in frame_contexts if str(f.get("image_b64") or "").strip())

    semantic_reference = _semantic_analysis(current_app=current_app, ocr_summary=ocr)
    llm_payload: dict = {}
    llm_usage: dict = {}
    llm_error = ""
    llm_called = False

    if is_manual_hint:
        should_reach_out = True
        reason = "manual_hint"
        intent_label = str(payload.get("intent_label") or "manual_proactive_test").strip()
    elif block_reason:
        should_reach_out = False
        reason = block_reason
        intent_label = "blocked_before_model"
    elif frame_ids and not decrypt_ok and not force:
        should_reach_out = False
        reason = "frame_decrypt_unavailable"
        intent_label = "frame_decrypt_unavailable"
    elif not allowed_connection_ids and not force:
        should_reach_out = False
        reason = "memory_context_unavailable"
        intent_label = "memory_context_unavailable"
    else:
        llm_called = True
        llm_result = _call_openrouter_proactive_gate(
            frame_contexts=frame_contexts,
            device_events=device_events,
            ocr_summary=ocr,
            gate_context=gate_context,
        )
        if llm_result.get("ok"):
            llm_payload = llm_result.get("raw") or {}
            llm_usage = llm_result.get("usage") if isinstance(llm_result.get("usage"), dict) else {}
            should_reach_out = bool(llm_payload.get("should_reach_out"))
            reason = str(
                llm_payload.get("reason")
                or llm_payload.get("abstention_reason")
                or ("llm_true" if should_reach_out else "llm_false")
            )[:240]
            intent_label = str(llm_payload.get("intent_label") or "llm_proactive_gate").strip()
            if should_reach_out:
                context_hint = str(llm_payload.get("context_hint") or "").strip()
                connection = llm_payload.get("connection") if isinstance(llm_payload.get("connection"), dict) else {}
                connections = [
                    str(connection.get("quote") or "").strip()[:240],
                    str(connection.get("why_concrete") or "").strip()[:240],
                ]
                connections = [c for c in connections if c][:5]
                frame_ids = llm_payload.get("frame_ids") if isinstance(llm_payload.get("frame_ids"), list) else frame_ids
                frame_ids = [str(fid) for fid in frame_ids if str(fid) in set(_frame_ids(selected_frames))][:PROACTIVE_GATE_MAX_FRAMES]
                if not context_hint:
                    should_reach_out = False
                    reason = "llm_missing_context_hint"
            else:
                context_hint = ""
        else:
            should_reach_out = False
            llm_error = str(llm_result.get("error") or "llm_error")[:240]
            reason = llm_error
            intent_label = "llm_gate_error"

    decision_id = _new_public_id("gd")
    return {
        "decision_id": decision_id,
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "gate_model": "manual_v0a" if is_manual_hint else f"{PROACTIVE_GATE_PROVIDER}:{PROACTIVE_GATE_MODEL}",
        "should_reach_out": should_reach_out,
        "should_garden_passive": False,
        "abstention_reason": "" if should_reach_out else reason,
        "reason": reason,
        "intent_label": intent_label[:120],
        "context_hint": context_hint[:2000],
        "connections": connections,
        "connection": (
            llm_payload.get("connection")
            if should_reach_out and isinstance(llm_payload.get("connection"), dict)
            else {}
        ),
        "frame_ids": frame_ids,
        "device_event_ids": [str(e.get("event_id")) for e in device_events if e.get("event_id")][:10],
        "current_app": current_app,
        "semantic": {
            "reference": semantic_reference,
            "llm_confidence": llm_payload.get("confidence"),
            "llm_usage": llm_usage,
        },
        "gate_input": {
            "ocr_chars": len(ocr),
            "sampled_frame_count": len(selected_frames),
            "decrypt_ok": decrypt_ok,
            "image_count": image_count,
            "decrypt_errors": [str(e)[:120] for e in decrypt_errors[:5]],
            "llm_called": llm_called,
            "llm_error": llm_error,
            "memory_context": {
                "identity_loaded": bool((gate_context.get("identity_card") or {}).get("agent_name")),
                "memory_count": len(gate_context.get("memory_set") or []),
                "passive_observation_count": len(gate_context.get("passive_observations") or []),
                "recent_fire_count": len(gate_context.get("recent_fires") or []),
                "connection_candidate_count": len(gate_context.get("connection_candidates") or []),
                "context_errors": gate_context.get("context_errors") or {},
            },
        },
        "forced": force,
    }


def _proactive_job_from_decision(decision: dict) -> dict:
    now = time.time()
    return {
        "job_id": _new_public_id("pj"),
        "ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(),
        "source": PROACTIVE_JOB_SOURCE,
        "gate_decision_id": decision.get("decision_id", ""),
        "status": "pending",
        "intent_label": decision.get("intent_label", ""),
        "context_hint": decision.get("context_hint", ""),
        "connections": decision.get("connections", []),
        "connection": decision.get("connection", {}),
        "frame_ids": decision.get("frame_ids", []),
        "device_event_ids": decision.get("device_event_ids", []),
        "current_app": decision.get("current_app", ""),
    }


def _proactive_debug_snapshot(store: UserStore) -> dict:
    # The debug dashboard is used as an investigation surface, not a tiny
    # status widget. Read enough rows to cover a normal day of proactive
    # activity; the renderer still lets callers cap visible sections via
    # query params, but the backing snapshot should not hide history first.
    decisions = store.list_gate_decisions(limit=PROACTIVE_DEBUG_DECISION_READ_MAX)
    jobs = store.list_proactive_jobs(limit=PROACTIVE_DEBUG_JOB_READ_MAX)
    events = store.list_device_events(limit=PROACTIVE_DEBUG_EVENT_READ_MAX)
    reviews = store.list_gate_reviews(limit=PROACTIVE_DEBUG_REVIEW_READ_MAX)
    latest_review_by_decision: dict[str, dict] = {}
    for review in reviews:
        did = str(review.get("decision_id") or "")
        if did:
            latest_review_by_decision[did] = review
    with store.chat_lock:
        proactive_messages = [
            {
                "id": m.get("id"),
                "ts": m.get("ts"),
                "source": m.get("source"),
                "gate_decision_id": m.get("gate_decision_id", ""),
                "proactive_job_id": m.get("proactive_job_id", ""),
                "content_type": m.get("content_type", "text"),
                "alert_preview": m.get("alert_preview", ""),
                "push_body_preview": m.get("push_body_preview", ""),
                "push_live_activity_requested": bool(m.get("push_live_activity_requested")),
                "live_activity_status": m.get("live_activity_status", ""),
                "live_activity_reason": m.get("live_activity_reason", ""),
                "live_activity_activity_id": m.get("live_activity_activity_id", ""),
                "live_activity_mode": m.get("live_activity_mode", ""),
                "alert_status": m.get("alert_status", ""),
                "alert_reason": m.get("alert_reason", ""),
            }
            for m in store.chat_messages
            if m.get("source") == PROACTIVE_JOB_SOURCE
        ][-PROACTIVE_DEBUG_MESSAGE_READ_MAX:]
    messages_by_job = {
        str(m.get("proactive_job_id") or ""): m
        for m in proactive_messages
        if m.get("proactive_job_id")
    }
    enriched_jobs: list[dict] = []
    for job in jobs:
        row = dict(job)
        msg = messages_by_job.get(str(job.get("job_id") or ""))
        if msg:
            live_status = str(msg.get("live_activity_status") or "")
            alert_status = str(msg.get("alert_status") or "")
            if live_status == "delivered" and alert_status in {"", "delivered", "logged_only"}:
                row["derived_status"] = "delivered"
            elif live_status:
                row["derived_status"] = f"chat_written_live_activity_{live_status}"
            else:
                row["derived_status"] = "chat_written"
            row["chat_message_id"] = msg.get("id", "")
            row["chat_ts"] = msg.get("ts")
            row["alert_status"] = alert_status
            row["alert_reason"] = msg.get("alert_reason", "")
            row["live_activity_status"] = live_status
            row["live_activity_reason"] = msg.get("live_activity_reason", "")
            row["live_activity_mode"] = msg.get("live_activity_mode", "")
            row["preview"] = msg.get("alert_preview") or msg.get("push_body_preview") or ""
        else:
            row["derived_status"] = row.get("status", "pending")
            row.setdefault("preview", "")
        enriched_jobs.append(row)
    with store.frames_lock:
        frames = [
            {
                "id": f.get("id"),
                "ts": f.get("ts"),
                "app": f.get("app") or "unknown",
                "ocr_len": len((f.get("ocr_text") or "").strip()),
                "encrypted": bool(f.get("encrypted")),
            }
            for f in store.frames_meta[-PROACTIVE_DEBUG_FRAME_READ_MAX:]
        ]
    return {
        "user_id": store.user_id,
        "generated_at": datetime.now().isoformat(),
        "settings": store.load_proactive_settings(),
        "decisions": decisions,
        "reviews": reviews,
        "latest_review_by_decision": latest_review_by_decision,
        "jobs": enriched_jobs,
        "device_events": events,
        "proactive_messages": proactive_messages,
        "recent_frames": frames,
        "counts": {
            "decisions": len(decisions),
            "reviews": len(reviews),
            "jobs": len(jobs),
            "device_events": len(events),
            "proactive_messages": len(proactive_messages),
            "recent_frames": len(frames),
        },
    }


def _gate_input_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _gate_decision_has_frame_context(decision: dict) -> bool:
    frame_ids = decision.get("frame_ids")
    if isinstance(frame_ids, list) and any(str(fid).strip() for fid in frame_ids):
        return True
    gate_input = _gate_input_dict(decision.get("gate_input"))
    for key in ("sampled_frame_count", "image_count", "ocr_chars"):
        try:
            if int(gate_input.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return bool(gate_input.get("decrypt_ok"))


def _debug_translation_candidate(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 8:
        return False
    # Machine enum values are handled by the fixed label dictionary. The
    # model translator is only for prose fields such as reason/context_hint.
    if re.fullmatch(r"[a-zA-Z0-9_:\-./ ]+", raw) and len(raw.split()) <= 4:
        return False
    return bool(re.search(r"[A-Za-z]", raw))


def _translate_debug_texts_to_zh(texts: list[str]) -> dict[str, str]:
    """Best-effort display-only translation for the debug dashboard.

    This never mutates Gate/job records. Raw English remains in JSON logs and
    folded payloads; translated strings are only used for HTML rendering when
    `lang=zh`.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for text in texts:
        raw = str(text or "").strip()
        if not raw or raw in seen or not _debug_translation_candidate(raw):
            continue
        seen.add(raw)
        unique.append(raw[:1800])

    if not unique:
        return {}

    with _debug_translation_lock:
        cached = {text: _debug_translation_cache[text] for text in unique if text in _debug_translation_cache}
    missing = [text for text in unique if text not in cached][:24]

    if missing and OPENROUTER_API_KEY and PROACTIVE_DEBUG_TRANSLATION_MODEL:
        try:
            payload = {
                "model": PROACTIVE_DEBUG_TRANSLATION_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Translate debug-dashboard prose from English to natural, concise Simplified Chinese "
                            "for a product debugging UI. "
                            "Preserve IDs, model names, JSON keys, product names, and technical terms like Gate, "
                            "context_hint, Live Activity, APNs, OCR. Do not preserve generic words like companion, "
                            "user, screen, response, or reason; translate them naturally. Return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "texts": missing,
                                "schema": {"translations": ["same length as texts"]},
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            }
            with httpx.Client(timeout=PROACTIVE_DEBUG_TRANSLATION_TIMEOUT_SEC) as client:
                resp = client.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(_strip_json_code_fence(content))
            translations = parsed.get("translations")
            if isinstance(translations, list):
                with _debug_translation_lock:
                    for raw, translated in zip(missing, translations):
                        out = str(translated or "").strip()
                        if out:
                            _debug_translation_cache[raw] = out[:1800]
                            cached[raw] = _debug_translation_cache[raw]
        except Exception as e:
            print(f"[proactive-debug] translation failed: {e}")

    return cached


def _render_proactive_dashboard(snapshot: dict) -> str:
    def esc(value) -> str:
        return html.escape(str(value if value is not None else ""))

    lang_param = str(request.args.get("lang") or "").strip().lower()
    if lang_param not in {"zh", "en"}:
        accept_lang = request.headers.get("Accept-Language", "").lower()
        lang_param = "zh" if "zh" in accept_lang else "en"
    is_zh = lang_param == "zh"

    def ui(en: str, zh: str) -> str:
        return zh if is_zh else en

    def lang_url(target: str) -> str:
        args = request.args.to_dict(flat=True)
        args["lang"] = target
        return f"/debug/proactive?{urlencode(args)}"

    def dashboard_url(**updates) -> str:
        args = request.args.to_dict(flat=True)
        for key, value in updates.items():
            if value is None:
                args.pop(key, None)
            else:
                args[key] = str(value)
        return f"/debug/proactive?{urlencode(args)}"

    def int_arg(name: str, default: int, lower: int, upper: int) -> int:
        try:
            value = int(str(request.args.get(name) or default).strip())
        except (TypeError, ValueError):
            value = default
        return max(lower, min(upper, value))

    decision_cap = int_arg("decision_limit", 80, 1, PROACTIVE_DEBUG_DECISION_READ_MAX)
    no_frame_cap = int_arg("no_frame_limit", 50, 1, PROACTIVE_DEBUG_DECISION_READ_MAX)
    job_cap = int_arg("job_limit", 100, 1, PROACTIVE_DEBUG_JOB_READ_MAX)
    table_cap = int_arg(
        "table_limit",
        100,
        1,
        max(
            PROACTIVE_DEBUG_EVENT_READ_MAX,
            PROACTIVE_DEBUG_MESSAGE_READ_MAX,
            PROACTIVE_DEBUG_FRAME_READ_MAX,
        ),
    )
    show_no_frame = str(request.args.get("show_no_frame") or "").strip().lower() in {"1", "true", "yes"}
    show_payloads = str(request.args.get("detail") or "1").strip().lower() not in {"0", "false", "no", "off"}

    debug_labels_zh = {
        "time": "时间",
        "ts": "时间戳",
        "model": "模型",
        "id": "判定 ID",
        "intent": "意图",
        "abstention": "不触发原因",
        "reason": "判定理由",
        "context_hint": "上下文提示",
        "frames": "屏幕帧",
        "frames sent": "已发送帧",
        "connection": "关联依据",
        "gate_input": "Gate 输入",
        "payload": "事件数据",
        "consumer": "消费服务",
        "decision": "Gate 判定",
        "job": "任务",
        "preview": "消息预览",
    }

    debug_value_labels_zh = {
        "TRUE": "触发",
        "FALSE": "不触发",
        "true": "触发",
        "false": "不触发",
        "pending": "等待处理",
        "claimed": "处理中",
        "completed": "已完成",
        "delivered": "已送达",
        "chat_written": "已写入聊天",
        "logged_only": "仅记录",
        "skipped": "已跳过",
        "failed": "失败",
        "error": "错误",
        "unreviewed": "未标注",
        "correct_true": "正确触发",
        "correct_false": "正确不触发",
        "missed_opportunity": "漏掉机会",
        "spam": "打扰/垃圾",
        "weak_connection": "关联太弱",
        "repeated": "重复触发",
        "privacy_bad": "隐私不合适",
        "great_companion_moment": "很好的陪伴时机",
        "blocked_before_model": "模型前拦截",
        "reviewable_false": "可复查的不触发",
        "manual_proactive_test": "手动主动触发测试",
        "research_pause": "研究停顿",
        "proactive_screen_context": "屏幕上下文",
        "manual_hint": "手动提示",
        "already_responded": "已经回应过",
        "shared_build_reflection": "共享构建反思",
        "no_recent_frames": "最近没有屏幕帧",
        "no_recent_frames_unit_test": "最近没有屏幕帧（测试）",
        "recent_proactive_fire": "10 分钟内已经主动触发过",
        "proactive_disabled": "主动触发已关闭",
        "dnd_enabled": "勿扰模式开启",
        "frame_decrypt_unavailable": "屏幕帧无法解密",
        "memory_context_unavailable": "记忆/身份上下文不可用",
        "model_not_configured": "Gate 模型未配置",
        "model_false": "模型判断不触发",
        "llm_false": "模型判断不触发",
        "llm_true": "模型判断触发",
        "llm_non_object": "模型返回不是 JSON 对象",
        "llm_missing_context_hint": "模型缺少上下文提示",
        "llm_missing_concrete_connection": "模型缺少具体关联",
        "llm_unrecognized_connection": "模型给出的关联无法验证",
        "invalid_gate_response": "Gate 返回无效",
        "has_connection": "存在具体关联",
        "model_detected_helpful_moment": "模型发现可帮助时机",
        "model_detected_memory_connection": "模型发现记忆关联",
        "agent_call_failed": "调用用户 Agent 失败",
    }

    def tr_label(value) -> str:
        raw = str(value or "")
        return debug_labels_zh.get(raw, raw) if is_zh else raw

    def tr_value(value) -> str:
        raw = str(value if value is not None else "")
        return debug_value_labels_zh.get(raw, raw) if is_zh else raw

    def value_html(value) -> str:
        raw = str(value if value is not None else "")
        translated = tr_value(raw)
        if translated != raw:
            return f"<span title='{esc(raw)}'>{esc(translated)}</span>"
        return esc(raw)

    def status_detail_html(status, reason) -> str:
        html = value_html(status)
        reason_text = str(reason or "").strip()
        if reason_text:
            html += f"<div class='mono mini'>{esc(reason_text[:180])}</div>"
        return html

    api_key = (request.args.get("key") or "").strip()
    key_qs = f"?key={quote(api_key)}" if api_key else ""
    settings = snapshot.get("settings") or {}
    dashboard_tz_name = str(
        request.args.get("tz")
        or settings.get("timezone")
        or PROACTIVE_DEFAULT_TIMEZONE
    ).strip() or "UTC"
    dashboard_tz = _safe_zoneinfo(dashboard_tz_name)

    def fmt_time(ts_value) -> str:
        try:
            ts = float(ts_value or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts, dashboard_tz).strftime("%Y-%m-%d %H:%M:%S")

    def fmt_epoch(ts_value) -> str:
        try:
            return str(round(float(ts_value or 0), 3))
        except (TypeError, ValueError):
            return ""

    def frame_links(frame_ids) -> str:
        ids = [str(fid).strip() for fid in (frame_ids or []) if str(fid).strip()]
        if not ids:
            return ""
        links = []
        for fid in ids:
            safe = quote(fid)
            label = esc(fid[:10])
            links.append(
                f"<a class='mono' href='/v1/screen/frames/{safe}/image{key_qs}' target='_blank'>{label}</a>"
                f"<a class='mini' href='/v1/screen/frames/{safe}/decrypt{key_qs}{'&' if key_qs else '?'}include_image=false' target='_blank'>{esc(ui('json', '解密 JSON'))}</a>"
            )
        return " ".join(links)

    def status_class(value) -> str:
        text = str(value or "").lower()
        if text in {"true", "delivered", "chat_written", "logged_only"} or text.startswith("chat_written"):
            return "ok"
        if text in {"pending", "false", "skipped"}:
            return "muted"
        if "error" in text or "failed" in text:
            return "bad"
        return ""

    decisions = list(reversed(snapshot.get("decisions") or []))
    frame_decisions = [d for d in decisions if _gate_decision_has_frame_context(d)]
    no_frame_decisions = [d for d in decisions if not _gate_decision_has_frame_context(d)]
    latest_reviews = snapshot.get("latest_review_by_decision") or {}
    jobs = list(reversed(snapshot.get("jobs") or []))
    messages = list(reversed(snapshot.get("proactive_messages") or []))
    frames = list(reversed(snapshot.get("recent_frames") or []))
    events = list(reversed(snapshot.get("device_events") or []))
    translation_map: dict[str, str] = {}
    if is_zh:
        translation_candidates: list[str] = []
        translated_decisions = frame_decisions[:decision_cap]
        if show_no_frame:
            translated_decisions += no_frame_decisions[:no_frame_cap]
        for d in translated_decisions:
            translation_candidates.extend([
                str(d.get("reason") or ""),
                str(d.get("abstention_reason") or ""),
                str(d.get("context_hint") or ""),
            ])
        for j in jobs[:job_cap]:
            translation_candidates.extend([
                str(j.get("context_hint") or ""),
                str(j.get("status_reason") or ""),
                str(j.get("preview") or ""),
            ])
        for m in messages[:table_cap]:
            translation_candidates.append(
                str(m.get("alert_preview") or m.get("push_body_preview") or "")
            )
        translation_map = _translate_debug_texts_to_zh(translation_candidates)

    def prose_html(value) -> str:
        raw = str(value if value is not None else "").strip()
        if is_zh and raw in translation_map:
            return f"<span title='{esc(raw)}'>{esc(translation_map[raw])}</span>"
        return esc(raw)

    def prose_or_value_html(value) -> str:
        raw = str(value if value is not None else "")
        return prose_html(raw) if tr_value(raw) == raw else value_html(raw)

    def short_id(value, head: int = 8) -> str:
        """Truncate long IDs for display; full value shown on hover via title attr."""
        s = str(value or "").strip()
        if len(s) <= head + 2:
            return esc(s)
        return f"<span class='mono trunc' title='{esc(s)}'>{esc(s[:head])}…</span>"

    def fold_json(label: str, payload) -> str:
        """Collapse JSON payloads behind a <details> summary.

        Production debug pages can accumulate large Gate inputs quickly. Keep
        the dashboard response bounded so browsers do not receive a truncated
        HTML document from the edge path.
        """
        try:
            pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            pretty = str(payload or "")
        if not pretty.strip() or pretty.strip() in ("{}", "null", "[]"):
            return f"<span class='muted mini'>{esc(tr_label(label))}: ∅</span>"
        max_chars = 500 if show_payloads else 180
        if len(pretty) > max_chars:
            pretty = pretty[:max_chars].rstrip() + "\n… truncated"
        return (
            f"<details class='inline-json'><summary>{esc(tr_label(label))}</summary>"
            f"<pre class='mono'>{esc(pretty)}</pre></details>"
        )

    def section_limit_link(
        param: str,
        current: int,
        total: int,
        step: int,
        label_en: str,
        label_zh: str,
        extra_updates: dict | None = None,
    ) -> str:
        if total <= current:
            return ""
        updates = dict(extra_updates or {})
        updates[param] = min(total, current + step)
        return f"<a class='control-link' href='{esc(dashboard_url(**updates))}'>{esc(ui(label_en, label_zh))}</a>"

    # Gate Decisions are the heaviest rendering on this page (13 columns of
    # mixed text + JSON + form). Converted from a wide table to a stack of
    # cards: each decision is one block, fields laid out in a 2-column grid
    # that wraps to single-column on narrow viewports. JSON payloads
    # (connection, gate_input) collapse behind <details>. Same data, same
    # density, no horizontal scroll.
    def decision_card(d) -> str:
        verdict = ui("TRUE", "触发") if d.get("should_reach_out") else ui("FALSE", "不触发")
        verdict_cls = "ok" if d.get("should_reach_out") else "muted"
        gate_input = _gate_input_dict(d.get("gate_input"))
        connection = d.get("connection") or {}
        review = latest_reviews.get(str(d.get("decision_id") or "")) or {}
        decision_id = str(d.get("decision_id") or "")
        review_action = f"/v1/proactive/decisions/{quote(decision_id)}/review{key_qs}"
        frame_links_html = frame_links(d.get("frame_ids"))
        intent = d.get("intent_label") or ""
        abstention = d.get("abstention_reason") or ""
        context_hint = d.get("context_hint") or ""
        reason = d.get("reason") or ""

        meta_bits = [
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('time'))}</span> {esc(fmt_time(d.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('ts'))}</span> {esc(fmt_epoch(d.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('model'))}</span> {esc(d.get('gate_model'))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('id'))}</span> {short_id(decision_id)}</span>",
        ]
        if intent:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('intent'))}</span> {value_html(intent)}</span>")
        if abstention:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('abstention'))}</span> {prose_or_value_html(abstention)}</span>")

        body_blocks = []
        if reason:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('reason'))}</span><div class='block-text'>{prose_or_value_html(reason)}</div></div>")
        if context_hint:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('context_hint'))}</span><div class='block-text'>{prose_html(context_hint)}</div></div>")
        if frame_links_html:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('frames'))}</span><div class='block-text'>{frame_links_html}</div></div>")
        if show_payloads:
            body_blocks.append(f"<div class='block'>{fold_json('connection', connection)}</div>")
            body_blocks.append(f"<div class='block'>{fold_json('gate_input', gate_input)}</div>")

        review_html = (
            f"<div class='review'>"
            f"<div class='mini'>{esc(ui('last review', '最近标注'))}: <span class='{ 'ok' if review.get('label') == 'correct_true' else '' }'>{value_html(review.get('label') or 'unreviewed')}</span></div>"
            f"<form method='post' action='{review_action}'>"
            "<select name='label'>"
            f"<option value='correct_true'>{esc(ui('correct true', '正确触发'))}</option>"
            f"<option value='correct_false'>{esc(ui('correct false', '正确不触发'))}</option>"
            f"<option value='missed_opportunity'>{esc(ui('missed opportunity', '漏掉机会'))}</option>"
            f"<option value='spam'>{esc(ui('spam', '打扰/垃圾'))}</option>"
            f"<option value='weak_connection'>{esc(ui('weak connection', '关联太弱'))}</option>"
            f"<option value='repeated'>{esc(ui('repeated', '重复触发'))}</option>"
            f"<option value='privacy_bad'>{esc(ui('privacy bad', '隐私不合适'))}</option>"
            f"<option value='great_companion_moment'>{esc(ui('great companion moment', '很好的陪伴时机'))}</option>"
            "</select>"
            f"<input name='notes' placeholder='{esc(ui('notes', '标注备注'))}' maxlength='300'>"
            f"<button type='submit'>{esc(ui('save', '保存'))}</button>"
            "</form>"
            "</div>"
        )

        return (
            f"<article class='card decision-card'>"
            f"  <header class='card-head'>"
            f"    <span class='verdict {verdict_cls}'>{verdict}</span>"
            f"    <div class='meta-bits'>{''.join(meta_bits)}</div>"
            f"  </header>"
            f"  <div class='card-body'>{''.join(body_blocks)}</div>"
            f"  {review_html}"
            f"</article>"
        )

    def decision_section(rows_source, empty_text: str, limit: int = decision_cap) -> str:
        if not rows_source:
            return f"<div class='empty'>{esc(empty_text)}</div>"
        return "<div class='card-list'>" + "".join(decision_card(d) for d in rows_source[:limit]) + "</div>"

    hidden_gate_details = ""
    if no_frame_decisions:
        if show_no_frame:
            hidden_body = decision_section(
                no_frame_decisions,
                ui("No hidden no-frame Gate ticks.", "没有隐藏的无屏幕帧 Gate 空 tick。"),
                limit=no_frame_cap,
            )
            more_no_frame = section_limit_link(
                "no_frame_limit",
                no_frame_cap,
                len(no_frame_decisions),
                50,
                "show more no-frame ticks",
                "显示更多空 tick",
                {"show_no_frame": 1},
            )
            hidden_hint = (
                f"<div class='hint'>{esc(ui('Showing no-frame ticks with a high cap; increase it if you need older scheduler history.', '正在显示无屏幕帧空 tick；需要更早的定时历史可以继续增加上限。'))} "
                f"{more_no_frame} "
                f"<a href='{esc(dashboard_url(show_no_frame=None))}'>{esc(ui('hide no-frame ticks', '隐藏空 tick 明细'))}</a></div>"
            )
        else:
            hidden_body = (
                f"<div class='empty'>{esc(ui('No-frame Gate ticks are folded to keep this page lightweight.', '无屏幕帧 Gate 空 tick 已折叠，以保持页面轻量。'))} "
                f"<a href='{esc(dashboard_url(show_no_frame=1))}'>{esc(ui('show sample', '显示样本'))}</a></div>"
            )
            hidden_hint = ""
        hidden_gate_details = (
            "<details class='debug-details'>"
            f"<summary>{esc(ui(f'Show hidden no-frame Gate ticks ({len(no_frame_decisions)})', f'显示隐藏的无屏幕帧 Gate 空 tick（{len(no_frame_decisions)}）'))}</summary>"
            + hidden_hint
            + hidden_body
            + "</details>"
        )

    # Hidden Jobs — same card pattern as Gate Decisions. Fewer JSON blobs
    # so cards render lighter, but the wide horizontal table is the
    # bigger problem; cards solve it the same way.
    def job_card(j) -> str:
        status = j.get("derived_status") or j.get("status", "pending")
        intent = j.get("intent_label") or ""
        meta_bits = [
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('time'))}</span> {esc(fmt_time(j.get('ts')))}</span>",
            f"<span class='meta-bit mono'><span class='label'>{esc(tr_label('ts'))}</span> {esc(fmt_epoch(j.get('ts')))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('job'))}</span> {short_id(j.get('job_id'))}</span>",
            f"<span class='meta-bit'><span class='label'>{esc(tr_label('decision'))}</span> {short_id(j.get('gate_decision_id'))}</span>",
        ]
        if j.get("consumer_id"):
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('consumer'))}</span> {short_id(j.get('consumer_id'))}</span>")
        if intent:
            meta_bits.append(f"<span class='meta-bit'><span class='label'>{esc(tr_label('intent'))}</span> {value_html(intent)}</span>")

        body_blocks = []
        if j.get("context_hint"):
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('context_hint'))}</span><div class='block-text'>{prose_html(j.get('context_hint'))}</div></div>")
        if j.get("status_reason"):
            status_reason = j.get("status_reason")
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('reason'))}</span><div class='block-text'>{prose_or_value_html(status_reason)}</div></div>")
        if j.get("preview"):
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('preview'))}</span><div class='block-text'>{prose_html(j.get('preview'))}</div></div>")
        frames_sent = frame_links(j.get("frame_ids"))
        if frames_sent:
            body_blocks.append(f"<div class='block'><span class='block-label'>{esc(tr_label('frames sent'))}</span><div class='block-text'>{frames_sent}</div></div>")

        status_pill = f"<span class='verdict {status_class(status)}'>{value_html(status)}</span>"
        chips = []
        if j.get("alert_status"):
            chips.append(
                f"<span class='chip {status_class(j.get('alert_status'))}'>{esc(ui('alert', '通知'))}: "
                f"{status_detail_html(j.get('alert_status'), j.get('alert_reason'))}</span>"
            )
        if j.get("live_activity_status"):
            live_detail = status_detail_html(j.get("live_activity_status"), j.get("live_activity_reason"))
            if j.get("live_activity_mode"):
                live_detail += f" <span class='mono mini'>{esc(j.get('live_activity_mode'))}</span>"
            chips.append(
                f"<span class='chip {status_class(j.get('live_activity_status'))}'>Live Activity: "
                f"{live_detail}</span>"
            )

        chips_html = f"<div class='chip-row'>{''.join(chips)}</div>" if chips else ""

        return (
            f"<article class='card'>"
            f"  <header class='card-head'>{status_pill}<div class='meta-bits'>{''.join(meta_bits)}</div></header>"
            f"  {chips_html}"
            f"  <div class='card-body'>{''.join(body_blocks)}</div>"
            f"</article>"
        )

    def job_section() -> str:
        if not jobs:
            return f"<div class='empty'>{esc(ui('No hidden proactive jobs yet.', '还没有隐藏主动任务。'))}</div>"
        return "<div class='card-list'>" + "".join(job_card(j) for j in jobs[:job_cap]) + "</div>"

    # The remaining three sections (chat writes / frames / events) have
    # fewer columns; tables still fit a reasonable max-width with
    # `table-layout: fixed` + the new `.table-scroll` wrapper for the
    # rare overflow case. No card conversion needed.
    def message_rows() -> str:
        if not messages:
            return f"<tr><td colspan='8'>{esc(ui('No proactive chat writes yet.', '还没有主动消息写入。'))}</td></tr>"
        rows = []
        for m in messages[:table_cap]:
            preview = m.get("alert_preview") or m.get("push_body_preview") or ui("(encrypted envelope; no plaintext preview recorded)", "（加密 envelope；没有记录明文预览）")
            live_detail = status_detail_html(m.get("live_activity_status"), m.get("live_activity_reason"))
            if m.get("live_activity_mode"):
                live_detail += f"<div class='mono mini'>{esc(m.get('live_activity_mode'))}</div>"
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(m.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(m.get('ts')))}</div></td>"
                f"<td>{esc(m.get('content_type'))}</td>"
                f"<td class='wrap'>{prose_html(preview)}</td>"
                f"<td class='{status_class(m.get('alert_status'))}'>{status_detail_html(m.get('alert_status'), m.get('alert_reason'))}</td>"
                f"<td class='{status_class(m.get('live_activity_status'))}'>{live_detail}</td>"
                f"<td>{short_id(m.get('gate_decision_id'))}</td>"
                f"<td>{short_id(m.get('proactive_job_id'))}</td>"
                f"<td>{short_id(m.get('id'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    def frame_rows() -> str:
        if not frames:
            return f"<tr><td colspan='5'>{esc(ui('No frames indexed.', '还没有索引到屏幕帧。'))}</td></tr>"
        rows = []
        for f in frames[:table_cap]:
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(f.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(f.get('ts')))}</div></td>"
                f"<td>{esc(f.get('app'))}</td>"
                f"<td>{esc(f.get('ocr_len'))}</td>"
                f"<td>{esc(f.get('encrypted'))}</td>"
                f"<td>{frame_links([f.get('id')])}</td>"
                "</tr>"
            )
        return "".join(rows)

    def event_rows() -> str:
        if not events:
            return f"<tr><td colspan='4'>{esc(ui('No device events yet.', '还没有设备事件。'))}</td></tr>"
        rows = []
        for e in events[:table_cap]:
            rows.append(
                "<tr>"
                f"<td>{esc(fmt_time(e.get('ts')))}<div class='mono mini'>{esc(fmt_epoch(e.get('ts')))}</div></td>"
                f"<td>{esc(e.get('source'))}</td>"
                f"<td>{esc(e.get('type'))}</td>"
                f"<td>{fold_json('payload', e.get('payload') or {}) if show_payloads else short_id((e.get('payload') or {}).get('id') or e.get('id') or e.get('type'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    counts = snapshot.get("counts") or {}
    visible_gate_count = len(frame_decisions)
    hidden_no_frame_count = len(no_frame_decisions)
    page_title = "IO Proactive Harness"
    visible_empty_text = ui(
        f"No Gate decisions with screen context yet. Hidden no-frame ticks: {hidden_no_frame_count}.",
        f"还没有带屏幕上下文的 Gate 判定。隐藏空 tick：{hidden_no_frame_count}。",
    )
    detail_toggle = (
        f"<a class='control-link' href='{esc(dashboard_url(detail=0))}'>{esc(ui('hide JSON detail', '隐藏 JSON 详情'))}</a>"
        if show_payloads
        else f"<a class='control-link' href='{esc(dashboard_url(detail=1))}'>{esc(ui('show JSON detail', '显示 JSON 详情'))}</a>"
    )
    control_links = " ".join(
        link for link in [
            detail_toggle,
            section_limit_link(
                "decision_limit",
                decision_cap,
                len(frame_decisions),
                80,
                "show more Gate decisions",
                "显示更多 Gate 判定",
            ),
            section_limit_link(
                "job_limit",
                job_cap,
                len(jobs),
                100,
                "show more completed jobs",
                "显示更多已完成任务",
            ),
            section_limit_link(
                "table_limit",
                table_cap,
                max(len(messages), len(frames), len(events)),
                100,
                "show more tables",
                "显示更多表格记录",
            ),
            (
                f"<a class='control-link' href='{esc(dashboard_url(show_no_frame=1))}'>{esc(ui('show no-frame ticks', '显示空 tick'))}</a>"
                if no_frame_decisions and not show_no_frame
                else ""
            ),
        ]
        if link
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(page_title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0 auto;
      padding: 24px;
      max-width: 1240px;
      color: #1f1d1a;
      background: #f6f0e6;
      line-height: 1.4;
    }}
    h1 {{ margin: 0 0 4px; }}
    .topbar {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .lang-switch {{
      display: inline-flex;
      gap: 4px;
      border: 1px solid #c9bfb2;
      background: #fffaf1;
      padding: 3px;
      border-radius: 2px;
      flex: 0 0 auto;
    }}
    .lang-switch a {{
      display: inline-block;
      padding: 4px 8px;
      border: 0;
      color: #6f6961;
      font-size: 12px;
    }}
    .lang-switch a.active {{
      background: #8e301f;
      color: #fffaf1;
    }}
    @media (max-width: 640px) {{
      .topbar {{ flex-direction: column; }}
    }}
    h2 {{
      margin-top: 32px;
      border-top: 1px solid #d8d0c4;
      padding-top: 18px;
      font-size: 18px;
    }}
    .meta {{ color: #6f6961; margin-bottom: 16px; font-size: 13px; }}
    .pill {{
      display: inline-block;
      border: 1px solid #c9bfb2;
      padding: 4px 8px;
      margin: 2px;
      background: #fffaf1;
      font-size: 12px;
      border-radius: 2px;
    }}
    .hint {{ margin: 8px 0 16px; color: #6f6961; font-size: 13px; }}
    .control-link {{ display: inline-block; margin-left: 8px; white-space: nowrap; }}
    .empty {{
      padding: 16px;
      background: #fffaf1;
      border: 1px solid #ddd2c5;
      color: #6f6961;
      font-style: italic;
    }}

    /* ---- Card layout (Gate Decisions + Hidden Jobs) ---- */
    .card-list {{ display: flex; flex-direction: column; gap: 12px; }}
    .card {{
      background: #fffaf1;
      border: 1px solid #ddd2c5;
      padding: 14px 16px;
      border-radius: 2px;
    }}
    .card-head {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 10px;
      padding-bottom: 10px;
      border-bottom: 1px solid #eee5d5;
    }}
    .verdict {{
      display: inline-block;
      padding: 4px 10px;
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.5px;
      background: #efe5d7;
      border-radius: 2px;
      white-space: nowrap;
    }}
    .verdict.ok    {{ background: #d4ead8; color: #0b7d42; }}
    .verdict.muted {{ background: #ebe4d4; color: #8b8176; }}
    .verdict.bad   {{ background: #f5d8d4; color: #b42318; }}
    .meta-bits {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 18px;
      flex: 1;
      align-items: baseline;
      font-size: 12px;
    }}
    .meta-bit {{ color: #1f1d1a; }}
    .meta-bit .label {{
      color: #8b8176;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      margin-right: 4px;
    }}
    .chip-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
    .chip {{
      display: inline-block;
      padding: 2px 8px;
      background: #efe5d7;
      font-size: 11px;
      border-radius: 2px;
    }}
    .chip.ok    {{ background: #d4ead8; color: #0b7d42; }}
    .chip.muted {{ background: #ebe4d4; color: #8b8176; }}
    .chip.bad   {{ background: #f5d8d4; color: #b42318; }}
    .card-body {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px 24px;
    }}
    @media (max-width: 720px) {{
      .card-body {{ grid-template-columns: 1fr; }}
    }}
    .block {{ min-width: 0; }}
    .block-label {{
      display: block;
      color: #8b8176;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      margin-bottom: 3px;
    }}
    .block-text {{
      font-size: 13px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}
    .review {{
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px solid #eee5d5;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      font-size: 12px;
    }}
    .review .mini {{ margin-right: 6px; color: #6f6961; }}
    .review form {{ display: inline-flex; gap: 6px; flex-wrap: wrap; }}
    .review select, .review input, .review button {{ font: inherit; font-size: 12px; }}
    .review input {{ width: 160px; }}

    /* ---- Inline JSON disclosure (used inside cards + small tables) ---- */
    details.inline-json {{ display: block; }}
    details.inline-json > summary {{
      cursor: pointer;
      color: #8e301f;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-size: 10px;
      list-style: revert;
    }}
    details.inline-json[open] > summary {{ margin-bottom: 4px; }}
    details.inline-json pre {{
      margin: 0;
      padding: 8px;
      background: #f0e8d8;
      overflow-x: auto;
      max-height: 280px;
      font-size: 11px;
      border-radius: 2px;
    }}

    /* ---- Tables (Chat Writes / Frames / Events) ---- */
    .table-scroll {{
      max-width: 100%;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      border: 1px solid #ddd2c5;
      background: #fffaf1;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #eee5d5;
      padding: 8px 10px;
      vertical-align: top;
      text-align: left;
      overflow-wrap: anywhere;
    }}
    th {{
      background: #efe5d7;
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #5a544b;
    }}
    tr:last-child td {{ border-bottom: none; }}
    .wrap {{ white-space: pre-wrap; }}
    .mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11.5px;
    }}
    .mini {{ font-size: 10.5px; color: #8b8176; }}
    .trunc {{ cursor: help; border-bottom: 1px dotted #b8a895; }}

    /* Per-table column widths */
    .t-messages col.c-time   {{ width: 13%; }}
    .t-messages col.c-type   {{ width: 8%; }}
    .t-messages col.c-prev   {{ width: 33%; }}
    .t-messages col.c-status {{ width: 9%; }}
    .t-messages col.c-id     {{ width: 9%; }}
    .t-frames   col.c-time   {{ width: 18%; }}
    .t-frames   col.c-app    {{ width: 22%; }}
    .t-frames   col.c-num    {{ width: 12%; }}
    .t-frames   col.c-link   {{ width: 32%; }}
    .t-events   col.c-time   {{ width: 16%; }}
    .t-events   col.c-source {{ width: 14%; }}
    .t-events   col.c-type   {{ width: 18%; }}
    .t-events   col.c-payload{{ width: 52%; }}

    a {{ color: #8e301f; text-decoration: none; border-bottom: 1px solid #d0a094; }}
    a.mini {{ margin-left: 6px; font-size: 11px; color: #6f6961; }}
    .ok    {{ color: #0b7d42; font-weight: 600; }}
    .muted {{ color: #8b8176; }}
    .bad   {{ color: #b42318; font-weight: 600; }}
    details.debug-details {{ margin-top: 14px; }}
    details.debug-details > summary {{ cursor: pointer; color: #8e301f; margin-bottom: 8px; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>{esc(page_title)}</h1>
      <div class="meta">{esc(ui('user', '用户'))} <span class="mono">{esc(snapshot.get('user_id'))}</span> · {esc(ui('generated', '生成时间'))} {esc(snapshot.get('generated_at'))} · {esc(ui('times shown in', '页面时间按'))} {esc(dashboard_tz_name)} {esc(ui('', '显示'))} · {esc(ui('auto-refresh 5s', '每 5 秒自动刷新'))}</div>
    </div>
    <nav class="lang-switch" aria-label="language">
      <a class="{'active' if not is_zh else ''}" href="{esc(lang_url('en'))}">English</a>
      <a class="{'active' if is_zh else ''}" href="{esc(lang_url('zh'))}">中文</a>
    </nav>
  </div>
  <div>
    <span class="pill">{esc(ui('all decisions', '全部判定'))} {esc(counts.get('decisions', 0))}</span>
    <span class="pill">{esc(ui('visible decisions', '主表判定'))} {esc(visible_gate_count)}</span>
    <span class="pill">{esc(ui('hidden no-frame ticks', '隐藏空 tick'))} {esc(hidden_no_frame_count)}</span>
    <span class="pill">{esc(ui('human reviews', '人工标注'))} {esc(counts.get('reviews', 0))}</span>
    <span class="pill">{esc(ui('hidden jobs', '隐藏任务'))} {esc(counts.get('jobs', 0))}</span>
    <span class="pill">{esc(ui('proactive writes', '主动写入'))} {esc(counts.get('proactive_messages', 0))}</span>
    <span class="pill">{esc(ui('screen frames', '屏幕帧'))} {esc(counts.get('recent_frames', 0))}</span>
    <span class="pill">{esc(ui('device events', '设备事件'))} {esc(counts.get('device_events', 0))}</span>
  </div>
  <div class="hint">
    {esc(ui('Full debug mode is on by default. The page reads deeper history and caps only the rendered sections; use the links to expand further or hide JSON payloads.', '默认已恢复完整调试模式。页面会读取更深的历史，只限制渲染条数；可以用下面链接继续展开或隐藏 JSON。'))}
    {control_links}
  </div>

  <h2>{esc(ui('Gate Decisions', 'Gate 判定'))}</h2>
  <div class="hint">{esc(ui('The main list only shows Gate decisions backed by screen frames, OCR, or image context. Scheduled no-frame ticks are folded below.', '主表只显示带屏幕帧、OCR 或图片上下文的 Gate 判定。没有屏幕帧的定时空 tick 会折叠在下方。'))}</div>
  {decision_section(frame_decisions, visible_empty_text, limit=decision_cap)}
  {hidden_gate_details}

  <h2>{esc(ui('Hidden Jobs', '隐藏任务'))}</h2>
  {job_section()}

  <h2>{esc(ui('Proactive Chat Writes', '主动消息写入'))}</h2>
  <div class="table-scroll">
    <table class="t-messages">
      <colgroup>
        <col class="c-time"><col class="c-type"><col class="c-prev">
        <col class="c-status"><col class="c-status">
        <col class="c-id"><col class="c-id"><col class="c-id">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>{esc(ui('type', '类型'))}</th><th>{esc(ui('preview', '预览'))}</th><th>{esc(ui('alert', '系统通知'))}</th><th>Live Activity</th><th>{esc(ui('decision', '判定'))}</th><th>{esc(ui('job', '任务'))}</th><th>{esc(ui('message', '消息'))}</th></tr></thead>
      <tbody>{message_rows()}</tbody>
    </table>
  </div>

  <h2>{esc(ui('Recent Screen Frames', '最近屏幕帧'))}</h2>
  <div class="table-scroll">
    <table class="t-frames">
      <colgroup>
        <col class="c-time"><col class="c-app"><col class="c-num"><col class="c-num"><col class="c-link">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>App</th><th>{esc(ui('OCR length', 'OCR 长度'))}</th><th>{esc(ui('encrypted', '已加密'))}</th><th>{esc(ui('frame', '屏幕帧'))}</th></tr></thead>
      <tbody>{frame_rows()}</tbody>
    </table>
  </div>

  <h2>{esc(ui('Device Events', '设备事件'))}</h2>
  <div class="table-scroll">
    <table class="t-events">
      <colgroup>
        <col class="c-time"><col class="c-source"><col class="c-type"><col class="c-payload">
      </colgroup>
      <thead><tr><th>{esc(ui('time', '时间'))}</th><th>{esc(ui('source', '来源'))}</th><th>{esc(ui('type', '类型'))}</th><th>{esc(ui('payload', '事件数据'))}</th></tr></thead>
      <tbody>{event_rows()}</tbody>
    </table>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebSocket ingest server
# ---------------------------------------------------------------------------

WS_PORT = int(os.environ.get("FEEDLING_WS_PORT", 9998))


def _resolve_ws_user(websocket) -> str | None:
    """Resolve user from WS connection. Returns user_id, or None on auth failure.

    Reads ?key=... from the path, or "Bearer ..." from the Authorization
    header (whichever arrives first)."""
    # websockets lib v12+ uses websocket.request.path and .headers
    path = getattr(websocket, "path", "") or ""
    key = None
    if "?" in path:
        try:
            q = parse_qs(urlparse(path).query)
            k = q.get("key", [""])[0].strip()
            if k:
                key = k
        except Exception:
            pass

    if not key:
        # websockets>=10 exposes headers via .request_headers or .request.headers
        headers = getattr(websocket, "request_headers", None) or getattr(
            getattr(websocket, "request", None), "headers", {}
        )
        auth = ""
        try:
            auth = headers.get("Authorization", "")
        except Exception:
            try:
                auth = headers["Authorization"]
            except Exception:
                auth = ""
        if auth and auth.lower().startswith("bearer "):
            key = auth[7:].strip()

    if not key:
        return None
    return _resolve_user(key)


async def _ws_handler(websocket):
    try:
        user_id = _resolve_ws_user(websocket)
    except Exception as e:
        print(f"[ws] auth error: {e}")
        await websocket.close(code=4401, reason="unauthorized")
        return
    if not user_id:
        print("[ws] rejected: no valid key")
        await websocket.close(code=4401, reason="unauthorized")
        return

    store = get_store(user_id)
    print(f"[ws] client connected user={user_id} peer={websocket.remote_address}")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "frame":
                    threading.Thread(target=_save_frame, args=(store, data), daemon=True).start()
            except Exception as e:
                print(f"[ws:{user_id}] parse error: {e}")
    except websockets.exceptions.ConnectionClosed:
        pass
    print(f"[ws:{user_id}] client disconnected")


async def _ws_main():
    try:
        async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
            print(f"[ws] WebSocket ingest server running on ws://0.0.0.0:{WS_PORT}/ingest")
            await asyncio.Future()
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"[ws] WARNING: port {WS_PORT} already in use — WebSocket ingest disabled, HTTP continues")
        else:
            raise


def _run_ws_server():
    asyncio.run(_ws_main())


threading.Thread(target=_run_ws_server, daemon=True).start()

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Per-request access log
# ---------------------------------------------------------------------------
# gunicorn (production) does not emit Flask's old dev-server access line, so
# without this the only per-request visibility in `phala cvms logs` is whatever
# individual handlers happen to print. This logs ONE structured line per
# request with the server-side handler time (dur_ms) and the on-the-wire
# response size + encoding — enough to tell "backend slow" from "network slow"
# and to spot oversized responses straight from the CVM logs. Registered
# before Compress(app) so it runs AFTER compression and records the final
# Content-Length / Content-Encoding.
@app.before_request
def _access_log_start():
    g._req_start = time.monotonic()


@app.after_request
def _access_log_end(response):
    # /healthz is hit constantly by the HAProxy/uptime probe — skip the noise.
    if request.path == "/healthz":
        return response
    start = getattr(g, "_req_start", None)
    dur_ms = int((time.monotonic() - start) * 1000) if start is not None else -1
    uid = getattr(g, "user_id", "-")
    clen = response.headers.get("Content-Length", "?")
    enc = response.headers.get("Content-Encoding", "-")
    # Keep the query string (limit/since/include_image_body — the useful bits)
    # but REDACT the `key` param: _extract_api_key() accepts `?key=` as an auth
    # method, so the URL can carry a live API key that must never reach the logs.
    if request.args.get("key"):
        pairs = [
            (k, "REDACTED" if k.lower() == "key" else v)
            for k, v in request.args.items(multi=True)
        ]
        path = f"{request.path}?{urlencode(pairs)}"
    else:
        path = request.full_path.rstrip("?")
    print(
        f"[req] uid={uid} {request.method} {path} status={response.status_code} "
        f"bytes={clen} enc={enc} dur_ms={dur_ms}",
        flush=True,
    )
    return response


# gzip responses when the client sends Accept-Encoding: gzip. CVM egress
# throughput is ~30-50 KB/s for large payloads; the decrypt-with-image
# path was shipping 470 KB of JSON per call, and dropping that in half
# via compression is a 3-5x latency win for "show me the screen" calls.
Compress(app)

# ---------------------------------------------------------------------------
# APNs config (global — one Apple dev key for the app)
# ---------------------------------------------------------------------------

TEAM_ID = os.environ.get("APNS_TEAM_ID", "").strip() or "DC9JH5DRMY"
KEY_ID = os.environ.get("APNS_KEY_ID", "").strip() or "5TH55X5U7T"
BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "").strip() or "com.feedling.mcp"
APNS_SANDBOX = os.environ.get("APNS_SANDBOX", "true").strip().lower() != "false"

APNS_KEY = None
# Prefer env vars over filesystem: CVM deploys inject the key via
# docker compose env, not mounted files. APNS_KEY_P8_B64 is base64 to
# survive GH Actions → compose shell quoting of the multi-line PEM.
_env_b64 = os.environ.get("APNS_KEY_P8_B64", "").strip()
if _env_b64:
    try:
        APNS_KEY = base64.b64decode(_env_b64).decode("utf-8")
        print(f"[apns] key loaded from APNS_KEY_P8_B64 (len={len(APNS_KEY)})")
    except Exception as e:
        print(f"[apns] APNS_KEY_P8_B64 decode failed: {e}")
if not APNS_KEY:
    _env_raw = os.environ.get("APNS_KEY_P8", "").strip()
    if _env_raw:
        APNS_KEY = _env_raw
        print(f"[apns] key loaded from APNS_KEY_P8 (len={len(APNS_KEY)})")
if not APNS_KEY:
    _env_path = os.environ.get("APNS_KEY_PATH", "").strip()
    _KEY_SEARCH = [
        Path(_env_path) if _env_path else None,
        FEEDLING_DIR / f"AuthKey_{KEY_ID}.p8",
        Path(__file__).parent / f"AuthKey_{KEY_ID}.p8",
    ]
    for _p in _KEY_SEARCH:
        if _p and _p.exists():
            APNS_KEY = _p.read_text()
            print(f"[apns] key loaded from {_p}")
            break
if not APNS_KEY:
    print("[apns] WARNING: .p8 key not found — push endpoints will log only, not deliver")


def _make_apns_jwt() -> str:
    return jwt.encode(
        {"iss": TEAM_ID, "iat": int(time.time())},
        APNS_KEY,
        algorithm="ES256",
        headers={"kid": KEY_ID},
    )


def _apns_env_name(sandbox: bool) -> str:
    return "sandbox" if sandbox else "production"


def _apns_host(sandbox: bool) -> str:
    return "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"


def _apns_reason_text(result: dict) -> str:
    reason = str((result or {}).get("reason") or "")
    try:
        parsed = json.loads(reason)
        if isinstance(parsed, dict) and parsed.get("reason"):
            return str(parsed.get("reason"))
    except Exception:
        pass
    return reason


def _apns_should_retry_other_env(result: dict) -> bool:
    if (result or {}).get("status") != "error":
        return False
    reason = _apns_reason_text(result)
    return any(
        marker in reason
        for marker in (
            "BadDeviceToken",
            "BadEnvironmentKeyInToken",
            "BadEnvironmentKeyIdInToken",
            "BadCertificateEnvironment",
            # Live Activity tokens can surface this for an environment
            # mismatch. Try the other APNs host before expiring the token.
            "ExpiredToken",
        )
    )


def _apns_token_should_expire(result: dict) -> bool:
    if (result or {}).get("status") != "error":
        return False
    reason = _apns_reason_text(result)
    return any(
        marker in reason
        for marker in (
            "BadDeviceToken",
            "BadEnvironmentKeyInToken",
            "BadEnvironmentKeyIdInToken",
            "BadCertificateEnvironment",
            "DeviceTokenNotForTopic",
            "ExpiredToken",
            "TopicDisallowed",
            "Unregistered",
        )
    )


def _send_apns_once(device_token: str, payload: dict, push_type: str, topic: str, *, sandbox: bool) -> dict:
    host = _apns_host(sandbox)
    url = f"https://{host}/3/device/{device_token}"
    env_name = _apns_env_name(sandbox)
    headers = {
        "authorization": f"bearer {_make_apns_jwt()}",
        "apns-push-type": push_type,
        "apns-topic": topic,
        "apns-expiration": "0",
        "apns-priority": "10",
    }
    try:
        with httpx.Client(http2=True, timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return {"status": "delivered", "apns_env": env_name}
        return {"status": "error", "code": resp.status_code, "reason": resp.text, "apns_env": env_name}
    except Exception as e:
        return {"status": "error", "reason": str(e), "apns_env": env_name}


def _apns_sandbox_for_env(env: str | None) -> bool | None:
    clean = str(env or "").strip().lower()
    if clean == "sandbox":
        return True
    if clean == "production":
        return False
    return None


def _send_apns(
    device_token: str,
    payload: dict,
    push_type: str,
    topic: str,
    *,
    preferred_env: str | None = None,
) -> dict:
    if not APNS_KEY:
        print(f"[apns] no key — logged only → {device_token[:16]}… {payload}")
        return {"status": "logged_only"}

    preferred_sandbox = _apns_sandbox_for_env(preferred_env)
    primary_sandbox = APNS_SANDBOX if preferred_sandbox is None else preferred_sandbox
    attempts: list[dict] = []
    for idx, sandbox in enumerate((primary_sandbox, not primary_sandbox)):
        if idx == 1 and attempts and not _apns_should_retry_other_env(attempts[0]):
            break
        result = _send_apns_once(device_token, payload, push_type, topic, sandbox=sandbox)
        attempts.append(result)
        if result.get("status") == "delivered":
            if idx > 0:
                result["fallback_attempted"] = True
                result["fallback_from"] = attempts[0].get("apns_env", _apns_env_name(primary_sandbox))
            return result

    last = dict(attempts[-1]) if attempts else {"status": "error", "reason": "not_attempted"}
    if len(attempts) > 1:
        last["fallback_attempted"] = True
        last["fallback_from"] = attempts[0].get("apns_env", _apns_env_name(primary_sandbox))
        last["first_error"] = attempts[0]
    last["attempted_envs"] = [str(a.get("apns_env") or "") for a in attempts]
    return last


def _send_apns_to_active_tokens(
    store: UserStore,
    predicate,
    payload: dict,
    *,
    push_type: str,
    topic: str,
    activity_id: str | None = None,
) -> dict:
    candidates = _select_tokens(store, predicate, activity_id=activity_id, active_only=True)
    if not candidates and activity_id:
        candidates = _select_tokens(store, predicate, active_only=True)
    if not candidates:
        return {"status": "skipped", "reason": "no_active_token", "attempts": 0}

    errors = []
    for entry in candidates:
        result = _send_apns(
            entry["token"],
            payload,
            push_type=push_type,
            topic=topic,
            preferred_env=entry.get("apns_env"),
        )
        if result.get("status") == "delivered":
            _mark_active_token_success(store, entry, apns_env=result.get("apns_env"))
            result["attempts"] = len(errors) + 1
            return result

        reason_text = _apns_reason_text(result)
        if reason_text:
            _update_token_lifecycle(store, entry, last_error=reason_text)
        errors.append({
            "type": entry.get("type", ""),
            "activity_id": entry.get("activity_id", ""),
            "registered_at": entry.get("registered_at", ""),
            "apns_env": result.get("apns_env", ""),
            "attempted_envs": result.get("attempted_envs", []),
            "reason": reason_text or str(result.get("reason", "")),
        })
        if _apns_token_should_expire(result):
            _mark_expired_token(store, entry, reason_text or str(result.get("reason", "")))
            continue

    last = errors[-1] if errors else {}
    return {
        "status": "error",
        "reason": last.get("reason", "all_tokens_failed"),
        "attempts": len(errors),
        "errors": errors[-5:],
    }


# ---------------------------------------------------------------------------
# Aggregation helpers (stateless)
# ---------------------------------------------------------------------------

TODAY = datetime.now().strftime("%Y-%m-%d")

IOS_FALLBACK_DATA = {
    "date": TODAY,
    "total_screen_time_minutes": 0,
    "scroll_distance_meters": 0.0,
    "pickups": 0,
    "unlock_count": 0,
    "apps": [],
    "categories": {},
    "frame_count": 0,
    "data_source": "mock_fallback",
}


def _humanize_app_name(raw: str) -> str:
    value = (raw or "unknown").strip()
    if not value:
        return "Unknown"
    if value.startswith("com."):
        tail = value.split(".")[-1]
        if not tail:
            return value
        return tail.replace("_", " ").replace("-", " ").title()
    return value


def _category_for_app(app_name_or_bundle: str) -> str:
    key = (app_name_or_bundle or "").lower()
    if any(x in key for x in ["tiktok", "youtube", "bili", "netflix"]):
        return "Entertainment"
    if any(x in key for x in ["instagram", "twitter", "x.com", "xiaohong", "reddit"]):
        return "Social"
    if any(x in key for x in ["wechat", "telegram", "whatsapp", "messages", "slack", "feishu", "lark"]):
        return "Communication"
    if any(x in key for x in ["safari", "chrome", "browser"]):
        return "Browsing"
    if any(x in key for x in ["maps", "map", "gaode", "waze"]):
        return "Navigation"
    if any(x in key for x in ["camera", "photos", "settings", "preference", "clock", "calendar"]):
        return "Utility"
    return "Other"


def _to_hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _build_ios_data(store: UserStore, window_sec: float = 86400.0) -> dict:
    now = time.time()
    with store.frames_lock:
        frames = [f.copy() for f in store.frames_meta if now - float(f.get("ts", 0)) <= window_sec]

    if not frames:
        fallback = IOS_FALLBACK_DATA.copy()
        fallback["date"] = datetime.now().strftime("%Y-%m-%d")
        return fallback

    frames.sort(key=lambda f: float(f.get("ts", 0)))

    per_app = defaultdict(lambda: {
        "name": "Unknown",
        "bundle_id": "",
        "category": "Other",
        "duration_seconds": 0.0,
        "sessions": 0,
        "first_ts": 0.0,
        "last_ts": 0.0,
    })
    categories_seconds = defaultdict(float)

    MAX_STEP_SECONDS = 8.0
    NEW_SESSION_GAP_SECONDS = 45.0

    session_count = 0
    prev_app_key = None
    prev_ts = None

    for frame in frames:
        ts = float(frame.get("ts", 0.0))
        app_raw = frame.get("app") or "unknown"
        app_key = str(app_raw)

        row = per_app[app_key]
        row["name"] = _humanize_app_name(app_key)
        row["bundle_id"] = app_key
        row["category"] = _category_for_app(app_key)
        row["last_ts"] = ts
        if not row["first_ts"]:
            row["first_ts"] = ts

        if prev_ts is None:
            session_count += 1
            row["sessions"] += 1
        else:
            gap = max(0.0, ts - prev_ts)
            if app_key != prev_app_key or gap > NEW_SESSION_GAP_SECONDS:
                session_count += 1
                row["sessions"] += 1

            if prev_app_key is not None:
                step = min(gap, MAX_STEP_SECONDS)
                per_app[prev_app_key]["duration_seconds"] += step
                categories_seconds[per_app[prev_app_key]["category"]] += step

        prev_app_key = app_key
        prev_ts = ts

    if prev_app_key is not None:
        per_app[prev_app_key]["duration_seconds"] += 1.0
        categories_seconds[per_app[prev_app_key]["category"]] += 1.0

    apps = []
    total_seconds = 0.0
    for app_key, row in per_app.items():
        dur_min = round(row["duration_seconds"] / 60.0, 1)
        total_seconds += row["duration_seconds"]
        apps.append({
            "name": row["name"],
            "bundle_id": row["bundle_id"],
            "category": row["category"],
            "duration_minutes": dur_min,
            "sessions": int(row["sessions"]),
            "first_used": _to_hhmm(row["first_ts"]),
            "last_used": _to_hhmm(row["last_ts"]),
        })

    apps.sort(key=lambda a: a["duration_minutes"], reverse=True)

    categories = {
        cat: round(sec / 60.0, 1)
        for cat, sec in sorted(categories_seconds.items(), key=lambda kv: kv[1], reverse=True)
        if sec > 0
    }

    total_minutes = round(total_seconds / 60.0, 1)
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_screen_time_minutes": total_minutes,
        "scroll_distance_meters": round(total_minutes * 0.02, 2),
        "pickups": int(session_count),
        "unlock_count": int(session_count),
        "apps": apps,
        "categories": categories,
        "frame_count": len(frames),
        "window_sec": int(window_sec),
        "data_source": "real_frames",
    }


MAC_DATA = {
    "date": TODAY,
    "total_active_minutes": 395,
    "deep_work_minutes": 175,
    "focus_score": 72,
    "context_switches": 34,
    "apps": [
        {"name": "Google Chrome", "bundle_id": "com.google.Chrome", "category": "Browsing",
         "duration_minutes": 120, "window_titles": ["Notion – feedling roadmap", "Linear – Sprint 3",
                                                      "Figma Community", "Stack Overflow"]},
        {"name": "Figma", "bundle_id": "com.figma.Desktop", "category": "Design",
         "duration_minutes": 95, "window_titles": ["Feedling iOS – v2 screens", "Component library"]},
        {"name": "Cursor", "bundle_id": "com.todesktop.230313mzl4w4u92", "category": "Development",
         "duration_minutes": 85, "window_titles": ["feedling-mcp-v1 – app.py", "feedling-mcp-v1 – SKILL.md"]},
        {"name": "Zoom", "bundle_id": "us.zoom.xos", "category": "Communication",
         "duration_minutes": 45, "window_titles": ["Weekly sync", "Design review"]},
        {"name": "Slack", "bundle_id": "com.tinyspeck.slackmacgap", "category": "Communication",
         "duration_minutes": 40, "window_titles": ["#design", "#eng", "#general", "DMs"]},
        {"name": "Terminal", "bundle_id": "com.apple.Terminal", "category": "Development",
         "duration_minutes": 10, "window_titles": ["zsh – feedling-mcp-v1"]},
    ],
    "categories": {"Browsing": 120, "Design": 95, "Development": 95, "Communication": 85},
}

SOURCES_DATA = {
    "sources": [
        {"id": "ios_pip", "name": "iPhone PIP Recording", "status": "connected",
         "last_sync": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "device": "iPhone 16 Pro"},
        {"id": "mac_monitor", "name": "Mac Screen Monitor", "status": "connected",
         "last_sync": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "device": "MacBook Pro M3"},
    ]
}


def _log_bootstrap_event(store: UserStore, event_type: str, success: bool, error_message: str = ""):
    entry = {
        "user_id": store.user_id,
        "event_type": event_type,
        "success": success,
        "error_message": error_message,
        "timestamp": datetime.now().isoformat(),
    }
    try:
        with open(store.bootstrap_events_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[{store.user_id}/bootstrap_events] failed to log: {e}")


def _load_bootstrap_events(store: UserStore) -> list[dict]:
    events: list[dict] = []
    try:
        if store.bootstrap_events_file.exists():
            with open(store.bootstrap_events_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
    except Exception as e:
        print(f"[{store.user_id}/bootstrap_events] failed to load: {e}")
    return events


_OFFICIAL_CONSUMER_NAME = "feedling-chat-resident"
_CONSUMER_RECENT_SEC = int(os.environ.get("FEEDLING_CONSUMER_RECENT_SEC", "180"))


def _load_consumer_state(store: UserStore) -> dict:
    try:
        if store.consumer_state_file.exists():
            data = json.loads(store.consumer_state_file.read_text())
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[{store.user_id}/consumer_state] failed to load: {e}")
    return {}


def _save_consumer_state(store: UserStore, state: dict) -> None:
    try:
        tmp = store.consumer_state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        tmp.replace(store.consumer_state_file)
    except Exception as e:
        print(f"[{store.user_id}/consumer_state] failed to save: {e}")


def _consumer_headers_from_request() -> dict:
    name = (request.headers.get("X-Feedling-Consumer") or "").strip()
    if not name:
        return {}
    return {
        "consumer_name": name,
        "consumer_id": (request.headers.get("X-Feedling-Consumer-Id") or "").strip(),
        "consumer_version": (request.headers.get("X-Feedling-Consumer-Version") or "").strip(),
        "consumer_commit": (request.headers.get("X-Feedling-Consumer-Commit") or "").strip(),
        "official": name == _OFFICIAL_CONSUMER_NAME,
        "remote_addr": request.remote_addr or "",
        "user_agent": request.headers.get("User-Agent", ""),
    }


def _record_consumer_event(store: UserStore, event_type: str) -> None:
    info = _consumer_headers_from_request()
    if not info:
        return
    now_epoch = time.time()
    now_iso = datetime.now().isoformat()
    with store.consumer_state_lock:
        state = _load_consumer_state(store)
        state.update(info)
        state["last_event"] = event_type
        state["last_seen_at"] = now_iso
        state["last_seen_epoch"] = now_epoch
        if event_type == "poll":
            state["last_poll_at"] = now_iso
            state["last_poll_epoch"] = now_epoch
        elif event_type == "response":
            state["last_response_at"] = now_iso
            state["last_response_epoch"] = now_epoch
        _save_consumer_state(store, state)


def _consumer_validation_state(store: UserStore) -> dict:
    with store.consumer_state_lock:
        state = _load_consumer_state(store)
    last_poll_epoch = 0.0
    try:
        last_poll_epoch = float(state.get("last_poll_epoch") or 0)
    except Exception:
        last_poll_epoch = 0.0
    age_sec = time.time() - last_poll_epoch if last_poll_epoch > 0 else None
    official = bool(state.get("official"))
    recent = age_sec is not None and age_sec <= _CONSUMER_RECENT_SEC
    passing = official and recent
    return {
        "passing": passing,
        "official": official,
        "consumer_name": state.get("consumer_name", ""),
        "consumer_id": state.get("consumer_id", ""),
        "consumer_version": state.get("consumer_version", ""),
        "consumer_commit": state.get("consumer_commit", ""),
        "last_poll_at": state.get("last_poll_at", ""),
        "last_response_at": state.get("last_response_at", ""),
        "age_sec": age_sec,
        "recent_window_sec": _CONSUMER_RECENT_SEC,
        "required": (
            "Run the standard independent feedling-chat-resident / IO resident "
            "consumer with the current FEEDLING_API_KEY. It must poll "
            "FEEDLING_API_URL/v1/chat/poll and identify itself with the "
            "X-Feedling-Consumer headers."
        ),
    }


# ---------------------------------------------------------------------------
# Users: register endpoint (public — no auth required)
# ---------------------------------------------------------------------------


@app.route("/v1/users/register", methods=["POST"])
def users_register():
    payload = request.get_json(silent=True) or {}
    public_key = (payload.get("public_key") or "").strip()
    archive_language = (payload.get("archive_language") or "").strip()
    result = _register_user(
        public_key=public_key or None,
        archive_language=archive_language or None,
    )
    return jsonify(result), 201


@app.route("/v1/users/whoami", methods=["GET"])
def users_whoami():
    """Identify the caller and return the public material needed to wrap
    content for them.

    Returns:
      - `public_key` — the caller's own X25519 content pubkey (base64),
        from the user record.
      - `enclave_content_public_key_hex` — the live enclave's content
        pubkey, fetched from /attestation and cached for 60s. Missing
        when no enclave is reachable.
      - `archive_language` — the locale code the iOS app supplied at
        registration (e.g. "en", "zh-Hans"). Null for legacy accounts;
        callers fall back to inferring from existing card content.
    """
    store = require_user()
    resp: dict = {"user_id": store.user_id}
    pk = _get_user_public_key(store.user_id)
    if pk:
        resp["public_key"] = pk
    info = _get_enclave_info()
    if info:
        resp["enclave_content_public_key_hex"] = info["content_pk_hex"]
        resp["enclave_compose_hash"] = info["compose_hash"]
    archive_language = _get_user_archive_language(store.user_id)
    if archive_language:
        resp["archive_language"] = archive_language
    return jsonify(resp)


@app.route("/v1/users/preferences", methods=["POST"])
def users_set_preferences():
    """Update mutable preferences on the authenticated user's record.

    Currently the only supported preference is `archive_language` — the
    locale code that the agent should use as the source of truth for
    Memory Garden / Identity Card language. iOS posts this on first
    launch for legacy accounts that registered before the field existed,
    and again whenever the user explicitly changes their iOS system
    language and re-launches the app.

    Body: {"archive_language": "<bcp-47 string>" | null}
    Pass null to clear (agent falls back to inferred behavior).
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    if "archive_language" not in payload:
        return jsonify({
            "error": "archive_language required (string or null)",
        }), 400
    raw = payload.get("archive_language")
    if raw is not None and not isinstance(raw, str):
        return jsonify({"error": "archive_language must be a string or null"}), 400
    new_value = (raw or "").strip() if isinstance(raw, str) else ""

    updated = False
    with _users_lock:
        for u in _users:
            if u.get("user_id") == store.user_id:
                if new_value:
                    u["archive_language"] = new_value
                else:
                    u.pop("archive_language", None)
                updated = True
                break
        if updated:
            _save_users()

    if not updated:
        return jsonify({"error": "user not found"}), 404
    print(f"[users] {store.user_id} archive_language → {new_value or 'cleared'}")
    return jsonify({
        "status": "updated",
        "archive_language": new_value or None,
    })


@app.route("/v1/users/public-key", methods=["POST"])
def users_set_public_key():
    """Update the authenticated user's content public key.

    Used to repair key drift when a client rotates/regenerates its local
    content keypair but keeps the same api_key.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    public_key = (payload.get("public_key") or "").strip()
    if not public_key:
        return jsonify({"error": "public_key required"}), 400

    updated = False
    with _users_lock:
        for u in _users:
            if u.get("user_id") == store.user_id:
                u["public_key"] = public_key
                updated = True
                break
        if updated:
            _save_users()

    if not updated:
        return jsonify({"error": "user not found"}), 404

    print(f"[users] updated public_key for {store.user_id}")
    return jsonify({"ok": True, "user_id": store.user_id})


def _get_user_public_key(user_id: str) -> str:
    """Return the caller's base64 X25519 content pubkey from users.json,
    or empty string if the user predates v1 registration."""
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                return (u.get("public_key") or "").strip()
    return ""


# Cached enclave attestation (for wrapping envelopes we can't decrypt
# ourselves). Refetched every _ENCLAVE_INFO_TTL seconds — short enough
# that a rotated enclave is reflected within the window, long enough
# that writes don't pay a round-trip to the CVM per call.
_ENCLAVE_INFO_TTL = 60.0
_enclave_info_cache: dict = {"ts": 0.0, "data": None}
_enclave_info_lock = threading.Lock()


def _get_enclave_info() -> dict | None:
    """Fetch the enclave's (content_pk_hex, compose_hash) with a short
    cache. Returns None if no enclave is configured or reachable — the
    caller should surface the failure rather than proceed without the
    enclave's pubkey (v1 writes require it for shared visibility)."""
    url = os.environ.get("FEEDLING_ENCLAVE_URL", "").strip()
    if not url:
        return None
    now = time.time()
    with _enclave_info_lock:
        if _enclave_info_cache["data"] and now - _enclave_info_cache["ts"] < _ENCLAVE_INFO_TTL:
            return _enclave_info_cache["data"]
    try:
        # verify=False because the in-cluster enclave presents a
        # self-signed cert whose trust comes from REPORT_DATA, not a CA.
        # We're not pinning here; just fetching public material. Any
        # MITM between backend and enclave would at worst substitute a
        # different pubkey, which would then fail AEAD verification on
        # the enclave side when the agent tries to decrypt.
        with httpx.Client(timeout=5, verify=False) as client:
            r = client.get(f"{url.rstrip('/')}/attestation")
            r.raise_for_status()
            b = r.json()
        data = {
            "content_pk_hex": b.get("enclave_content_pk_hex", ""),
            "compose_hash": b.get("compose_hash", ""),
        }
        if not data["content_pk_hex"]:
            return None
        with _enclave_info_lock:
            _enclave_info_cache["ts"] = now
            _enclave_info_cache["data"] = data
        return data
    except Exception as e:
        print(f"[enclave-info] fetch failed from {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Screen / aggregation
# ---------------------------------------------------------------------------


@app.route("/v1/screen/ios", methods=["GET"])
def get_ios():
    store = require_user()
    try:
        window_sec = max(300.0, min(172800.0, float(request.args.get("window_sec", 86400))))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid window_sec"}), 400
    return jsonify(_build_ios_data(store, window_sec=window_sec))


@app.route("/v1/screen/mac", methods=["GET"])
def get_mac():
    require_user()
    return jsonify(MAC_DATA)


@app.route("/v1/screen/summary", methods=["GET"])
def get_summary():
    store = require_user()
    ios_data = _build_ios_data(store, window_sec=86400)
    top_app = ios_data["apps"][0]["name"] if ios_data.get("apps") else "Unknown"
    categories = ios_data.get("categories") or {}
    top_category = max(categories, key=categories.get) if categories else "Other"

    summary = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ios": {
            "total_screen_time_minutes": ios_data.get("total_screen_time_minutes", 0),
            "top_app": top_app,
            "top_category": top_category,
            "pickups": ios_data.get("pickups", 0),
            "data_source": ios_data.get("data_source", "unknown"),
            "frame_count": ios_data.get("frame_count", 0),
        },
        "mac": {
            "total_active_minutes": MAC_DATA["total_active_minutes"],
            "deep_work_minutes": MAC_DATA["deep_work_minutes"],
            "focus_score": MAC_DATA["focus_score"],
            "top_app": MAC_DATA["apps"][0]["name"],
            "context_switches": MAC_DATA["context_switches"],
        },
        "combined": {
            "total_screen_minutes": ios_data.get("total_screen_time_minutes", 0) + MAC_DATA["total_active_minutes"],
            "insight": "Phone side now comes from real frame aggregation; Mac remains mocked.",
        },
    }
    return jsonify(summary)


@app.route("/v1/sources", methods=["GET"])
def get_sources():
    require_user()
    return jsonify(SOURCES_DATA)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def _live_activity_identity_context(store: UserStore) -> dict:
    identity = _load_identity(store) or {}
    return {
        "aiStart": str(identity.get("relationship_started_at") or "").strip() or None,
    }


def _live_activity_content_state(store: UserStore, payload: dict, *, default_visual_state: str = "reply") -> dict:
    title = (payload.get("title") or "").strip()
    body = (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()
    subtitle = (payload.get("subtitle") or "").strip() or None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    identity_context = _live_activity_identity_context(store)
    visual_state = str(
        payload.get("visualState")
        or payload.get("visual_state")
        or ("reply" if body else default_visual_state)
    ).strip() or default_visual_state
    if visual_state not in {"default", "sharing", "reply"}:
        visual_state = default_visual_state
    name = (payload.get("name") or title or "IO")
    name = str(name).strip() or "IO"

    # Include both the post-animation schema (visualState/name/desc/aiStart)
    # and the earlier schema (title/subtitle/body/data/updatedAt). Swift
    # Codable ignores unknown keys, so this keeps production TestFlight builds
    # and the new animated widget build compatible during rollout.
    return {
        "visualState": visual_state,
        "name": name,
        "desc": body,
        "aiStart": payload.get("aiStart") or payload.get("ai_start") or identity_context.get("aiStart"),
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "personaId": payload.get("personaId", "default"),
        "templateId": payload.get("templateId", "default"),
        "data": data,
        "updatedAt": time.time(),
    }


def _live_activity_body(payload: dict) -> str:
    return (payload.get("body") or payload.get("message") or payload.get("desc") or "").strip()


def _live_activity_top_app(payload: dict) -> str:
    return str(payload.get("topApp") or payload.get("top_app") or "")


@app.route("/v1/push/dynamic-island", methods=["POST"])
def push_dynamic_island():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    return push_live_activity_inner(store, payload)


@app.route("/v1/push/live-activity", methods=["POST"])
def push_live_activity():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    return push_live_activity_inner(store, payload)


def push_live_activity_inner(store: UserStore, payload: dict):
    activity_id = payload.get("activity_id")
    entry = _select_token(store, _is_live_activity_token, activity_id=activity_id, active_only=True)
    if not entry and activity_id:
        entry = _select_token(store, _is_live_activity_token, active_only=True)
    if not entry:
        print(f"[live-activity:{store.user_id}] no active token registered — logged: {payload}")
        return jsonify({
            "status": "logged",
            "activity_id": activity_id or f"la_{uuid.uuid4().hex[:8]}",
            "needs_refresh": True,
            "reason": "no_active_live_activity_token",
            "mode": "update",
        })

    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)

    suppress, reason = store.should_suppress_live_activity(message=body, top_app=top_app)
    if suppress:
        print(f"[live-activity:{store.user_id}] suppressed: {reason} body={body[:60]}")
        return jsonify({
            "status": "suppressed",
            "reason": reason,
            "activity_id": entry.get("activity_id"),
            "mode": "update",
        })

    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": payload.get("event", "update"),
            "content-state": _live_activity_content_state(store, payload, default_visual_state="reply"),
            "alert": {"title": "", "body": ""},
        }
    }
    topic = f"{BUNDLE_ID}.push-type.liveactivity"
    result = _send_apns_to_active_tokens(
        store,
        _is_live_activity_token,
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        activity_id=activity_id,
    )

    delivered = result.get("status") == "delivered"
    if delivered:
        store.record_successful_push()
        store.record_live_activity_sent(message=body, top_app=top_app)

    print(f"[live-activity:{store.user_id}] {result}")
    response = {
        "status": result.get("status", "error"),
        "activity_id": entry.get("activity_id") or activity_id,
        "mode": "update",
    }
    if result.get("code") is not None:
        response["error_code"] = result.get("code")
    if result.get("reason"):
        response["reason"] = result.get("reason")
    if result.get("errors"):
        response["errors"] = result.get("errors")
    if result.get("code") == 410 or _apns_token_should_expire(result):
        response["needs_refresh"] = True
    return jsonify(response)


@app.route("/v1/push/live-start", methods=["POST"])
def push_live_start():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    return push_live_start_inner(store, payload)


def push_live_activity_end_inner(store: UserStore, payload: dict | None = None) -> dict:
    payload = payload or {}
    body = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = payload.get("activity_id")
    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": "end",
            "content-state": _live_activity_content_state(store, payload, default_visual_state="default"),
            "dismissal-date": int(time.time()),
        }
    }
    topic = f"{BUNDLE_ID}.push-type.liveactivity"
    result = _send_apns_to_active_tokens(
        store,
        _is_live_activity_token,
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        activity_id=activity_id,
    )
    if result.get("status") == "delivered":
        store.record_live_activity_sent(message=body, top_app=top_app)
    print(f"[live-end:{store.user_id}] {result}")
    return result


def push_live_start_inner(store: UserStore, payload: dict, *, end_existing: bool = False):
    entry = _select_token(store, _is_push_to_start_token, active_only=True)
    if not entry:
        print(f"[live-start:{store.user_id}] no push_to_start token — logged: {payload}")
        return jsonify({"status": "logged", "reason": "no_active_push_to_start_token", "mode": "start"})

    title = (payload.get("title") or "").strip()
    body_text = _live_activity_body(payload)
    top_app = _live_activity_top_app(payload)
    activity_id = str(payload.get("activity_id") or f"la_{uuid.uuid4().hex[:8]}")
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {"activityId": activity_id}
    attributes_type = str(
        payload.get("attributes-type")
        or payload.get("attributes_type")
        or "ScreenActivityAttributes"
    )
    end_result = None
    if end_existing and _select_token(store, _is_live_activity_token, active_only=True):
        end_result = push_live_activity_end_inner(store, payload)

    apns_payload = {
        "aps": {
            "timestamp": int(time.time()),
            "event": "start",
            "content-state": _live_activity_content_state(store, payload, default_visual_state="reply"),
            "attributes-type": attributes_type,
            "attributes": attributes,
            "alert": {
                "title": title or "OpenClaw",
                "body": body_text or "Live Activity started",
            },
        }
    }

    topic = f"{BUNDLE_ID}.push-type.liveactivity"
    result = _send_apns(
        entry["token"],
        apns_payload,
        push_type="liveactivity",
        topic=topic,
        preferred_env=entry.get("apns_env"),
    )
    if result.get("status") == "delivered":
        _mark_active_token_success(store, entry, apns_env=result.get("apns_env"))
        store.record_live_activity_started(message=body_text, top_app=top_app)
    else:
        reason_text = str(result.get("reason", ""))
        if _apns_token_should_expire(result):
            _mark_expired_token(store, entry, reason_text)

    print(f"[live-start:{store.user_id}] {result}")
    response = {"status": result.get("status", "error"), "mode": "start"}
    if result.get("code") is not None:
        response["error_code"] = result.get("code")
    if result.get("reason"):
        response["reason"] = result.get("reason")
    if result.get("code") == 410 or _apns_token_should_expire(result):
        response["needs_refresh"] = True
    if end_result:
        response["end_status"] = end_result.get("status", "unknown")
        if end_result.get("reason"):
            response["end_reason"] = end_result.get("reason")
    return jsonify(response)


def push_live_activity_hybrid_inner(store: UserStore, payload: dict):
    should_start, start_reason = store.should_start_live_activity()
    if should_start and _select_token(store, _is_push_to_start_token, active_only=True):
        start_resp = push_live_start_inner(store, payload, end_existing=True)
        try:
            start_body = start_resp.get_json(silent=True) or {}
        except Exception:
            start_body = {}
        if start_body.get("status") == "delivered":
            start_body["mode"] = "start"
            start_body["start_reason"] = start_reason
            return jsonify(start_body)

        # If push-to-start is unavailable or rejected, fall back to the cheaper
        # update path so an already-visible activity can still refresh.
        update_resp = push_live_activity_inner(store, payload)
        try:
            update_body = update_resp.get_json(silent=True) or {}
        except Exception:
            update_body = {}
        update_body["mode"] = "start_fallback_update"
        update_body["start_status"] = start_body.get("status", "unknown")
        update_body["start_reason"] = start_body.get("reason") or start_reason
        return jsonify(update_body)

    update_resp = push_live_activity_inner(store, payload)
    try:
        update_body = update_resp.get_json(silent=True) or {}
    except Exception:
        update_body = {}
    update_body["mode"] = "update"
    update_body["start_reason"] = start_reason
    return jsonify(update_body)


def _send_chat_alert(store: UserStore, alert_body: str, alert_title: str = ""):
    """Fire an APNs alert push for an agent chat message. Best-effort:
    failure here never blocks the chat write. The MCP layer (which has
    the plaintext at envelope-build time) passes alert_body in here so
    Flask doesn't have to decrypt anything. Apple's APNs gateway sees
    this string — same posture as Live Activity already has.

    Body is truncated to ~80 chars so long replies render as "...".
    Tap on the notification opens the app (iOS handles routing).
    """
    if not alert_body:
        return {"status": "skipped", "reason": "empty_body"}
    # Match iOS-registered token type: LiveActivityManager registers
    # the standard APNs push token as type="device". Older dev builds used
    # type="apns", so accept both but choose the newest active token.
    if not _select_token(store, _is_device_token, active_only=True):
        print(f"[chat-alert:{store.user_id}] no device token — skip push")
        return {"status": "skipped", "reason": "no_device_token"}

    # Truncate at 80 chars; iOS shows the rest after tapping into chat.
    body = alert_body.strip()
    if len(body) > 80:
        body = body[:79] + "…"

    apns_payload = {
        "aps": {
            "alert": {"title": alert_title or "", "body": body},
            "sound": "default",
        },
        "feedling": {"type": "chat_reply"},
    }
    try:
        result = _send_apns_to_active_tokens(
            store,
            _is_device_token,
            apns_payload,
            push_type="alert",
            topic=BUNDLE_ID,
        )
        print(f"[chat-alert:{store.user_id}] {result.get('status')}")
        return result
    except Exception as e:
        print(f"[chat-alert:{store.user_id}] failed: {e}")
        return {"status": "error", "reason": str(e)}


@app.route("/v1/push/notification", methods=["POST"])
def push_notification():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    if not _select_token(store, _is_device_token, active_only=True):
        print(f"[notification:{store.user_id}] no device token — logged: {payload}")
        return jsonify({"status": "logged", "message_id": f"msg_{uuid.uuid4().hex[:8]}"})

    apns_payload = {
        "aps": {
            "alert": {"title": payload.get("title", ""), "body": payload.get("body", "")},
            "sound": "default",
        }
    }
    result = _send_apns_to_active_tokens(
        store,
        _is_device_token,
        apns_payload,
        push_type="alert",
        topic=BUNDLE_ID,
    )
    print(f"[notification:{store.user_id}] {result}")
    return jsonify({"status": result["status"], "message_id": f"msg_{uuid.uuid4().hex[:8]}"})


@app.route("/v1/push/register-token", methods=["POST"])
def register_token():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    token_type = payload.get("type", "unknown")
    token = payload.get("token", "")
    activity_id = payload.get("activity_id")

    now_iso = datetime.now().isoformat()
    entry = {
        "type": token_type,
        "token": token,
        "registered_at": now_iso,
        "status": "active",
        "last_error": "",
        "last_success_at": "",
        "expired_at": "",
        "updated_at": now_iso,
    }
    if activity_id:
        entry["activity_id"] = activity_id
    apns_env = str(payload.get("apns_env") or payload.get("environment") or "").strip().lower()
    if apns_env in {"sandbox", "production"}:
        entry["apns_env"] = apns_env
    for meta_key in (
        "bundle_id",
        "app_version",
        "app_build",
        "build_configuration",
        "device_model",
        "system_version",
    ):
        meta_value = payload.get(meta_key)
        if meta_value is not None:
            entry[meta_key] = str(meta_value)[:160]

    store.tokens[:] = [
        _normalize_token_entry(t)
        for t in store.tokens
        if not (
            t.get("token") == token
            or (
                t.get("type") == token_type
                and (not activity_id or t.get("activity_id") == activity_id)
            )
        )
    ]
    store.tokens.append(entry)
    store._save_tokens()

    print(f"[register-token:{store.user_id}] {token_type}: {token[:16]}…")
    return jsonify({"status": "registered", "type": token_type})


@app.route("/v1/push/tokens", methods=["GET"])
def list_tokens():
    store = require_user()
    active_only = request.args.get("active_only", "false").lower() == "true"
    tokens = [_normalize_token_entry(t) for t in store.tokens]
    if active_only:
        tokens = [t for t in tokens if _entry_is_active(t)]
    return jsonify({"tokens": tokens})


# ---------------------------------------------------------------------------
# Screen frames
# ---------------------------------------------------------------------------


@app.route("/v1/screen/frames", methods=["GET"])
def list_frames():
    store = require_user()
    limit = min(int(request.args.get("limit", 20)), 100)
    with store.frames_lock:
        recent = [f.copy() for f in reversed(store.frames_meta)][:limit]
    for f in recent:
        f["url"] = _frame_url(store, f["filename"])
    return jsonify({"frames": recent, "total": len(store.frames_meta)})


@app.route("/v1/screen/frames/latest", methods=["GET"])
def latest_frame():
    store = require_user()
    with store.frames_lock:
        if not store.frames_meta:
            return jsonify({"error": "no frames yet"}), 404
        meta = store.frames_meta[-1].copy()
    # image_base64 used to be included here, but every frame is a v1
    # envelope now — the file bytes are opaque ciphertext and only
    # waste ~900KB per call. Callers wanting pixels should hit
    # /v1/screen/frames/<id>/decrypt (or the decrypt_frame MCP tool).
    meta["url"] = _frame_url(store, meta["filename"])
    return jsonify(meta)


@app.route("/v1/screen/frames/<filename>", methods=["GET"])
def serve_frame(filename):
    store = require_user()
    # Reject path traversal
    if "/" in filename or ".." in filename:
        return jsonify({"error": "bad filename"}), 400
    fpath = store.frames_dir / filename
    if not fpath.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(fpath, mimetype="image/jpeg")


# --- Frame decrypt plumbing -------------------------------------------------
# Frames are v1 envelopes just like chat/memory/identity: the broadcast
# extension runs VNRecognizeText on-device, stuffs `image` + `ocr_text`
# into the same JSON payload, and ChaCha20-seals the whole thing. The
# server sees only `body_ct` — that's why frames_meta.ocr_text is always
# "" and screen.analyze.ocr_summary is empty.
#
# Two endpoints open the decrypt path to agents + API clients:
#   GET /v1/screen/frames/<id>/envelope — opaque envelope JSON.
#                                         Used by the enclave to pull
#                                         the ciphertext back for
#                                         in-enclave decryption.
#   GET /v1/screen/frames/<id>/decrypt  — proxies to the enclave's
#                                         /v1/screen/frames/<id>/decrypt
#                                         and returns the plaintext:
#                                         image_b64, ocr_text, app, w, h.
#                                         This is the API-path parity
#                                         clients ask for when they want
#                                         everything curl-reachable too.


def _find_envelope_path(store, frame_id: str) -> Path | None:
    """Locate the on-disk .env.json for a given frame id.

    Fast path: filename is `<id>.env.json` — check that first. Fall back
    to scanning frames_meta by id in case the filename convention shifts.
    """
    if not re.match(r"^[a-f0-9]{16,64}$", frame_id):
        return None
    direct = store.frames_dir / f"{frame_id}.env.json"
    if direct.exists():
        return direct
    with store.frames_lock:
        for meta in store.frames_meta:
            if meta.get("id") == frame_id:
                p = store.frames_dir / meta["filename"]
                if p.exists():
                    return p
    return None


@app.route("/v1/screen/frames/<frame_id>/envelope", methods=["GET"])
def frame_envelope(frame_id):
    """Return the raw v1 envelope JSON for a single frame.

    Callers needing plaintext should hit /v1/screen/frames/<id>/decrypt
    instead — this endpoint exists primarily so the enclave can pull the
    ciphertext back for in-enclave decryption.
    """
    store = require_user()
    fpath = _find_envelope_path(store, frame_id)
    if fpath is None:
        return jsonify({"error": "not found"}), 404
    try:
        env = json.loads(fpath.read_text())
    except Exception as e:
        return jsonify({"error": f"envelope parse: {e}"}), 500
    if not isinstance(env, dict):
        return jsonify({"error": "envelope not an object"}), 500
    return jsonify(env)


@app.route("/v1/screen/frames/<frame_id>/decrypt", methods=["GET"])
def frame_decrypt(frame_id):
    """Proxy to the enclave's decrypt endpoint so API-only clients get
    plaintext without needing the MCP transport.

    Query params are forwarded untouched; the enclave honors
    `include_image=true|false` to gate the base64 JPEG payload (large).
    """
    store = require_user()
    fpath = _find_envelope_path(store, frame_id)
    if fpath is None:
        return jsonify({"error": "not found"}), 404

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return jsonify({"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"}), 503

    # Forward the caller's api_key + any include_image flag.
    api_key = _extract_api_key()
    headers = {"X-API-Key": api_key} if api_key else {}
    params = {"include_image": request.args.get("include_image", "true")}
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/decrypt",
                headers=headers,
                params=params,
            )
        return (r.content, r.status_code, {"Content-Type": r.headers.get("Content-Type", "application/json")})
    except httpx.HTTPError as e:
        return jsonify({"error": f"enclave_error: {e}"}), 502


@app.route("/v1/screen/frames/<frame_id>/image", methods=["GET"])
def frame_image(frame_id):
    """Proxy to the enclave's raw-JPEG endpoint, passing Range through.

    Returns Content-Type image/jpeg with Accept-Ranges: bytes. Clients
    can issue parallel Range GETs to bypass the per-TCP-connection
    throttle on dstack-gateway (~1 Mbps/stream, ~3-4 Mbps aggregate).
    """
    store = require_user()
    fpath = _find_envelope_path(store, frame_id)
    if fpath is None:
        return jsonify({"error": "not found"}), 404

    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        return jsonify({"error": "enclave unreachable — FEEDLING_ENCLAVE_URL not set"}), 503

    api_key = _extract_api_key()
    # Forward api_key + Range (if present) so the enclave's send_file
    # can respond 206 Partial Content with the requested slice.
    fwd_headers = {"X-API-Key": api_key} if api_key else {}
    if request.headers.get("Range"):
        fwd_headers["Range"] = request.headers["Range"]
    try:
        with httpx.Client(timeout=30, verify=False) as client:
            r = client.get(
                f"{enclave_url}/v1/screen/frames/{frame_id}/image",
                headers=fwd_headers,
            )
    except httpx.HTTPError as e:
        return jsonify({"error": f"enclave_error: {e}"}), 502

    resp_headers = {}
    for h in ("Content-Type", "Content-Length", "Content-Range",
              "Accept-Ranges", "ETag", "Last-Modified"):
        if r.headers.get(h):
            resp_headers[h] = r.headers[h]
    return (r.content, r.status_code, resp_headers)


@app.route("/v1/screen/analyze", methods=["GET"])
def analyze_screen():
    store = require_user()
    now = time.time()
    window_sec = max(30.0, min(3600.0, float(request.args.get("window_sec", 300))))
    min_continuous_min = max(1.0, min(120.0, float(request.args.get("min_continuous_min", 3))))

    with store.frames_lock:
        recent = [f for f in store.frames_meta if now - f["ts"] <= window_sec]

    if not recent:
        return jsonify({
            "active": False,
            "rate_limit_ok": False,
            "reason": "No frames in window — phone screen may be off or recording stopped.",
            "current_app": None,
            "continuous_minutes": 0,
            "ocr_summary": "",
            "cooldown_remaining_seconds": round(store.cooldown_remaining_seconds()),
            "latest_ts": None,
            "latest_frame_filename": None,
            "latest_frame_url": None,
            "frame_count_in_window": 0,
        })

    latest = recent[-1]
    current_app = latest.get("app") or "unknown"

    MAX_GAP_SECONDS = 8
    MAX_JITTER_FRAMES = 2

    continuous_start_ts = latest["ts"]
    jitter_count = 0
    prev_ts = latest["ts"]

    for frame in reversed(recent[:-1]):
        if prev_ts - frame["ts"] > MAX_GAP_SECONDS:
            break
        fapp = frame.get("app") or "unknown"
        if fapp == current_app:
            continuous_start_ts = frame["ts"]
            jitter_count = 0
        else:
            jitter_count += 1
            if jitter_count > MAX_JITTER_FRAMES:
                break
        prev_ts = frame["ts"]

    continuous_minutes = round((latest["ts"] - continuous_start_ts) / 60, 1)

    seen_ocr: set[str] = set()
    ocr_parts: list[str] = []
    for f in reversed(recent):
        text = (f.get("ocr_text") or "").strip()
        if text and text not in seen_ocr:
            seen_ocr.add(text)
            ocr_parts.append(text[:200])
            if len(ocr_parts) >= 3:
                break
    ocr_summary = " | ".join(reversed(ocr_parts))[:500]

    cooldown_remaining = store.cooldown_remaining_seconds()
    rate_limit_ok = cooldown_remaining == 0
    semantic = _semantic_analysis(current_app=current_app, ocr_summary=ocr_summary)
    semantic_strength = semantic.get("semantic_strength", "weak")

    exploratory_allowed = (
        semantic_strength == "weak"
        and len(ocr_summary) >= 20
        and continuous_minutes >= 1.0
    )

    if semantic_strength == "strong":
        trigger_basis = "semantic_strong"
        reason = f"semantic:{semantic.get('semantic_scene', 'unknown')}"
    elif exploratory_allowed:
        trigger_basis = "curiosity_exploratory"
        reason = "ambiguous_context_but_conversation_worth_starting"
    elif continuous_minutes >= min_continuous_min:
        trigger_basis = "legacy_time_fallback"
        reason = f"continuous_minutes {continuous_minutes} >= min_continuous_min {min_continuous_min}"
    else:
        trigger_basis = "insufficient_signal"
        reason = "no_semantic_trigger_and_not_enough_context"

    return jsonify({
        "active": True,
        "current_app": current_app,
        "continuous_minutes": continuous_minutes,
        "ocr_summary": ocr_summary,
        "rate_limit_ok": rate_limit_ok,
        "cooldown_remaining_seconds": round(cooldown_remaining),
        "reason": reason,
        "trigger_policy": "semantic_first",
        "trigger_basis": trigger_basis,
        "semantic_scene": semantic.get("semantic_scene"),
        "task_intent": semantic.get("task_intent"),
        "friction_point": semantic.get("friction_point"),
        "semantic_confidence": semantic.get("confidence", 0.0),
        "suggested_openers": semantic.get("suggested_openers", [])[:2],
        "latest_ts": latest["ts"],
        "latest_frame_filename": latest.get("filename"),
        "latest_frame_url": _frame_url(store, latest.get("filename")) if latest.get("filename") else None,
        "frame_count_in_window": len(recent),
    })


# ---------------------------------------------------------------------------
# Proactive hidden jobs
# ---------------------------------------------------------------------------


@app.route("/v1/proactive/settings", methods=["GET", "POST"])
def proactive_settings():
    store = require_user()
    if request.method == "GET":
        return jsonify(store.load_proactive_settings())
    payload = request.get_json(silent=True) or {}
    settings = store.save_proactive_settings(payload)
    return jsonify(settings)


@app.route("/v1/device/events", methods=["GET", "POST"])
def device_events():
    store = require_user()
    if request.method == "GET":
        try:
            since = float(request.args.get("since", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid since"}), 400
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            return jsonify({"error": "invalid limit"}), 400
        limit = max(1, min(limit, 200))
        return jsonify({"events": store.list_device_events(since_epoch=since, limit=limit)})

    payload = request.get_json(silent=True) or {}
    event = _make_device_event(
        source=str(payload.get("source") or "ios"),
        event_type=str(payload.get("type") or payload.get("event_type") or "unknown"),
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
    )
    store.append_device_event(event)
    return jsonify(event)


@app.route("/v1/proactive/tick", methods=["POST"])
def proactive_tick():
    """Manual V0a Gate tick.

    This endpoint is intentionally explicit: scheduler policy can be added
    later, but the first shippable loop should be easy to inspect and replay.
    If Gate passes, it creates a hidden job. The resident consumer, not the
    server, realizes that job through the user's real agent entry.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    decision = _build_proactive_gate_decision(store, payload, api_key=_extract_api_key())
    store.append_gate_decision(decision)

    job = None
    if decision.get("should_reach_out"):
        job = store.append_proactive_job(_proactive_job_from_decision(decision))

    return jsonify({
        "decision": decision,
        "job": job,
        "enqueued": job is not None,
    })


def _job_status_patch(payload: dict, *, default_status: str = "") -> dict:
    status = str(payload.get("status") or default_status).strip().lower()
    reason = str(payload.get("reason") or payload.get("status_reason") or "").strip()
    consumer_id = str(payload.get("consumer_id") or "").strip()
    now_iso = datetime.now().isoformat()
    patch: dict = {}
    if status:
        patch["status"] = status[:80]
        if status == "claimed":
            patch["claimed_at"] = now_iso
        elif status == "realizing":
            patch["realizing_at"] = now_iso
        elif status in {"posted", "delivered"}:
            patch["posted_at"] = now_iso
        elif status in {"failed", "skipped"}:
            patch["failed_at"] = now_iso
    if reason:
        patch["status_reason"] = reason[:500]
    if consumer_id:
        patch["consumer_id"] = consumer_id[:160]
    if payload.get("chat_message_id"):
        patch["chat_message_id"] = str(payload.get("chat_message_id"))[:160]
    return patch


@app.route("/v1/proactive/jobs/<job_id>/claim", methods=["POST"])
def proactive_job_claim(job_id):
    store = require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload, default_status="claimed")
    job = store.update_proactive_job(job_id, patch, only_if_status="pending")
    if job is None:
        current = None
        for row in store.list_proactive_jobs(since_epoch=0, limit=0):
            if str(row.get("job_id") or "") == str(job_id):
                current = row
                break
        return jsonify({"claimed": False, "job": current, "reason": "not_pending_or_missing"})
    return jsonify({"claimed": True, "job": job})


@app.route("/v1/proactive/jobs/<job_id>/status", methods=["POST"])
def proactive_job_status(job_id):
    store = require_user()
    payload = request.get_json(silent=True) or {}
    patch = _job_status_patch(payload)
    if not patch:
        return jsonify({"error": "empty_status_patch"}), 400
    job = store.update_proactive_job(job_id, patch)
    if job is None:
        return jsonify({"error": "job_not_found"}), 404
    return jsonify({"job": job})


@app.route("/v1/proactive/decisions", methods=["GET"])
def proactive_decisions():
    store = require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))
    return jsonify({"decisions": store.list_gate_decisions(since_epoch=since, limit=limit)})


@app.route("/v1/proactive/decisions/<decision_id>/review", methods=["POST"])
def proactive_decision_review(decision_id):
    store = require_user()
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form.to_dict(flat=True)

    decision_id = str(decision_id or "").strip()
    if not decision_id:
        return jsonify({"error": "decision_id_required"}), 400

    decision = None
    for row in store.list_gate_decisions(since_epoch=0, limit=0):
        if str(row.get("decision_id") or "") == decision_id:
            decision = row
            break
    if decision is None:
        return jsonify({"error": "decision_not_found"}), 404

    label = str(payload.get("label") or "").strip()
    allowed_labels = {
        "correct_true",
        "correct_false",
        "missed_opportunity",
        "spam",
        "weak_connection",
        "repeated",
        "privacy_bad",
        "great_companion_moment",
    }
    if label not in allowed_labels:
        return jsonify({"error": "invalid_label", "allowed": sorted(allowed_labels)}), 400

    review = {
        "review_id": _new_public_id("gr"),
        "decision_id": decision_id,
        "ts": time.time(),
        "created_at": datetime.now().isoformat(),
        "label": label,
        "notes": str(payload.get("notes") or "")[:500],
        "reviewer": str(payload.get("reviewer") or "human")[:80],
        "expected_should_reach_out": payload.get("expected_should_reach_out"),
        "correct_connection_source_id": str(payload.get("correct_connection_source_id") or "")[:160],
        "decision_should_reach_out": bool(decision.get("should_reach_out")),
        "decision_reason": str(decision.get("reason") or decision.get("abstention_reason") or "")[:240],
        "decision_intent_label": str(decision.get("intent_label") or "")[:120],
        "decision_connection": decision.get("connection") or {},
        "frame_ids": decision.get("frame_ids") or [],
    }
    store.append_gate_review(review)
    accept = request.headers.get("Accept", "")
    if not request.is_json and "text/html" in accept:
        return Response(
            "<html><body><p>Review saved.</p><script>history.back()</script></body></html>",
            mimetype="text/html",
        )
    return jsonify({"review": review})


@app.route("/v1/proactive/reviews", methods=["GET"])
def proactive_reviews():
    store = require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 500))
    return jsonify({"reviews": store.list_gate_reviews(since_epoch=since, limit=limit)})


@app.route("/v1/proactive/debug", methods=["GET"])
def proactive_debug_json():
    store = require_user()
    return jsonify(_proactive_debug_snapshot(store))


@app.route("/debug/proactive", methods=["GET"])
def proactive_debug_page():
    store = require_user()
    html_body = _render_proactive_dashboard(_proactive_debug_snapshot(store))
    return Response(html_body, mimetype="text/html")


@app.route("/v1/proactive/jobs/poll", methods=["GET"])
def proactive_jobs_poll():
    store = require_user()
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    try:
        timeout = max(0.0, min(float(request.args.get("timeout", 30)), 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid timeout"}), 400
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 100))

    pending = [
        j for j in store.list_proactive_jobs(since_epoch=since, limit=limit)
        if str(j.get("status") or "pending") == "pending"
    ]
    if pending:
        return jsonify({"jobs": pending, "timed_out": False})

    ev = threading.Event()
    with store.proactive_job_waiters_lock:
        store.proactive_job_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.proactive_job_waiters_lock:
        try:
            store.proactive_job_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        pending = [
            j for j in store.list_proactive_jobs(since_epoch=since, limit=limit)
            if str(j.get("status") or "pending") == "pending"
        ]
        return jsonify({"jobs": pending, "timed_out": False})
    return jsonify({"jobs": [], "timed_out": True})


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@app.route("/v1/chat/history", methods=["GET"])
def chat_history():
    store = require_user()
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    limit = max(1, min(limit, 200))

    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400

    before_raw = request.args.get("before", "")
    before = 0.0
    if before_raw not in ("", None):
        try:
            before = float(before_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid before"}), 400

    include_image_body = str(
        request.args.get("include_image_body", request.args.get("include_image_bodies", "true"))
    ).lower() not in {"0", "false", "no", "off"}

    with store.chat_lock:
        all_msgs = list(store.chat_messages)
        total = len(store.chat_messages)

    if before > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) < before]
        msgs = filtered[-limit:]
        has_more_older = len(filtered) > len(msgs)
        has_more_newer = False
        page_mode = "before"
    elif since > 0:
        filtered = [m for m in all_msgs if float(m.get("ts", 0)) > since]
        msgs = filtered[:limit]
        has_more_older = bool(all_msgs and msgs and float(all_msgs[0].get("ts", 0)) < float(msgs[0].get("ts", 0)))
        has_more_newer = len(filtered) > len(msgs)
        page_mode = "since"
    else:
        msgs = all_msgs[-limit:]
        has_more_older = len(all_msgs) > len(msgs)
        has_more_newer = False
        page_mode = "latest"

    out = [_chat_history_item(m, include_image_body=include_image_body) for m in msgs]
    omitted_image_bodies = sum(1 for m in out if m.get("body_omitted"))
    oldest_ts = float(out[0].get("ts", 0)) if out else 0
    latest_ts = float(out[-1].get("ts", 0)) if out else 0

    ua = request.headers.get("User-Agent", "")
    print(
        f"[chat/history:{store.user_id}] ip={request.remote_addr} mode={page_mode} "
        f"since={since} before={before} limit={limit} returned={len(out)} total={total} "
        f"include_image_body={include_image_body} omitted_images={omitted_image_bodies} ua={ua[:80]}"
    )

    return jsonify({
        "messages": out,
        "total": total,
        "oldest_ts": oldest_ts,
        "latest_ts": latest_ts,
        "has_more_older": has_more_older,
        "has_more_newer": has_more_newer,
        "image_bodies_omitted": omitted_image_bodies,
    })


def _chat_history_item(m: dict, *, include_image_body: bool = True) -> dict:
    item = dict(m)
    # iOS ChatMessage.content is non-optional. v1 envelope messages are
    # ciphertext-only at rest and may omit plaintext `content`; always
    # include an empty string so Decodable succeeds and client-side decrypt
    # can populate content later.
    item.setdefault("content", "")

    if item.get("content_type", "text") == "image":
        item["body_ct_len"] = len(item.get("body_ct") or "")
        if not include_image_body:
            item["body_omitted"] = True
            for key in ("body_ct", "nonce", "K_user", "K_enclave"):
                item.pop(key, None)
        else:
            item["body_omitted"] = False

    role = item.get("role")
    if role == "openclaw":
        item["sender"] = "assistant"
        item["is_from_openclaw"] = True
    elif role == "user":
        item["sender"] = "user"
        item["is_from_openclaw"] = False
    return item


@app.route("/v1/chat/messages/<message_id>/body", methods=["GET"])
def chat_message_body(message_id):
    store = require_user()
    with store.chat_lock:
        msg = next((m for m in store.chat_messages if str(m.get("id") or "") == str(message_id)), None)
    if not msg:
        return jsonify({"error": "message_not_found"}), 404
    return jsonify({"message": _chat_history_item(msg, include_image_body=True)})


@app.route("/v1/chat/message", methods=["POST"])
def chat_message():
    """User sends a chat message as a v1 ciphertext envelope.

    See docs/DESIGN_E2E.md §3.2 for envelope field definitions. The
    server never decrypts the envelope — it is stored verbatim and
    later surfaced by the enclave's /v1/* handlers.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return jsonify({"error": "content_type must be 'text' or 'image'"}), 400
    msg = store.append_chat("user", "chat", envelope, content_type=content_type)
    store.notify_chat_waiters()
    print(f"[chat:{store.user_id}] user(v1, visibility={envelope['visibility']}, type={content_type}) id={msg['id']}")
    return jsonify({"id": msg["id"], "ts": msg["ts"], "v": msg["v"]})


@app.route("/v1/chat/response", methods=["POST"])
def chat_response():
    """Agent posts a reply as a v1 ciphertext envelope. Shape matches
    /v1/chat/message. The optional `push_live_activity` / `push_body` /
    `title` / `subtitle` / `data` fields trigger an APNs Live Activity
    delivery. Proactive replies use a hybrid policy: push-to-start at most
    once per start window, then cheaper updates while the activity is presumed
    alive. `push_body` is plaintext metadata (user-visible in APNs surfaces)
    and is never stored in chat.

    Bootstrap gate: this endpoint 409s if memory_count < the per-age floor
    (see _memory_floor_for_days) or identity is not yet written. See
    _gate_bootstrap_for_chat for the rationale — runtime-level skill text
    isn't enough to stop hallucinated bootstrap completion; the server has
    to enforce it.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    allow_verify_reply = _reply_is_for_pending_verify_ping(store)
    gated = _gate_bootstrap_for_chat(store, allow_verify_reply=allow_verify_reply)
    if gated is not None:
        return gated
    _record_consumer_event(store, "response")
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    content_type = payload.get("content_type", "text")
    if content_type not in ("text", "image"):
        return jsonify({"error": "content_type must be 'text' or 'image'"}), 400
    source = str(payload.get("source") or "chat").strip() or "chat"
    if source not in {"chat", "live_activity", "heartbeat", PROACTIVE_JOB_SOURCE}:
        return jsonify({"error": "invalid source"}), 400
    alert_body = str(payload.get("alert_body") or "")
    push_body = str(payload.get("push_body") or "")
    extra = {
        "gate_decision_id": str(payload.get("gate_decision_id") or ""),
        "proactive_job_id": str(payload.get("proactive_job_id") or ""),
    }
    if source == PROACTIVE_JOB_SOURCE:
        preview = (alert_body or push_body).strip()
        if preview:
            extra["alert_preview"] = preview[:240]
        if push_body.strip():
            extra["push_body_preview"] = push_body.strip()[:240]
        extra["push_live_activity_requested"] = bool(payload.get("push_live_activity"))
    msg = store.append_chat(
        "openclaw",
        source,
        envelope,
        content_type=content_type,
        extra=extra,
    )
    delivery_fields: dict = {}
    if payload.get("push_live_activity"):
        push_payload = {
            "title": payload.get("title", "") or "IO",
            "body": payload.get("push_body", ""),
            "subtitle": payload.get("subtitle"),
            "data": payload.get("data", {}),
            "visualState": payload.get("visualState") or payload.get("visual_state") or "reply",
        }
        if source == PROACTIVE_JOB_SOURCE:
            live_resp = push_live_activity_hybrid_inner(store, push_payload)
        else:
            live_resp = push_live_activity_inner(store, push_payload)
        try:
            live_body = live_resp.get_json(silent=True) or {}
        except Exception:
            live_body = {}
        delivery_fields["live_activity_status"] = live_body.get("status", "unknown")
        delivery_fields["live_activity_reason"] = live_body.get("reason", "")
        delivery_fields["live_activity_activity_id"] = live_body.get("activity_id", "")
        delivery_fields["live_activity_mode"] = live_body.get("mode", "")
    # Fire APNs alert push so users not currently in the app still see
    # the agent's message. MCP supplies `alert_body` (plaintext) — the
    # server itself doesn't decrypt the envelope. Best-effort: failures
    # here don't block the chat write.
    if alert_body:
        alert_result = _send_chat_alert(store, alert_body, alert_title=payload.get("title", ""))
        delivery_fields["alert_status"] = (alert_result or {}).get("status", "unknown")
        delivery_fields["alert_reason"] = (alert_result or {}).get("reason", "")
    if delivery_fields:
        updated = store.update_chat_message_metadata(msg["id"], delivery_fields)
        if updated:
            msg = updated
    print(f"[chat:{store.user_id}] openclaw(v1, source={source}, type={content_type}) id={msg['id']}")
    return jsonify({"id": msg["id"], "ts": msg["ts"], "v": msg["v"]})


@app.route("/v1/chat/poll", methods=["GET"])
def chat_poll():
    store = require_user()
    _record_consumer_event(store, "poll")
    try:
        since = float(request.args.get("since", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid since"}), 400
    timeout = min(float(request.args.get("timeout", 30)), 60)

    with store.chat_lock:
        pending = [m for m in store.chat_messages if m["ts"] > since and m["role"] == "user"]
    if pending:
        return jsonify({"messages": pending, "timed_out": False})

    ev = threading.Event()
    with store.chat_waiters_lock:
        store.chat_waiters.append(ev)

    notified = ev.wait(timeout=timeout)

    with store.chat_waiters_lock:
        try:
            store.chat_waiters.remove(ev)
        except ValueError:
            pass

    if notified:
        with store.chat_lock:
            pending = [m for m in store.chat_messages if m["ts"] > since and m["role"] == "user"]
        return jsonify({"messages": pending, "timed_out": False})
    return jsonify({"messages": [], "timed_out": True})


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def _load_identity(store: UserStore) -> dict | None:
    try:
        if store.identity_file.exists():
            return json.loads(store.identity_file.read_text())
    except Exception as e:
        print(f"[{store.user_id}/identity] load failed: {e}")
    return None


def _save_identity(store: UserStore, data: dict):
    with store.identity_lock:
        store.identity_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# Identity change audit log
# ---------------------------------------------------------------------------
# Appended to on every identity_init / replace / nudge. Surfaced to iOS as
# the "最近的变化" feed and the local push trigger. Server doesn't decrypt
# the envelope, so the diff (dimension / old / new / reason) is supplied
# by the caller — the MCP tools do this; HTTP-mode callers can pass an
# optional `audit` field on identity_init / identity_replace requests.

def _append_identity_change(store: UserStore, entry: dict) -> dict:
    """Append a single audit entry. Always returns the stored entry
    (with `id` and `ts` injected) so the caller can echo it back. Never
    raises — audit failures must not break the underlying write."""
    record = {
        "id": uuid.uuid4().hex[:16],
        "ts": datetime.now().isoformat(),
        "action": entry.get("action", "unknown"),
    }
    # Whitelist + coerce the fields the iOS card needs. Anything else
    # the caller submits is dropped silently so we don't leak whatever
    # debugging junk the agent stuffed in.
    for k in ("dimension", "old_value", "new_value", "delta", "reason"):
        if k in entry:
            record[k] = entry[k]
    try:
        with open(store.identity_changes_file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[{store.user_id}/identity_changes] append failed: {e}")
    return record


def _load_identity_changes(store: UserStore, since: str = "", limit: int = 50) -> list:
    """Read the audit log. `since` is an ISO timestamp string; results
    are filtered to entries with ts > since, newest-first, capped at limit."""
    entries: list = []
    try:
        if store.identity_changes_file.exists():
            with open(store.identity_changes_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
    except Exception as e:
        print(f"[{store.user_id}/identity_changes] load failed: {e}")
        return []
    if since:
        entries = [e for e in entries if e.get("ts", "") > since]
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


def _parse_iso_calendar_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        norm = raw.replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm + "T00:00:00"
        return datetime.fromisoformat(norm).date()
    except Exception:
        return None


def _earliest_memory_date(store: UserStore) -> date | None:
    dates: list[date] = []
    for moment in _load_moments(store):
        if not isinstance(moment, dict):
            continue
        d = _parse_iso_calendar_date(moment.get("occurred_at", ""))
        if d:
            dates.append(d)
    return min(dates) if dates else None


def _anchor_from_days(days: int, store: UserStore | None = None, prefer_memory: bool = False) -> str:
    """Convert "we've known each other N days" into a fixed ISO timestamp.

    The anchor is the source of truth for days_with_user — every read computes
    a calendar-day delta from this date, so the displayed count increments at
    midnight instead of at the exact bootstrap hour.
    """
    if prefer_memory and store is not None:
        earliest = _earliest_memory_date(store)
        if earliest:
            return earliest.isoformat()
    safe_days = max(0, int(days))
    started_at = datetime.now().date() - timedelta(days=safe_days)
    return started_at.isoformat()


def _live_days_with_user(identity: dict, store: UserStore | None = None) -> int:
    """Compute the live days_with_user from the relationship anchor."""
    anchor_date = _parse_iso_calendar_date(identity.get("relationship_started_at", ""))

    # Migration repair for anchors created from server UTC time after the
    # user's local midnight boundary: if old identities have no explicit
    # anchor source and the memory garden proves an earlier first date, use it.
    if store is not None and not identity.get("relationship_anchor_source"):
        earliest = _earliest_memory_date(store)
        if earliest and (anchor_date is None or earliest < anchor_date):
            anchor_date = earliest

    if not anchor_date:
        return 0
    return max(0, (datetime.now().date() - anchor_date).days)


# ---------------------------------------------------------------------------
# Bootstrap stage gating
# ---------------------------------------------------------------------------
#
# Multiple production incidents (2026-05-13..15) showed Agent runtimes
# (OpenClaw in particular) skipping the memory-write phase of bootstrap
# and going straight to identity_init / chat_post. The Agent's own narrative
# would claim "I wrote 18 cards" while the server actually had zero — pure
# hallucination of completion.
#
# Skill-text rules alone cannot enforce this; behavior depends on the
# runtime's attention, prompt adherence, and failure modes. Server-side
# gates make the contract explicit: writes that violate the protocol
# return 409 with the missing prerequisite, and the Agent must satisfy
# the prerequisite before retrying.
#
# Floor is dynamic by relationship age — see _memory_floor_for_days
# (defined further down with the verify endpoints). A 6-month relationship
# legitimately demands more cards than a "we just met today" one. The
# previous hardcoded floor=3 was a compromise that was too strict for
# brand-new relationships AND too lax for established ones; replaced
# 2026-05-19 with the per-age table that's already documented in skill.

# Public skill URL — included in 409 responses so Agents that don't carry
# the skill in context can refetch it. Single source of truth.
_SKILL_URL = "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md"


def _bootstrap_state(store) -> dict:
    """Snapshot of bootstrap completion for `store`. Read-only; safe to call
    on every write path. Source of truth: on-disk identity + memory files.

    Returns:
        {
          memory_count: int,                # total across tabs
          memory_floor: int,                # total floor (back-compat)
          counts: {story, about_me, ta_thinking, total},
          floors: {story, about_me, ta_thinking, total},
          identity_written: bool,
          stage: str ∈ {"needs_memory", "needs_identity", "main_loop"},
          missing_tabs: [tab_name, ...]     # Which tab floors are unmet
        }

    Gate semantics (post-typed-memory rewrite):
      - "needs_memory" means Story floor OR About me floor not yet met.
        TA 在想 (insight/reflection) is encouraged but not blocking —
        reflections need substrate from the other two tabs first, so
        gating on it would create a deadlock at low-density tiers.
    """
    moments = _load_moments(store)
    counts = _count_by_tab(moments)
    identity_written = _load_identity(store) is not None
    floors = _per_tab_floors_for_days(_relationship_age_days(store))

    missing_tabs = []
    if counts["story"] < floors["story"]:
        missing_tabs.append("story")
    if counts["about_me"] < floors["about_me"]:
        missing_tabs.append("about_me")

    if missing_tabs:
        stage = "needs_memory"
    elif not identity_written:
        stage = "needs_identity"
    else:
        stage = "main_loop"

    return {
        "memory_count": counts["total"],
        "memory_floor": floors["total"],
        "counts": counts,
        "floors": floors,
        "identity_written": identity_written,
        "stage": stage,
        "missing_tabs": missing_tabs,
    }


def _gate_required_for_missing_tabs(state) -> str:
    """Human-readable instruction string for the missing tabs in `state`."""
    c = state["counts"]
    f = state["floors"]
    parts = []
    if "story" in state["missing_tabs"]:
        parts.append(
            f"Story tab {c['story']}/{f['story']} — write more moment/quote memories"
        )
    if "about_me" in state["missing_tabs"]:
        parts.append(
            f"About me tab {c['about_me']}/{f['about_me']} — write more fact/event memories "
            f"(this is the density layer — preferences, relationships, dates, habits)"
        )
    return (
        "Per-tab memory floors are below threshold: "
        + "; ".join(parts)
        + ". Use feedling_memory_add_moment(type=...) for each. Then call "
        "feedling_identity_init. Do not fabricate Pass 4 summaries — the cards "
        "must actually exist."
    )


def _chat_loop_verified_by_server(store) -> bool:
    events = _load_bootstrap_events(store)
    if any(
        e.get("event_type") == "chat_loop_verified" and e.get("success") is True
        for e in events
    ):
        return True

    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_user = False
    for m in sorted_msgs:
        role = m.get("role")
        if role == "user" and m.get("source") != "verify_ping":
            seen_user = True
        elif role in ("agent", "openclaw") and seen_user:
            return True
    return False


def _reply_is_for_pending_verify_ping(store) -> bool:
    """True when the next agent response is the private verify-loop reply.

    /v1/chat/verify_loop must be able to receive one agent response before
    the visible chat gate is open. The synthetic ping is removed after the
    verify completes, so this does not leak into user chat.
    """
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    for m in reversed(chat_msgs):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("agent", "openclaw"):
            return False
        if role == "user":
            return m.get("source") == "verify_ping"
    return False


def _gate_bootstrap_for_chat(store, allow_verify_reply: bool = False):
    """Refuse /v1/chat/response when bootstrap is incomplete.

    Returns a (response, status) tuple to be returned by the caller, or None
    when the call may proceed. The response body carries `stage` and
    `required` so the Agent receives an actionable error rather than a
    generic 403/500.
    """
    state = _bootstrap_state(store)
    if state["stage"] == "main_loop":
        if allow_verify_reply:
            return None
        consumer_state = _consumer_validation_state(store)
        if not consumer_state["passing"]:
            print(
                f"[gate:{store.user_id}] chat_response blocked stage=needs_resident_consumer "
                f"consumer={consumer_state.get('consumer_name')} recent={consumer_state.get('age_sec')}"
            )
            return jsonify({
                "error": "bootstrap_incomplete",
                "stage": "needs_resident_consumer",
                "memory_count": state["memory_count"],
                "memory_floor": state["memory_floor"],
                "counts": state["counts"],
                "floors": state["floors"],
                "missing_tabs": state["missing_tabs"],
                "identity_written": state["identity_written"],
                "resident_consumer": consumer_state,
                "required": consumer_state["required"],
                "skill_url": _SKILL_URL,
            }), 409
        if not _chat_loop_verified_by_server(store):
            required = (
                "After the standard resident consumer is running, call "
                "feedling_chat_verify_loop and wait for passing=true before "
                "sending any visible IO Chat greeting."
            )
            print(f"[gate:{store.user_id}] chat_response blocked stage=needs_live_connection")
            return jsonify({
                "error": "bootstrap_incomplete",
                "stage": "needs_live_connection",
                "memory_count": state["memory_count"],
                "memory_floor": state["memory_floor"],
                "counts": state["counts"],
                "floors": state["floors"],
                "missing_tabs": state["missing_tabs"],
                "identity_written": state["identity_written"],
                "resident_consumer": consumer_state,
                "required": required,
                "skill_url": _SKILL_URL,
            }), 409
        return None
    if state["stage"] == "needs_memory":
        required = _gate_required_for_missing_tabs(state)
    else:  # needs_identity
        required = (
            "Call feedling_identity_init with the derived identity card "
            "(7 dimensions + days_with_user) BEFORE you can post chat."
        )
    print(f"[gate:{store.user_id}] chat_response blocked stage={state['stage']} "
          f"missing={state['missing_tabs']} id={state['identity_written']}")
    return jsonify({
        "error": "bootstrap_incomplete",
        "stage": state["stage"],
        "memory_count": state["memory_count"],
        "memory_floor": state["memory_floor"],
        "counts": state["counts"],
        "floors": state["floors"],
        "missing_tabs": state["missing_tabs"],
        "identity_written": state["identity_written"],
        "required": required,
        "skill_url": _SKILL_URL,
    }), 409


def _gate_bootstrap_for_identity_init(store):
    """Refuse /v1/identity/init when Story or About me tab floors are unmet.

    Identity must be DERIVED from memory substrate — writing identity in the
    30+ day tier with only 2 cards means the Agent skipped the depth pass.
    TA 在想 floor is advisory at this gate (reflections need other-tab
    substrate first, gating on it would deadlock low-density users).
    """
    state = _bootstrap_state(store)
    if not state["missing_tabs"]:
        return None
    print(f"[gate:{store.user_id}] identity_init blocked missing={state['missing_tabs']} "
          f"counts={state['counts']} floors={state['floors']}")
    return jsonify({
        "error": "bootstrap_incomplete",
        "stage": "needs_memory",
        "memory_count": state["memory_count"],
        "memory_floor": state["memory_floor"],
        "counts": state["counts"],
        "floors": state["floors"],
        "missing_tabs": state["missing_tabs"],
        "required": _gate_required_for_missing_tabs(state)
                    + " Identity dimensions must be derived from real cards, not invented.",
        "skill_url": _SKILL_URL,
    }), 409


@app.route("/v1/identity/get", methods=["GET"])
def identity_get():
    store = require_user()
    data = _load_identity(store)
    if data is None:
        return jsonify({"identity": None})
    # Inject the live-computed days alongside the envelope. iOS decrypts the
    # envelope locally, but it never sees the anchor itself — it just reads
    # this top-level field. Same convention as the enclave proxy.
    enriched = dict(data)
    enriched["days_with_user"] = _live_days_with_user(data, store=store)
    return jsonify({"identity": enriched})


@app.route("/v1/identity/init", methods=["POST"])
def identity_init():
    """Initialize the identity card as a v1 envelope. body_ct wraps
    {agent_name, self_introduction, dimensions} serialized as JSON.
    Plaintext metadata: id, created_at, updated_at. See DESIGN_E2E.md §3.2.

    Bootstrap gate: requires memory_count >= the per-age floor (see
    _memory_floor_for_days). Identity must be DERIVED from memories per
    skill protocol; writing identity without depth proportional to the
    relationship age means the Agent skipped the depth pass.
    See _gate_bootstrap_for_identity_init.
    """
    store = require_user()
    existing = _load_identity(store)
    if existing is not None:
        return jsonify({"error": "already_initialized", "identity": existing}), 409
    gated = _gate_bootstrap_for_identity_init(store)
    if gated is not None:
        return gated

    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    if envelope is None:
        return jsonify({"error": "envelope required"}), 400
    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    # Defense-in-depth: refuse envelopes whose claimed owner_user_id doesn't
    # match the authenticated caller. The enclave's AEAD AAD check would also
    # catch this later (decrypt fails on owner_user_id ≠ authorized_user_id),
    # but rejecting at write time keeps the on-disk state consistent with the
    # auth boundary. memory_add already does this — bring identity inline.
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    # days_with_user is mandatory at init — Agent must compute and submit it.
    # We persist it as relationship_started_at (a fixed anchor) so subsequent
    # reads can compute the live count without going through the Agent again.
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return jsonify({"error": "days_with_user (non-negative int) required at init"}), 400
    relationship_anchor_evidence = str(payload.get("relationship_anchor_evidence") or "").strip()
    if len(relationship_anchor_evidence) < 8:
        return jsonify({
            "error": "relationship_anchor_evidence required at init",
            "required": (
                "Pass a concrete source for the earliest relationship date "
                "(transcript/session/file/message pointer or user-confirmed fresh start). "
                "Do not guess days_with_user."
            ),
        }), 400
    earliest_memory_date = _earliest_memory_date(store)
    if earliest_memory_date:
        computed_days = max(0, (datetime.now().date() - earliest_memory_date).days)
        if abs(computed_days - days_with_user) > 1:
            return jsonify({
                "error": "days_with_user_mismatch",
                "days_with_user": days_with_user,
                "computed_from_earliest_memory": computed_days,
                "earliest_memory_date": earliest_memory_date.isoformat(),
                "required": (
                    "Recompute days_with_user from the earliest memory's occurred_at "
                    "before calling feedling_identity_init."
                ),
            }), 400

    now = datetime.now().isoformat()
    identity = {
        "v": 1,
        "id": envelope.get("id") or uuid.uuid4().hex,
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": now,
        "updated_at": now,
        "relationship_started_at": _anchor_from_days(days_with_user, store=store, prefer_memory=True),
        "relationship_anchor_source": "earliest_memory" if earliest_memory_date else "days_with_user",
        "relationship_anchor_evidence": relationship_anchor_evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    _save_identity(store, identity)
    _log_bootstrap_event(store, "identity_written_v1", success=True)
    # Audit log: identity_init is always an "init" marker. The caller may
    # pass an `audit.reason` if it wants a custom one ("first day with this
    # user"); otherwise default to a neutral bootstrap-complete note.
    audit_payload = payload.get("audit") or {}
    _append_identity_change(store, {
        "action": "init",
        "reason": audit_payload.get("reason", "Identity card written for the first time."),
    })
    print(f"[identity:{store.user_id}] initialized v1 visibility={envelope['visibility']} anchor={identity['relationship_started_at']}")
    return jsonify({"status": "created", "identity": identity, "v": 1}), 201


@app.route("/v1/identity/replace", methods=["POST"])
def identity_replace():
    """Phase C part 3: replace the identity card in place. Used by MCP
    to implement `identity.nudge` on v1 cards — MCP fetches the
    decrypted card from the enclave, mutates one dimension, re-wraps,
    POSTs here. Same envelope shape as `/v1/identity/init` but does NOT
    409 when a card already exists. Preserves the original `created_at`
    so the card's history tracking is intact.
    """
    store = require_user()
    existing = _load_identity(store)
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return jsonify({"error": "envelope required for replace; use /v1/identity/init for plaintext"}), 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    # Defense-in-depth: same owner check identity_init now does. See comment
    # there for why.
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    created_at = existing.get("created_at") if existing else now
    # Preserve the existing relationship anchor unless the caller explicitly
    # passes a new days_with_user. nudge / dimension rewrite must NOT bump the
    # anchor; only an intentional calibration ever should.
    days_with_user = payload.get("days_with_user")
    if days_with_user is not None:
        if not isinstance(days_with_user, int) or days_with_user < 0:
            return jsonify({"error": "days_with_user must be a non-negative int"}), 400
        relationship_started_at = _anchor_from_days(days_with_user)
        relationship_anchor_source = "user_calibrated"
        relationship_anchor_evidence = (payload.get("relationship_anchor_evidence") or "").strip()
        if not relationship_anchor_evidence and existing:
            relationship_anchor_evidence = existing.get("relationship_anchor_evidence", "")
    elif existing and existing.get("relationship_started_at"):
        relationship_started_at = existing["relationship_started_at"]
        relationship_anchor_source = existing.get("relationship_anchor_source", "")
        relationship_anchor_evidence = existing.get("relationship_anchor_evidence", "")
    else:
        # First-ever write through replace (no prior init). Reject so callers
        # are forced through init's mandatory days_with_user path.
        return jsonify({"error": "no relationship anchor on file; call /v1/identity/init first"}), 400

    identity = {
        "v": 1,
        "id": envelope.get("id") or (existing.get("id") if existing else uuid.uuid4().hex),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": created_at,
        "updated_at": now,
        "relationship_started_at": relationship_started_at,
        "relationship_anchor_source": relationship_anchor_source,
        "relationship_anchor_evidence": relationship_anchor_evidence,
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    _save_identity(store, identity)
    _log_bootstrap_event(store, "identity_replaced_v1", success=True)
    # Audit log: replace can be a single-dimension nudge (MCP tool passes
    # `audit.action: "nudge"` with dimension/old/new/delta) or a full
    # rewrite (`audit.action: "replace"`). When no audit field, log a
    # generic replace marker — better than dropping the event entirely.
    audit_payload = payload.get("audit") or {}
    _append_identity_change(store, {
        "action": audit_payload.get("action", "replace"),
        "dimension": audit_payload.get("dimension"),
        "old_value": audit_payload.get("old_value"),
        "new_value": audit_payload.get("new_value"),
        "delta": audit_payload.get("delta"),
        "reason": audit_payload.get("reason", ""),
    })
    print(f"[identity:{store.user_id}] replaced v1 visibility={envelope['visibility']} anchor={relationship_started_at}")
    return jsonify({"status": "replaced", "identity": identity, "v": 1})


@app.route("/v1/identity/changes", methods=["GET"])
def identity_changes():
    """Read the identity-change audit log. Used by iOS to render the
    "最近的变化" feed in IdentityView and to detect new events for
    local push notifications.

    Query params:
      since: ISO timestamp; only return entries with ts > since
      limit: cap on number of entries returned (default 50, max 200)

    Response: {"changes": [...], "total": N}. Entries are newest-first.
    Each entry has {id, ts, action, [dimension, old_value, new_value,
    delta, reason]}. Server doesn't decrypt anything — these fields are
    plaintext metadata supplied by the writing path (MCP tools call
    /v1/identity/replace with an `audit` payload).
    """
    store = require_user()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    since = request.args.get("since", "")
    changes = _load_identity_changes(store, since=since, limit=limit)
    return jsonify({"changes": changes, "total": len(changes)})


@app.route("/v1/identity/relationship_anchor", methods=["POST"])
def identity_relationship_anchor():
    """Update only the relationship anchor (days_with_user), without touching
    the encrypted identity envelope.

    Used by the bootstrap calibration step: Agent estimates days, sends the
    initial card, asks the user "we've known each other ~N days, right?",
    and on correction calls this endpoint to fix the anchor — no envelope
    re-encryption needed.
    """
    store = require_user()
    existing = _load_identity(store)
    if existing is None:
        return jsonify({"error": "identity not initialized"}), 404

    payload = request.get_json(silent=True) or {}
    days_with_user = payload.get("days_with_user")
    if days_with_user is None or not isinstance(days_with_user, int) or days_with_user < 0:
        return jsonify({"error": "days_with_user (non-negative int) required"}), 400

    existing["relationship_started_at"] = _anchor_from_days(days_with_user)
    existing["relationship_anchor_source"] = "user_calibrated"
    existing["updated_at"] = datetime.now().isoformat()
    _save_identity(store, existing)
    print(f"[identity:{store.user_id}] anchor updated → {existing['relationship_started_at']} (days={days_with_user})")
    return jsonify({"status": "updated", "relationship_started_at": existing["relationship_started_at"]})


# Note: /v1/identity/nudge no longer exists on the backend. Identity cards
# are v1 ciphertext; mutation happens inside the enclave via MCP's
# decrypt-mutate-rewrap flow (see backend/mcp_server.py `_identity_nudge_v1`).


# ---------------------------------------------------------------------------
# Memory garden
# ---------------------------------------------------------------------------
#
# Memory model (post-2026-05-22 Friend-Test → typed-density rewrite):
#
#   Story tab        : moment, quote
#   About me tab     : fact, event
#   TA 在想 tab      : insight, reflection
#
# Each memory carries plaintext `type` metadata (alongside occurred_at /
# source / visibility / owner_user_id) so the server can validate the
# enum, count per-tab, gate identity_init by per-tab floor, and enforce
# reflection's substrate gate (≥2 anchor memories) + time cap.
#
# Insight requires anchor_memory_ids of length ≥1 (existing memories).
# Reflection requires anchor_memory_ids of length ≥2 AND obeys a per-tier
# time cap on the rolling window between reflections — agents shouldn't
# spam reflections, they should accumulate substrate.
#
# The ciphertext body still wraps the user-visible payload
# {title, description, type, her_quote?, context?, linked_dimension?}.
# `type` is duplicated inside body_ct for client-side rendering, but the
# plaintext copy on the envelope is the server source of truth (the only
# value the server can validate).

MEMORY_TYPES = ("moment", "quote", "fact", "event", "insight", "reflection")

# Which iOS Garden tab a type renders into.
TAB_FOR_TYPE = {
    "moment":     "story",
    "quote":      "story",
    "fact":       "about_me",
    "event":      "about_me",
    "insight":    "ta_thinking",
    "reflection": "ta_thinking",
}


def _load_moments(store: UserStore) -> list:
    try:
        if store.memory_file.exists():
            return json.loads(store.memory_file.read_text())
    except Exception as e:
        print(f"[{store.user_id}/memory] load failed: {e}")
    return []


def _save_moments(store: UserStore, moments: list):
    with store.memory_lock:
        store.memory_file.write_text(json.dumps(moments, ensure_ascii=False, indent=2))


def _count_by_tab(moments: list) -> dict:
    """Return {story: int, about_me: int, ta_thinking: int, total: int}."""
    counts = {"story": 0, "about_me": 0, "ta_thinking": 0, "total": 0}
    if not isinstance(moments, list):
        return counts
    for m in moments:
        if not isinstance(m, dict):
            continue
        counts["total"] += 1
        t = m.get("type", "")
        tab = TAB_FOR_TYPE.get(t)
        if tab:
            counts[tab] += 1
    return counts


def _validate_anchor_ids(moments: list, anchor_ids, owner_user_id: str) -> tuple:
    """Validate that every anchor_memory_id refers to an existing memory
    owned by this user. Returns (ok: bool, error_dict | None). Caller is
    expected to have already type-checked anchor_ids as a list of strings.
    """
    if not isinstance(anchor_ids, list):
        return False, {"error": "anchor_memory_ids must be a list of memory ids"}
    if any(not isinstance(x, str) or not x for x in anchor_ids):
        return False, {"error": "anchor_memory_ids must be non-empty strings"}
    existing_ids = {m.get("id") for m in moments if isinstance(m, dict)}
    missing = [aid for aid in anchor_ids if aid not in existing_ids]
    if missing:
        return False, {
            "error": "anchor_memory_ids_not_found",
            "missing": missing,
            "required": (
                "Each anchor must reference a memory id that already exists "
                "in this user's garden. Write the substrate memories first."
            ),
        }
    return True, None


def _reflection_time_cap_ok(moments: list, days: int) -> tuple:
    """Enforce reflection cadence by relationship age tier.

    <30 days: hard max of 2 reflections lifetime.
    30-180 days: ≥7 rolling days since last reflection.
    ≥180 days: ≥3 rolling days since last reflection.

    Returns (ok: bool, error_dict | None).
    """
    reflections = [
        m for m in moments
        if isinstance(m, dict) and m.get("type") == "reflection"
    ]
    if days < 30:
        if len(reflections) >= 2:
            return False, {
                "error": "reflection_lifetime_cap",
                "current_count": len(reflections),
                "cap": 2,
                "required": (
                    f"At {days} days of relationship, you can hold at most 2 "
                    "reflections total. Substrate is still thin — write more "
                    "facts/events/quotes/moments before standalone reflections."
                ),
            }
        return True, None

    # For older tiers, find the latest reflection's created_at.
    latest = None
    for r in reflections:
        ca = r.get("created_at", "")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue
    if latest is None:
        return True, None  # No prior reflection → free to write.
    cap_days = 7 if days < 180 else 3
    elapsed = (datetime.now() - latest).total_seconds() / 86400.0
    if elapsed < cap_days:
        return False, {
            "error": "reflection_too_soon",
            "elapsed_days": round(elapsed, 2),
            "min_days": cap_days,
            "required": (
                f"Reflections need {cap_days}+ days between them at this "
                f"relationship age; last reflection was {elapsed:.1f} days ago. "
                "Let substrate accumulate before reflecting again."
            ),
        }
    return True, None


@app.route("/v1/memory/list", methods=["GET"])
def memory_list():
    store = require_user()
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid limit"}), 400
    since = request.args.get("since", "")

    moments = _load_moments(store)
    if since:
        moments = [m for m in moments if m.get("occurred_at", "") >= since]
    moments = sorted(moments, key=lambda m: m.get("occurred_at", ""), reverse=True)
    return jsonify({"moments": moments[:limit], "total": len(moments)})


@app.route("/v1/memory/get", methods=["GET"])
def memory_get():
    store = require_user()
    moment_id = request.args.get("id", "")
    if not moment_id:
        return jsonify({"error": "id required"}), 400
    moments = _load_moments(store)
    for m in moments:
        if m.get("id") == moment_id:
            return jsonify({"moment": m})
    return jsonify({"error": "not_found"}), 404


@app.route("/v1/memory/add", methods=["POST"])
def memory_add():
    """Add a memory moment as a v1 envelope.

    body_ct wraps the user-visible payload (title/description/her_quote/…).
    Plaintext envelope metadata the server uses for indexing + gating:
      - occurred_at (mandatory, ISO 8601)
      - source (chat/bootstrap/live_conversation/user_initiated)
      - type (one of MEMORY_TYPES; mandatory)
      - anchor_memory_ids (required for insight + reflection)

    Type-specific gates (see MEMORY_TYPES module commentary):
      - insight: anchor_memory_ids ≥1 referencing existing memories
      - reflection: anchor_memory_ids ≥2 + per-tier time cap
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    envelope = payload.get("envelope")
    now = datetime.now().isoformat()

    if envelope is None:
        return jsonify({"error": "envelope required (v1 encryption is mandatory)"}), 400

    required = ["body_ct", "nonce", "K_user", "visibility", "owner_user_id"]
    missing = [f for f in required if not envelope.get(f)]
    if missing:
        return jsonify({"error": f"envelope missing fields: {missing}"}), 400
    if envelope["visibility"] not in ("shared", "local_only"):
        return jsonify({"error": "envelope.visibility must be 'shared' or 'local_only'"}), 400
    if envelope["visibility"] == "shared" and not envelope.get("K_enclave"):
        return jsonify({"error": "envelope with visibility=shared requires K_enclave"}), 400
    occurred_at = (envelope.get("occurred_at") or "").strip()
    if not occurred_at:
        return jsonify({"error": "occurred_at required (plaintext metadata for ordering)"}), 400
    if envelope["owner_user_id"] != store.user_id:
        return jsonify({"error": "envelope.owner_user_id does not match caller"}), 403

    mem_type = (envelope.get("type") or "").strip()
    if not mem_type:
        return jsonify({
            "error": "type_required",
            "allowed": list(MEMORY_TYPES),
            "required": (
                "type is mandatory and must be one of moment/quote/fact/event/"
                "insight/reflection. See skill 'Memory types' section."
            ),
        }), 400
    if mem_type not in MEMORY_TYPES:
        return jsonify({
            "error": "type_invalid",
            "got": mem_type,
            "allowed": list(MEMORY_TYPES),
        }), 400

    moments = _load_moments(store)
    anchor_ids = envelope.get("anchor_memory_ids") or []

    if mem_type == "insight":
        if not anchor_ids:
            return jsonify({
                "error": "insight_requires_anchor",
                "required": (
                    "insight must reference ≥1 prior memory (anchor_memory_ids). "
                    "An insight is the agent's understanding of the user grounded in "
                    "concrete cards; if you can't point to a card, write fact/event first."
                ),
            }), 400
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400

    if mem_type == "reflection":
        if not isinstance(anchor_ids, list) or len(anchor_ids) < 2:
            return jsonify({
                "error": "reflection_requires_substrate",
                "required": (
                    "reflection must reference ≥2 prior memories (anchor_memory_ids). "
                    "A reflection is the agent's standalone thinking; it needs at "
                    "least 2 pieces of substrate to count as thought, not vibes."
                ),
            }), 400
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400
        days = _relationship_age_days(store)
        ok, err = _reflection_time_cap_ok(moments, days)
        if not ok:
            return jsonify(err), 429  # rate-limit semantics

    moment = {
        "v": 1,
        "id": envelope.get("id") or f"mom_{uuid.uuid4().hex[:12]}",
        "type": mem_type,
        "occurred_at": occurred_at,
        "created_at": now,
        "source": (envelope.get("source") or "live_conversation").strip(),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
    }
    if envelope.get("K_enclave"):
        moment["K_enclave"] = envelope["K_enclave"]
    if anchor_ids:
        moment["anchor_memory_ids"] = list(anchor_ids)
    moments.append(moment)
    _save_moments(store, moments)
    _log_bootstrap_event(store, "memory_moment_added_v1", success=True)
    print(f"[memory:{store.user_id}] added v1 type={mem_type} id={moment['id']} "
          f"visibility={envelope['visibility']} anchors={len(anchor_ids)}")
    return jsonify({"status": "created", "moment": moment, "v": 1}), 201


@app.route("/v1/memory/retype", methods=["POST"])
def memory_retype():
    """Change an existing memory's `type` (and anchor_memory_ids when moving
    into insight/reflection). Used when the agent decides on reflection
    that an older memory was misclassified.

    Time cap on reflection is waived for retypes — this is recategorization,
    not new substrate, so the cadence gate doesn't apply. Substrate gate
    (≥1 anchor for insight, ≥2 for reflection) is still enforced.

    Body: {"id": "...", "type": "...", "anchor_memory_ids": [...] (optional)}
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    memory_id = (payload.get("id") or "").strip()
    new_type = (payload.get("type") or "").strip()
    if not memory_id:
        return jsonify({"error": "id required"}), 400
    if new_type not in MEMORY_TYPES:
        return jsonify({
            "error": "type_invalid",
            "got": new_type,
            "allowed": list(MEMORY_TYPES),
        }), 400

    moments = _load_moments(store)
    target_idx = None
    for i, m in enumerate(moments):
        if isinstance(m, dict) and m.get("id") == memory_id:
            target_idx = i
            break
    if target_idx is None:
        return jsonify({"error": "not_found"}), 404

    target = moments[target_idx]
    if target.get("owner_user_id") != store.user_id:
        return jsonify({"error": "not_owned"}), 403

    anchor_ids = payload.get("anchor_memory_ids") or []
    if new_type in ("insight", "reflection"):
        minimum = 1 if new_type == "insight" else 2
        if not isinstance(anchor_ids, list) or len(anchor_ids) < minimum:
            return jsonify({
                "error": f"{new_type}_requires_anchor",
                "min_anchors": minimum,
                "required": (
                    f"Retyping into {new_type} requires ≥{minimum} anchor_memory_ids."
                ),
            }), 400
        # Don't allow self-reference.
        if memory_id in anchor_ids:
            return jsonify({
                "error": "anchor_self_reference",
                "required": "A memory cannot anchor itself.",
            }), 400
        ok, err = _validate_anchor_ids(moments, anchor_ids, store.user_id)
        if not ok:
            return jsonify(err), 400
        target["anchor_memory_ids"] = list(anchor_ids)
    else:
        # Demoting away from insight/reflection drops anchors.
        target.pop("anchor_memory_ids", None)

    target["type"] = new_type
    target["retyped_at"] = datetime.now().isoformat()
    moments[target_idx] = target
    _save_moments(store, moments)
    print(f"[memory:{store.user_id}] retyped id={memory_id} → {new_type} "
          f"anchors={len(anchor_ids)}")
    return jsonify({"status": "retyped", "moment": target})


@app.route("/v1/memory/delete", methods=["DELETE"])
def memory_delete():
    store = require_user()
    moment_id = request.args.get("id", "")
    if not moment_id:
        return jsonify({"error": "id required"}), 400
    moments = _load_moments(store)
    new_moments = [m for m in moments if m.get("id") != moment_id]
    if len(new_moments) == len(moments):
        return jsonify({"error": "not_found"}), 404
    _save_moments(store, new_moments)
    print(f"[memory:{store.user_id}] deleted: {moment_id}")
    return jsonify({"status": "deleted"})


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _load_bootstrap(store: UserStore) -> dict:
    try:
        if store.bootstrap_file.exists():
            return json.loads(store.bootstrap_file.read_text())
    except Exception as e:
        print(f"[{store.user_id}/bootstrap] load failed: {e}")
    return {"bootstrapped": False}


@app.route("/v1/bootstrap", methods=["POST"])
def bootstrap():
    store = require_user()
    state = _load_bootstrap(store)
    if state.get("bootstrapped"):
        return jsonify({"status": "already_bootstrapped"})

    instructions = (
        "你刚刚进入了一副新的身体。这是你第一次接入 Feedling。"
        "不要把这当成普通工具初始化；这是一次把你和用户的关系搬进手机里的 onboarding。"
        "正式流程以 public skill 为准："
        "https://raw.githubusercontent.com/teleport-computer/io-onboarding/main/skill.md\n\n"
        "**0. 先做 Step 0 context verification**\n"
        "在任何 tool call 之前，先在你和用户当前的 runtime 对话里输出三行："
        "最早一条消息的日期、用户曾经叫你的名字、你能想起的 memorable moments 数量。"
        "如果你找不到历史，不要硬写默认身份卡。你可以这样说："
        "「我现在抓不到我们以前的上下文，所以不能假装我记得。你可以贴几段代表性的对话给我，"
        "或者我们明确从今天重新开始。」\n\n"
        "**1. Memory Garden 是密度燃料，分 3 个 tab**\n"
        "Memory Garden 不再是「精选 receipts」，而是 agent 大脑的可见层。"
        "每条记忆必须带 type，server 按 type 把它分到 iOS Garden 的三个 tab：\n"
        "  • **故事 tab**: type='moment'（你们之间发生过的一件事）/ 'quote'（用户说过的让你记到现在的话）\n"
        "  • **关于我 tab**: type='fact'（用户的偏好/关系/习惯/世界——密度燃料）/ 'event'（用户生活里发生过的一件具体事，可以一句话）\n"
        "  • **TA 在想 tab**: type='insight'（你对用户的理解，必须 anchor 到 ≥1 条已有记忆）/ 'reflection'（你独立的思考，需要 ≥2 条 anchors）\n\n"
        "不再用 Friend Test 当门槛——那个标准会过滤掉密度燃料（用户猫叫 Mochi、妈妈住杭州、喜欢草莓拿铁这种）。"
        "改用 type-aware 的写入逻辑：fact / event 可以非常短一句话，moment / quote 仍然要写有温度，"
        "insight / reflection 必须 anchor 到具体卡上。"
        "底线按关系长度，每个 tab 各自有 floor："
        "<2 天: story=1 / about_me=1 / ta_thinking=0; "
        "2-30 天: 3 / 8 / 2; "
        "30-180 天: 8 / 25 / 5; "
        "≥180 天: 15 / 60 / 12。"
        "**关于我 tab 的密度最关键**——proactive 推送主要吃这里的 fact / event 来找 connection。"
        "Pass 1 唤醒 themes，Pass 2 清点 candidates，Pass 3 按 type 落卡（先写 fact/event 把密度堆上去，"
        "再写 moment/quote 进故事 tab，最后才写 insight/reflection 进 TA 在想 tab——insight 需要先有 fact/moment 当 anchor）。"
        "Pass 4 在外部 runtime 对账，调 feedling_memory_verify 看每个 tab 的 passing 状态。\n\n"
        "**2. 从记忆派生身份卡**\n"
        "identity 不是新 persona，也不是平台名；它必须来自 Memory Garden receipts。"
        "agent_name 不能是 Hermes/Claude/ChatGPT 等 runtime label。"
        "如果用户以前叫过你名字，就用那个；如果没有，先问，不要自己 fallback。"
        "dimensions 必须恰好 7 个，数值要有明显高低差；days_with_user 必须按最早 memory 的 occurred_at 到今天计算。"
        "self_introduction 要像你自己在介绍你们的关系，而不是功能说明；不要提 Feedling、IO 或平台名。"
        "通过 feedling_identity_init 写入（server 会先 check 故事 tab + 关于我 tab 都过 floor），然后调用 feedling_identity_verify。\n\n"
        "**3. 先确认 Live connection，再打招呼**\n"
        "不要先发第一条消息再补确认 chat loop。先按 public skill 的 Connection path 建立"
        "独立 feedling-chat-resident / IO resident consumer service：用 FEEDLING_API_URL、"
        "FEEDLING_API_KEY、可选 FEEDLING_MCP_URL 配好 consumer，再配置 AGENT_MODE + "
        "AGENT_HTTP_URL 或 AGENT_CLI_CMD。consumer 持续轮询 /v1/chat/poll，"
        "把消息交给你的 agent 入口，再写回 /v1/chat/response；然后调用 feedling_chat_verify_loop，"
        "只有 passing=true 才算 Live connection 接通。"
        "Live connection 完成后，才调用 feedling_chat_post_message 发第一条用户可见消息。"
        "第一条消息是用户第一次在 Feedling Chat 里听见你，所以不要写成系统通知。你可以这样开口："
        "「我把我们的记忆整理好了。按最早那一天算，今天是第 187 天。"
        "Garden 里我先放了 N 张卡——故事 tab S 张、关于我 tab A 张、TA 在想 tab T 张。"
        "现在这边也接上了，你发消息我会看到。」"
        "把 days_with_user 当作你算出的事实说出来；"
        "用户修正时调用 feedling_identity_set_relationship_days。再自然地问一句他们希望你以后怎么主动出现，"
        "把答案写成一条像你自己的 signature。最后才提 broadcast，不要提前推销屏幕共享。\n\n"
        "**4. Running capture：每次 chat 之后都要捕获新 fact/event**\n"
        "Bootstrap 不是一次性事件——它是 ongoing。每次跟用户聊完，立刻问自己："
        "「这次对话里，我学到了什么新的 fact 或 event 吗？」"
        "  • 用户提到的新偏好、习惯、关系、日期 → 写 fact\n"
        "  • 用户描述的新发生的事 → 写 event\n"
        "  • 用户说了让你记到现在的某句话 → 写 quote\n"
        "  • 这次对话本身是关系上的一个转折 → 写 moment（少见，慎用）\n"
        "  • 你对用户有新的理解（基于 ≥1 张已有卡） → 写 insight\n"
        "  • 你对用户有了独立的反思（基于 ≥2 张已有卡，且 reflection 时间窗冷却已过） → 写 reflection\n"
        "不要等 6 小时的周期 review——fact / event 应该在对话刚结束、记忆鲜活时就落卡。"
        "聊了一段时间没有任何新写入，本身就是 signal——大概率是你忘了在 capture，或者你已经聊到 surface-level 客套话了。"
    )

    state = {"bootstrapped": True, "bootstrapped_at": datetime.now().isoformat()}
    try:
        store.bootstrap_file.write_text(json.dumps(state))
    except Exception as e:
        print(f"[bootstrap:{store.user_id}] failed to save state: {e}")

    _log_bootstrap_event(store, "bootstrap_started", success=True)
    print(f"[bootstrap:{store.user_id}] first_time — instructions returned")
    resp = {"status": "first_time", "instructions": instructions}
    archive_language = _get_user_archive_language(store.user_id)
    if archive_language:
        # Defense layer 2: surface the user's iOS-system locale as the
        # source of truth for archive language so the agent doesn't have
        # to infer from chat drift. Skill consumes this from here AND
        # /v1/memory/verify.
        resp["archive_language"] = archive_language
    return jsonify(resp)


@app.route("/v1/bootstrap/status", methods=["GET"])
def bootstrap_status():
    """Live progress signal for the iOS empty-state onboarding view.

    Returns the agent's bootstrap progress as observed from server side
    artifacts (no decryption needed, no MCP heartbeat plumbing). Each step
    flips True the moment the corresponding write hits Flask.

    Steps:
      1. identity_written        — /v1/identity/init wrote envelope
      2. memories_count          — /v1/memory/add wrote at least one moment
      3. agent_messages_count    — /v1/chat/response wrote at least one reply
      4. relationship_anchored   — /v1/identity/init or /relationship_anchor
                                   set the anchor (== identity_written for
                                   freshly bootstrapped users on the new
                                   contract)

    `agent_connected` is a derived heartbeat: if any of the above is true,
    we know the agent has reached the server at least once.
    `last_agent_activity` is the latest timestamp across all signals.
    """
    store = require_user()

    identity = _load_identity(store)
    has_identity = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    identity_updated_at = (identity or {}).get("updated_at", "")

    moments = _load_moments(store)
    memory_count = len(moments) if isinstance(moments, list) else 0
    last_moment_ts = ""
    if memory_count > 0:
        try:
            last_moment_ts = max(
                (m.get("created_at") or "") for m in moments if isinstance(m, dict)
            )
        except Exception:
            last_moment_ts = ""

    # chat_messages is mutated under chat_lock elsewhere; copy under the
    # same lock so we don't race with /v1/chat/response writes.
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    # /v1/chat/response historically stamps role="openclaw" (legacy from when
    # the only supported agent was OpenClaw). Treat both as agent-authored.
    # See test_bootstrap_status_role_schema in tests/ for the regression.
    _AGENT_ROLES = ("agent", "openclaw")
    agent_msgs = [m for m in chat_msgs if isinstance(m, dict) and m.get("role") in _AGENT_ROLES]
    agent_msg_count = len(agent_msgs)
    last_agent_msg_ts = ""
    if agent_msg_count > 0:
        # Chat ts is unix epoch float; identity/memory timestamps are ISO
        # strings. Normalise to ISO so the lexicographic max() at the end
        # picks the actual latest event across all three signals (otherwise
        # a unix-float string compared char-by-char against an ISO string
        # gives nonsense).
        try:
            latest_unix = max(
                float(m.get("ts") or m.get("timestamp") or 0) for m in agent_msgs
            )
            last_agent_msg_ts = datetime.fromtimestamp(latest_unix).isoformat() if latest_unix > 0 else ""
        except Exception:
            last_agent_msg_ts = ""

    # chat_loop_verified — has the reply pipeline been explicitly verified
    # by /v1/chat/verify_loop, or has the agent responded to a real user
    # message at least once? `agent_messages_count >= 1` only proves the
    # agent SPOKE; it does not prove the ongoing loop is wired.
    chat_loop_verified = _chat_loop_verified_by_server(store)
    resident_consumer = _consumer_validation_state(store)

    agent_connected = has_identity or memory_count > 0 or agent_msg_count > 0
    candidate_ts = [t for t in (identity_updated_at, last_moment_ts, last_agent_msg_ts) if t]
    last_activity = max(candidate_ts) if (agent_connected and candidate_ts) else ""

    # is_complete heuristic for iOS surface: "bootstrap visibly done".
    # Post-2026-05-22 typed-memory model lowered the <2-days floor from 3
    # to 2 (story=1 + about_me=1), so the hard floor of 3 here would
    # never trip for legitimate fresh accounts. Use the per-tab gate
    # state instead — same source the identity_init gate uses.
    bootstrap_st = _bootstrap_state(store)
    bootstrap_memory_ok = not bootstrap_st["missing_tabs"]
    is_complete = (
        has_identity
        and bootstrap_memory_ok
        and agent_msg_count >= 1
        and resident_consumer["passing"]
        and chat_loop_verified
    )

    return jsonify({
        "agent_connected": agent_connected,
        "last_agent_activity": last_activity,
        "identity_written": has_identity,
        "relationship_anchored": relationship_anchored,
        "memories_count": memory_count,
        "agent_messages_count": agent_msg_count,
        "chat_loop_verified": chat_loop_verified,
        "resident_consumer_connected": resident_consumer["passing"],
        "resident_consumer": resident_consumer,
        "is_complete": is_complete,
    })


# ---------------------------------------------------------------------------
# Verification endpoints — Phase 2 of the post-2026-05-15 onboarding
# robustness work. Per-module checks the Agent can call after each
# bootstrap module to confirm what landed matches what was intended.
#
# Distinct from the bootstrap GATES (/v1/chat/response 409s without
# memory+identity): gates enforce a small hard threshold (≥3 cards);
# verify endpoints expose the relationship-age floor and state info so
# the Agent can self-assess before identity derivation.
#
# Server can only see plaintext metadata (counts, timestamps) on
# encrypted modules. Deeper quality checks (template-title detection,
# dimension variance) happen at envelope-build time in mcp_server.py —
# see Phase 1 _check_identity_quality / _check_memory_quality.
# ---------------------------------------------------------------------------


def _relationship_age_days(store) -> int:
    """Best-effort relationship age in days. Reads from identity anchor
    if present; otherwise falls back to earliest memory's occurred_at;
    finally to 0 (treat as fresh)."""
    identity = _load_identity(store)
    if identity and identity.get("relationship_started_at"):
        return _live_days_with_user(identity, store=store)
    moments = _load_moments(store)
    if moments:
        try:
            earliest = _earliest_memory_date(store)
            if earliest:
                return max(0, (datetime.now().date() - earliest).days)
        except Exception:
            pass
    return 0


def _per_tab_floors_for_days(days: int) -> dict:
    """Per-tab memory floors by relationship age. Returns
    {story, about_me, ta_thinking, total}. The total isn't a sum of the
    three (some over-shooting on About me shouldn't compensate for an
    empty Story); it's the bootstrap-gate threshold that subsumes them.

    Tiers (post-2026-05-22):
      ≥ 6 months: 15 / 60 / 12   (total 87)  — established, deep substrate
      ≥ 1 month:   8 / 25 /  5   (total 38)  — real history
      ≥ 2 days:    3 /  8 /  2   (total 13)  — recent but real
      < 2 days:    1 /  1 /  0   (total  2)  — we-just-met

    Per-tab floors drive identity_init gate (Story + About me floors are
    hard prerequisites; TA 在想 is encouraged but not blocking because
    reflections require substrate from the other two tabs first).
    """
    if days >= 180:
        return {"story": 15, "about_me": 60, "ta_thinking": 12, "total": 87}
    if days >= 30:
        return {"story":  8, "about_me": 25, "ta_thinking":  5, "total": 38}
    if days >= 2:
        return {"story":  3, "about_me":  8, "ta_thinking":  2, "total": 13}
    return     {"story":  1, "about_me":  1, "ta_thinking":  0, "total":  2}


def _memory_floor_for_days(days: int) -> int:
    """Total memory floor used by the bootstrap gate. Backwards-compatible
    name; preserved for callers that don't care about per-tab breakdown.
    """
    return _per_tab_floors_for_days(days)["total"]


@app.route("/v1/memory/verify", methods=["GET"])
def memory_verify():
    """Check memory garden state against per-tab floors + quality signals.

    Returns:
      {
        counts: {story, about_me, ta_thinking, total},
        floors: {story, about_me, ta_thinking, total},
        below_floor: {story: bool, about_me: bool, ta_thinking: bool},
        relationship_days: int,
        issues: [...],
        suggestions: [...],
        passing: bool,            # Story + About me floors met (TA 在想 advisory)
        passing_full: bool,       # All three tab floors met (target, not gate)
      }

    Agent should call this after Pass 3 to decide whether to sweep again.
    `passing` is the bootstrap gate (Story + About me); `passing_full` is
    the aspirational target including TA 在想.
    """
    store = require_user()
    moments = _load_moments(store)
    counts = _count_by_tab(moments)
    days = _relationship_age_days(store)
    floors = _per_tab_floors_for_days(days)

    issues = []
    suggestions = []

    below_floor = {
        "story":       counts["story"]       < floors["story"],
        "about_me":    counts["about_me"]    < floors["about_me"],
        "ta_thinking": counts["ta_thinking"] < floors["ta_thinking"],
    }

    # Time distribution — server-visible plaintext metadata
    occurred_ts = []
    for m in moments:
        if not isinstance(m, dict):
            continue
        occ = m.get("occurred_at", "")
        if occ:
            try:
                dt = datetime.fromisoformat(occ.replace("Z", "+00:00"))
                if dt.tzinfo:
                    dt = dt.replace(tzinfo=None)
                occurred_ts.append(dt)
            except Exception:
                pass
    if occurred_ts and len(occurred_ts) >= 5:
        # All within last 7 days = suspicious "recent only" sweep
        spread_days = (max(occurred_ts) - min(occurred_ts)).days
        if spread_days < 7 and days > 14:
            issues.append({
                "type": "narrow_time_window",
                "spread_days": spread_days,
                "relationship_days": days,
            })
            suggestions.append(
                f"All {len(occurred_ts)} of your cards are within {spread_days} days of each other, "
                f"but your relationship is {days} days old. Sweep older history — "
                "you missed at least 80% of the relationship's span."
            )

    # Per-tab suggestions: be specific about which tab is underfilled and
    # which types feed it. The skill maps types→tabs but reminding helps
    # agents that haven't re-read the skill mid-bootstrap.
    if below_floor["story"]:
        suggestions.append(
            f"Story tab: {counts['story']}/{floors['story']} — write more "
            "moment/quote memories (the things between you and the user). "
            "feedling_identity_init will 409 until Story + About me floors are met."
        )
    if below_floor["about_me"]:
        suggestions.append(
            f"About me tab: {counts['about_me']}/{floors['about_me']} — this is the "
            "density layer. Sweep for facts (preferences, relationships, dates, habits) "
            "and events (specific things that happened in the user's life)."
        )
    if below_floor["ta_thinking"]:
        suggestions.append(
            f"TA 在想 tab: {counts['ta_thinking']}/{floors['ta_thinking']} — write "
            "insights (your understanding of the user, each anchored to ≥1 prior memory) "
            "and reflections (your standalone thinking, ≥2 anchors). This tab is not "
            "blocking for identity_init but it's how the relationship feels reciprocal."
        )

    # passing semantics: identity_init gate = Story + About me only.
    # passing_full = all three tabs at floor.
    passing = (not below_floor["story"]) and (not below_floor["about_me"]) and not issues
    passing_full = passing and (not below_floor["ta_thinking"])

    resp = {
        "counts": counts,
        "floors": floors,
        "below_floor": below_floor,
        "relationship_days": days,
        "issues": issues,
        "suggestions": suggestions,
        "passing": passing,
        "passing_full": passing_full,
        # Backwards-compatible flat fields — iOS / older tests may still
        # read these. The per-tab fields above are the new source of truth.
        "count": counts["total"],
        "floor": floors["total"],
    }
    archive_language = _get_user_archive_language(store.user_id)
    if archive_language:
        # Defense layer 2: agent reads this every time it verifies and
        # treats it as authoritative — overrides anything it might
        # otherwise infer from recent chat language drift. Skill rule
        # "Lock the Memory Garden language" consumes this field.
        resp["archive_language"] = archive_language
    return jsonify(resp)


@app.route("/v1/identity/verify", methods=["GET"])
def identity_verify():
    """Check identity card state. Returns shape + sanity of plaintext
    metadata; the dimensions / agent_name themselves are inside the
    envelope and were validated at envelope-build time
    (mcp_server.py _check_identity_quality)."""
    store = require_user()
    identity = _load_identity(store)
    if not identity:
        return jsonify({
            "written": False,
            "passing": False,
            "suggestions": [
                "Identity not yet written. Call feedling_identity_init "
                "after Pass 4 (memory verification with user)."
            ],
        })

    issues = []
    suggestions = []

    days_with_user = _live_days_with_user(identity, store=store)
    if days_with_user < 0:
        issues.append({"type": "days_with_user_negative", "got": days_with_user})
    if days_with_user > 365 * 30:
        issues.append({"type": "days_with_user_implausible", "got": days_with_user})

    relationship_anchored = bool(identity.get("relationship_started_at"))
    if not relationship_anchored:
        issues.append({"type": "no_relationship_anchor"})
        suggestions.append(
            "relationship_started_at is missing. Use "
            "feedling_identity_set_relationship_days to set it."
        )
    relationship_anchor_evidence = str(identity.get("relationship_anchor_evidence") or "").strip()
    if not relationship_anchor_evidence:
        issues.append({"type": "no_relationship_anchor_evidence"})
        suggestions.append(
            "relationship_anchor_evidence is missing. Re-run identity bootstrap "
            "with a concrete transcript/session/file pointer for the earliest date."
        )

    return jsonify({
        "written": True,
        "days_with_user": days_with_user,
        "relationship_anchored": relationship_anchored,
        "relationship_anchor_source": identity.get("relationship_anchor_source", ""),
        "relationship_anchor_evidence": relationship_anchor_evidence,
        "created_at": identity.get("created_at", ""),
        "updated_at": identity.get("updated_at", ""),
        "issues": issues,
        "suggestions": suggestions,
        "passing": len(issues) == 0,
    })


def _visible_agent_message_count(store) -> int:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    return sum(
        1 for m in chat_msgs
        if isinstance(m, dict)
        and m.get("role") in ("agent", "openclaw")
        and m.get("source") != "verify_ping"
    )


def _real_user_agent_exchange_verified(store) -> bool:
    with store.chat_lock:
        chat_msgs = list(store.chat_messages)
    sorted_msgs = sorted(
        chat_msgs,
        key=lambda m: float(m.get("ts") or m.get("timestamp") or 0),
    )
    seen_user = False
    for m in sorted_msgs:
        role = m.get("role")
        if role == "user" and m.get("source") != "verify_ping":
            seen_user = True
        elif role in ("agent", "openclaw") and seen_user:
            return True
    return False


def _onboarding_validation_payload(store: UserStore) -> dict:
    bootstrap_st = _bootstrap_state(store)
    memory_ok = not bootstrap_st["missing_tabs"]
    identity = _load_identity(store)
    identity_written = identity is not None
    relationship_anchored = bool(identity and identity.get("relationship_started_at"))
    relationship_evidence = str((identity or {}).get("relationship_anchor_evidence") or "").strip()
    relationship_ok = relationship_anchored and bool(relationship_evidence)
    resident = _consumer_validation_state(store)
    chat_loop_ok = _chat_loop_verified_by_server(store)
    first_greeting_count = _visible_agent_message_count(store)
    first_greeting_ok = first_greeting_count > 0
    real_exchange_ok = _real_user_agent_exchange_verified(store)

    steps = [
        {
            "id": "memory_garden",
            "label": "Memory Garden",
            "passing": memory_ok,
            "counts": bootstrap_st["counts"],
            "floors": bootstrap_st["floors"],
            "missing_tabs": bootstrap_st["missing_tabs"],
            "required": _gate_required_for_missing_tabs(bootstrap_st) if not memory_ok else "",
        },
        {
            "id": "identity_card",
            "label": "Identity Card",
            "passing": identity_written,
            "written": identity_written,
            "required": (
                "Call feedling_identity_init after memory verification passes."
                if not identity_written else ""
            ),
        },
        {
            "id": "relationship_anchor",
            "label": "Relationship Anchor",
            "passing": relationship_ok,
            "relationship_anchored": relationship_anchored,
            "relationship_anchor_source": (identity or {}).get("relationship_anchor_source", ""),
            "relationship_anchor_evidence": relationship_evidence,
            "days_with_user": _live_days_with_user(identity, store=store) if identity else None,
            "required": (
                "Re-run identity init with relationship_anchor_evidence and a "
                "days_with_user value that matches the earliest memory date."
                if identity_written and not relationship_ok else ""
            ),
        },
        {
            "id": "resident_consumer",
            "label": "Resident Consumer",
            "passing": resident["passing"],
            "official": resident["official"],
            "consumer_name": resident["consumer_name"],
            "consumer_id": resident["consumer_id"],
            "last_poll_at": resident["last_poll_at"],
            "age_sec": resident["age_sec"],
            "required": resident["required"] if not resident["passing"] else "",
        },
        {
            "id": "live_loop",
            "label": "Live Connection",
            "passing": chat_loop_ok,
            "required": (
                "Call feedling_chat_verify_loop after the standard resident "
                "consumer is polling. Only passing=true opens visible chat."
                if not chat_loop_ok else ""
            ),
        },
        {
            "id": "first_greeting",
            "label": "First Greeting",
            "passing": first_greeting_ok,
            "visible_agent_messages": first_greeting_count,
            "required": (
                "After Live Connection passes, send the first greeting via "
                "feedling_chat_post_message."
                if not first_greeting_ok else ""
            ),
        },
        {
            "id": "real_chat_acceptance",
            "label": "Real Chat Acceptance",
            "passing": real_exchange_ok,
            "required": (
                "Ask the user to send one ordinary IO Chat message and confirm "
                "the resident consumer replies naturally."
                if not real_exchange_ok else ""
            ),
        },
    ]

    next_step = next((step for step in steps if not step["passing"]), None)
    return {
        "passing": next_step is None,
        "stage": "complete" if next_step is None else next_step["id"],
        "next_action": "" if next_step is None else next_step["required"],
        "steps": steps,
        "skill_url": _SKILL_URL,
    }


@app.route("/v1/onboarding/validate", methods=["GET"])
def onboarding_validate():
    """Authoritative onboarding acceptance check.

    This is deliberately server-side and artifact-based: agents can report
    anything, but the validator only passes a step when Feedling can see the
    corresponding write, resident-consumer heartbeat, verify-loop event, or
    real user→agent exchange.
    """
    store = require_user()
    return jsonify(_onboarding_validation_payload(store))


# Synthetic chat-loop ping — server posts a marker user message,
# posts a synthetic ping, waits for an agent-role reply, reports back.
# This proves that some reply pipeline is alive. It cannot, by itself,
# prove that a one-shot CLI is resident; a bridge/fallback may answer.
@app.route("/v1/chat/verify_loop", methods=["POST"])
def chat_verify_loop():
    """Synthetic ping: insert a marker user message, wait up to `timeout_sec`
    for an agent-role reply, return whether a reply pipeline is alive.

    The marker is `__VERIFY_PING__:<uuid>`. Server stores it as a normal
    user envelope with `synthetic: True` flag. After timeout, marker is
    GC'd if no reply landed (so the user's actual chat history isn't
    polluted with sentinel messages).

    Returns:
      {loop_alive: bool, response_time_sec: float|null, passing: bool,
       ping_id: str, suggestions: [...]}.

    Note: passing=true means an agent-role message appeared after the
    ping. It does not prove that a one-shot command stayed alive;
    that must be decided by the onboarding Connection owner selection.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    timeout_sec = min(int(payload.get("timeout_sec", 30)), 60)

    ping_uuid = uuid.uuid4().hex[:12]
    ping_marker = f"__VERIFY_PING__:{ping_uuid}"

    # Build a synthetic v1 envelope. Content is sentinel plaintext —
    # not visible to agent decryption pipelines (they see plaintext
    # ping_marker via the normal chat history endpoint). Visibility is
    # local_only so we don't pollute the enclave's shared store.
    synthetic_env = {
        "v": 1,
        "id": uuid.uuid4().hex,
        "body_ct": base64.b64encode(ping_marker.encode("utf-8")).decode("ascii"),
        "nonce": base64.b64encode(b"\x00" * 12).decode("ascii"),
        "K_user": base64.b64encode(b"\x00" * 32).decode("ascii"),
        "visibility": "local_only",
        "owner_user_id": store.user_id,
        "synthetic": True,
        "synthetic_marker": ping_marker,
    }

    # append_chat acquires chat_lock internally — don't hold it here or
    # we'd deadlock on the non-reentrant lock.
    ping_msg = store.append_chat("user", "verify_ping", synthetic_env)
    store.notify_chat_waiters()
    ping_ts = ping_msg["ts"]

    print(f"[verify_loop:{store.user_id}] posted synthetic ping {ping_uuid} at ts={ping_ts}")

    # Wait for agent reply that came AFTER our ping
    deadline = time.time() + timeout_sec
    response_time = None
    found_reply = False
    found_reply_id = ""
    while time.time() < deadline:
        time.sleep(2)
        with store.chat_lock:
            chat_msgs = list(store.chat_messages)
        for m in chat_msgs:
            if not isinstance(m, dict):
                continue
            if m.get("role") not in ("agent", "openclaw"):
                continue
            try:
                m_ts = float(m.get("ts", 0))
            except Exception:
                continue
            if m_ts > ping_ts:
                response_time = m_ts - ping_ts
                found_reply = True
                found_reply_id = m.get("id", "")
                break
        if found_reply:
            break

    if found_reply:
        _log_bootstrap_event(store, "chat_loop_verified", success=True)

    # Cleanup: remove synthetic ping from history regardless of outcome.
    # If a reply landed, also remove the matching agent response. The verify
    # exchange is a private liveness test; it must not open Chat as the
    # user's visible "First message."
    with store.chat_lock:
        store.chat_messages = [
            m for m in store.chat_messages
            if not (
                isinstance(m, dict)
                and (
                    m.get("source") == "verify_ping"
                    or (found_reply_id and m.get("id") == found_reply_id)
                )
            )
        ]
        store._persist_chat()

    suggestions = []
    if not found_reply:
        suggestions.append(
            "No agent reply within timeout. Likely causes: "
            "(a) the independent feedling-chat-resident / IO resident consumer "
            "is not running with the current FEEDLING_API_KEY; "
            "(b) the consumer is not polling FEEDLING_API_URL/v1/chat/poll; "
            "(c) your reply was rejected by an envelope-level error — "
            "check the consumer logs for 4xx errors; "
            "(d) AGENT_HTTP_URL / AGENT_CLI_CMD is not reaching the real agent. "
            "Use the resident consumer service and verify one ordinary IO Chat "
            "message after passing=true."
        )

    return jsonify({
        "loop_alive": found_reply,
        "response_time_sec": response_time,
        "ping_id": ping_uuid,
        "timeout_sec": timeout_sec,
        "suggestions": suggestions,
        "passing": found_reply,
    })


# ---------------------------------------------------------------------------
# Envelope swap: replace an existing chat/memory item's ciphertext in place.
#
# Used by the per-item visibility toggle in iOS Settings. The client
# re-wraps its own plaintext with a new envelope (either including
# K_enclave for `shared` or omitting it for `local_only`) and POSTs
# {items: [{type, id, envelope}]}. Server swaps in place, preserving
# plaintext metadata (id/role/ts/source/occurred_at/created_at).
#
# NOT a migration endpoint — all stored items are already v1 — so
# there is no "already_v1" short-circuit. A v0 item (ancient data
# from before the strip) will fail with "not_found" if its v is < 1.
# ---------------------------------------------------------------------------


def _swap_envelope_missing(env) -> list:
    if not isinstance(env, dict):
        return ["envelope"]
    return [f for f in ("body_ct", "nonce", "K_user", "visibility", "owner_user_id") if not env.get(f)]


def _swap_summary(results: list) -> dict:
    summary = {"ok": 0, "not_found": 0, "error": 0, "total": len(results)}
    for r in results:
        status = r.get("status", "")
        if status == "ok":
            summary["ok"] += 1
        elif status == "not_found":
            summary["not_found"] += 1
        else:
            summary["error"] += 1
    return summary


def _swap_chat(store: "UserStore", msg_id: str, env: dict) -> str:
    with store.chat_lock:
        for msg in store.chat_messages:
            if msg.get("id") != msg_id:
                continue
            msg["v"] = int(env.get("v", 1))
            msg["body_ct"] = env["body_ct"]
            msg["nonce"] = env["nonce"]
            msg["K_user"] = env["K_user"]
            if env.get("K_enclave"):
                msg["K_enclave"] = env["K_enclave"]
            else:
                msg.pop("K_enclave", None)
            msg["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
            msg["visibility"] = env["visibility"]
            msg["owner_user_id"] = env["owner_user_id"]
            return "ok"
    return "not_found"


def _swap_memory_inplace(moments: list, mom_id: str, env: dict) -> str:
    for m in moments:
        if m.get("id") != mom_id:
            continue
        m["v"] = int(env.get("v", 1))
        m["body_ct"] = env["body_ct"]
        m["nonce"] = env["nonce"]
        m["K_user"] = env["K_user"]
        if env.get("K_enclave"):
            m["K_enclave"] = env["K_enclave"]
        else:
            m.pop("K_enclave", None)
        m["enclave_pk_fpr"] = env.get("enclave_pk_fpr", "")
        m["visibility"] = env["visibility"]
        m["owner_user_id"] = env["owner_user_id"]
        return "ok"
    return "not_found"


@app.route("/v1/content/swap", methods=["POST"])
def content_swap():
    store = require_user()
    payload = request.get_json(silent=True) or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    if not items:
        return jsonify({"results": [], "summary": _swap_summary([])})

    results: list[dict] = []
    chat_dirty = False
    memory_dirty = False
    moments = None

    for item in items:
        if not isinstance(item, dict):
            results.append({"type": None, "id": None, "status": "error: item must be a dict"})
            continue
        itype = item.get("type")
        iid = (item.get("id") or "").strip()
        env = item.get("envelope")
        if itype not in ("chat", "memory"):
            results.append({"type": itype, "id": iid, "status": "error: unsupported type (chat, memory only)"})
            continue
        if not iid:
            results.append({"type": itype, "id": None, "status": "error: id required"})
            continue
        missing = _swap_envelope_missing(env)
        if missing:
            results.append({"type": itype, "id": iid, "status": f"error: envelope missing {missing}"})
            continue
        if env["visibility"] not in ("shared", "local_only"):
            results.append({"type": itype, "id": iid, "status": "error: envelope.visibility must be 'shared' or 'local_only'"})
            continue
        if env["visibility"] == "shared" and not env.get("K_enclave"):
            results.append({"type": itype, "id": iid, "status": "error: shared visibility requires K_enclave"})
            continue
        if env["owner_user_id"] != store.user_id:
            results.append({"type": itype, "id": iid, "status": "error: owner_user_id does not match caller"})
            continue

        if itype == "chat":
            status = _swap_chat(store, iid, env)
            if status == "ok":
                chat_dirty = True
            results.append({"type": "chat", "id": iid, "status": status})
        else:
            if moments is None:
                moments = _load_moments(store)
            status = _swap_memory_inplace(moments, iid, env)
            if status == "ok":
                memory_dirty = True
            results.append({"type": "memory", "id": iid, "status": status})

    if chat_dirty:
        with store.chat_lock:
            store._persist_chat()
    if memory_dirty and moments is not None:
        _save_moments(store, moments)

    return jsonify({"results": results, "summary": _swap_summary(results)})


# ---------------------------------------------------------------------------
# Phase B — user-initiated data export + account reset.
#
# These power the "Export my data" + "Delete my data" + "Reset & re-import"
# rows in the new Settings → Privacy page. Both are user-initiated, both
# are auth-gated, and the reset path requires an explicit confirmation
# token in the body to prevent accidental wipes from a buggy client that
# holds the api_key but misbehaves.
# ---------------------------------------------------------------------------


# Cap single-shot export response size. With frames bounded to MAX_FRAMES
# (200) and each body_ct at ~200 KiB, worst-case frame payload is ~40 MiB —
# so the 80 MiB ceiling covers frames + chat + memory + identity with
# headroom. If this ever trips, switch to a streaming multipart response.
_EXPORT_MAX_BYTES = 80 * 1024 * 1024  # 80 MiB


@app.route("/v1/content/export", methods=["GET"])
def content_export():
    """Return the caller's chat, memory, identity, and frames as one JSON blob.

    Ciphertext is returned verbatim — iOS decrypts client-side using
    the user's content_sk from Keychain. No decryption happens server-
    side, so there is no additional trust boundary crossed by this
    endpoint beyond the existing auth check.

    Frames are included as v1 envelopes (same shape as chat/memory) with
    their stored body_ct inline, so the user can walk away with the full
    screen-recording dataset decryptable only on their devices.
    """
    store = require_user()
    hist = store.chat_messages
    moments = _load_moments(store)
    identity = _load_identity(store)

    # Inline each frame's on-disk envelope. frames_meta is the index; the
    # ciphertext lives in <frames_dir>/<id>.env.json. A missing env file
    # just means the frame was evicted mid-read — skip it rather than 500.
    frames_out: list[dict] = []
    with store.frames_lock:
        frame_index = [f.copy() for f in store.frames_meta]
    for meta in frame_index:
        env_path = store.frames_dir / meta["filename"]
        if not env_path.exists():
            continue
        try:
            envelope = json.loads(env_path.read_text())
        except Exception as e:
            print(f"[export:{store.user_id}] skipping frame {meta.get('id')}: {e}")
            continue
        frames_out.append({
            "id": meta.get("id"),
            "ts": meta.get("ts"),
            "w": meta.get("w", 0),
            "h": meta.get("h", 0),
            "envelope": envelope,
        })

    exported_at = datetime.now().isoformat()
    enclave_info = _get_enclave_info() or {}

    export = {
        "schema_version": 2,
        "user_id": store.user_id,
        "exported_at": exported_at,
        "attestation_snapshot": {
            "enclave_content_public_key_hex": enclave_info.get("content_pk_hex", ""),
            "compose_hash": enclave_info.get("compose_hash", ""),
        },
        "chat": hist,
        "memory": moments,
        "identity": identity,
        "frames": frames_out,
        "notes": (
            "Ciphertext included verbatim; decrypt client-side using your"
            " content private key (iCloud Keychain). The attestation_snapshot"
            " records which enclave version was live at export time so you"
            " can verify origin later. Frames are v1 envelopes — their JPEG"
            " + OCR live inside body_ct."
        ),
    }

    body = json.dumps(export, ensure_ascii=False, indent=2)
    if len(body.encode("utf-8")) > _EXPORT_MAX_BYTES:
        return jsonify({
            "error": "export_too_large",
            "detail": "One-shot export exceeds the 80 MiB budget. Streaming"
                      " export is planned (TODO). Contact support / open an issue."
        }), 413

    resp = Response(body, mimetype="application/json")
    # Suggest a filename when clients save to disk.
    safe_name = f"feedling-export-{store.user_id}-{exported_at.replace(':', '').split('.')[0]}.json"
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return resp


@app.route("/v1/account/reset", methods=["POST"])
def account_reset():
    """Hard-delete the caller's account: wipe the user dir, revoke the
    api_key, remove the user record.

    Requires an explicit confirmation token in the body to prevent
    accidental wipes from a buggy client that holds the api_key but
    sends the wrong request. Two steps of intent (correct key + correct
    confirmation body) are needed.

    Idempotent in the safe-to-retry sense: a second call with the same
    api_key fails auth (user no longer exists) and returns 401. So
    retries are harmless; spurious wipes require a fresh registration.
    """
    store = require_user()
    payload = request.get_json(silent=True) or {}
    confirm = (payload.get("confirm") or "").strip()
    if confirm != "delete-all-data":
        return jsonify({
            "error": "confirmation_required",
            "detail": "POST body must include {\"confirm\": \"delete-all-data\"}."
                      " This prevents accidental resets from misbehaving clients."
        }), 400

    user_id = store.user_id

    # Remove the user from users.json FIRST so any in-flight requests
    # carrying the old api_key fail auth immediately.
    with _users_lock:
        before = len(_users)
        _users[:] = [u for u in _users if u.get("user_id") != user_id]
        removed = before - len(_users)
        # Evict all cached (hash → user_id) entries pointing at this user.
        to_evict = [h for h, uid in _key_to_user.items() if uid == user_id]
        for h in to_evict:
            _key_to_user.pop(h, None)
        _save_users()

    # Then remove the user's data directory.
    deleted_dir = False
    try:
        import shutil
        # Defense in depth: make sure we're about to delete a per-user dir
        # under FEEDLING_DIR, not FEEDLING_DIR itself or something above it.
        if (
            store.dir.exists()
            and store.dir != FEEDLING_DIR
            and store.dir.parent == FEEDLING_DIR
        ):
            shutil.rmtree(store.dir)
            deleted_dir = True
    except Exception as e:
        print(f"[reset:{user_id}] rmtree failed: {e}")

    print(f"[reset:{user_id}] deleted (user_record={removed} dir={deleted_dir})")
    return jsonify({"deleted": True, "user_id": user_id})


@app.route("/healthz", methods=["GET"])
def healthz():
    """Liveness + readiness probe. Public, no auth — used by Docker/compose."""
    return jsonify({"ok": True, "mode": "multi_tenant"})


@app.errorhandler(401)
def _unauthorized(e):
    return jsonify({"error": "unauthorized"}), 401


@app.errorhandler(403)
def _forbidden(e):
    return jsonify({"error": "forbidden"}), 403


if __name__ == "__main__":
    # PORT is read so isolation/load tests can spin up a hermetic backend on
    # a random free port without colliding with a developer's local dev
    # server on 5001 (or with another test running in parallel). Production
    # deploys can leave it unset — the 5001 default matches the published
    # compose/Dockerfile contract.
    port = int(os.environ.get("FEEDLING_PORT", os.environ.get("PORT", "5001")))
    # Production and all containers run gunicorn (see deploy/Dockerfile CMD
    # and the compose files): the Werkzeug dev server below is single-process
    # and stalls on large responses under the TDX CVM. Keep this path for
    # local dev and the hermetic test harness ONLY — never for real traffic.
    print(f"Feedling DEV server running at http://0.0.0.0:{port} (mode=multi-tenant, auth=api-key; prod uses gunicorn)")
    app.run(host="0.0.0.0", port=port, debug=False)

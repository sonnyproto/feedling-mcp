#!/usr/bin/env python3
"""
Feedling Chat Resident Consumer
================================
Polls /v1/chat/poll, routes each user message to a configured agent backend,
and writes the reply back via /v1/chat/response.

Supports two agent backend modes (set AGENT_MODE env var):

  http  — POST the user message to an HTTP endpoint and read the response body.
          Supports simple JSON endpoints and Hermes' OpenAI-compatible
          /v1/chat/completions API.

  cli   — Run a shell command with the user message passed via --query/-q flag.
          Works with any CLI agent that writes its reply to stdout.
          IMPORTANT: the CLI command MUST produce clean stdout (plain text or
          JSON only). See SKILL.md § "Chat Resident Consumer" for per-agent
          configuration requirements.

Required env vars (all keys go in CHAT_RESIDENT_ENV_FILE, never hardcoded):
  FEEDLING_API_URL      Base URL of the Feedling backend (e.g. http://localhost:5001)
  FEEDLING_API_KEY      Per-user API key from POST /v1/users/register
  AGENT_MODE            "http" or "cli"

HTTP mode:
  AGENT_HTTP_URL        Endpoint to POST user messages to
  AGENT_HTTP_TOKEN      Bearer token (optional)
  AGENT_HTTP_PROTOCOL   "simple" (POST {"message"}) or "openai" for Hermes
  AGENT_HTTP_FIELD      JSON response field containing the reply (default: "response")

CLI mode:
  AGENT_CLI_CMD         Full command template; {message} is replaced with the
                        user's message text.
                        Image messages can also use {image_path} or
                        {image_paths}; otherwise the path is appended to
                        the message text.
                        Example (Hermes): hermes chat -Q --source tool --max-turns 60 -q "{message}"
                        Example (plain):  mycli ask {message}
                        For Hermes, the consumer stores session_id and
                        auto-injects --resume on later turns.
  AGENT_CLI_PATH        Optional colon-separated executable search path added
                        before PATH. Useful for systemd services.

Optional:
  CHECKPOINT_FILE       Path to persist last-processed timestamp (default: /tmp/feedling_chat_checkpoint.json)
  SEND_FALLBACK_ON_AGENT_ERROR
                        Default false. When false, agent failures are logged
                        and no fake template is posted to the user.
  FALLBACK_REPLY        Optional opt-in fallback text
  IMAGE_TEMP_DIR        Where decrypted chat images are written for CLI agents
  SCREEN_CONTEXT_MODE   "on_mention" (default), "always", or "off". When active,
                        recent screen-sharing context is attached to screen
                        questions so the agent does not need to run curl/MCP
                        commands from its own sandbox.
  POLL_TIMEOUT          Long-poll timeout in seconds (default: 30)
  LOG_LEVEL             DEBUG / INFO / WARNING (default: INFO)
"""

import base64
import hashlib
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# v1 Envelope encryption (same logic as mcp_server.py / _whoami_pubkeys)
# ---------------------------------------------------------------------------
# The backend's build_envelope lives in backend/content_encryption.py.
# We add that directory to the path so the consumer can encrypt replies
# without duplicating crypto code.

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
try:
    from content_encryption import build_envelope as _build_envelope
    _ENCRYPTION_AVAILABLE = True
except ImportError:
    _ENCRYPTION_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("feedling.resident")


def _mask(val: str) -> str:
    if not val or len(val) < 8:
        return "***"
    return val[:4] + "***" + val[-4:]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FEEDLING_API_URL = os.environ["FEEDLING_API_URL"].rstrip("/")
FEEDLING_API_KEY = os.environ["FEEDLING_API_KEY"]
AGENT_MODE = os.environ.get("AGENT_MODE", "http").lower()

AGENT_HTTP_URL = os.environ.get("AGENT_HTTP_URL", "")
AGENT_HTTP_TOKEN = os.environ.get("AGENT_HTTP_TOKEN", "")
AGENT_HTTP_FIELD = os.environ.get("AGENT_HTTP_FIELD", "response")
AGENT_HTTP_PROTOCOL = os.environ.get("AGENT_HTTP_PROTOCOL", "simple").lower()
AGENT_HTTP_MODEL = os.environ.get("AGENT_HTTP_MODEL", "hermes-agent")
AGENT_HTTP_SESSION_KEY = os.environ.get("AGENT_HTTP_SESSION_KEY", "")
AGENT_HTTP_SESSION_HEADER = os.environ.get(
    "AGENT_HTTP_SESSION_HEADER", "X-Hermes-Session-Id"
)
AGENT_HTTP_SESSION_KEY_HEADER = os.environ.get(
    "AGENT_HTTP_SESSION_KEY_HEADER", "X-Hermes-Session-Key"
)

AGENT_CLI_CMD = os.environ.get("AGENT_CLI_CMD", "")
AGENT_CLI_PATH = os.environ.get("AGENT_CLI_PATH", "")

CHECKPOINT_FILE = Path(
    os.environ.get("CHECKPOINT_FILE", "/tmp/feedling_chat_checkpoint.json")
)
AGENT_SESSION_FILE_TEMPLATE = os.environ.get(
    "AGENT_SESSION_FILE",
    f"/tmp/feedling_agent_session_{hashlib.sha1(FEEDLING_API_KEY.encode()).hexdigest()[:10]}_{{user_id}}.txt",
)
IMAGE_TEMP_DIR = Path(os.environ.get("IMAGE_TEMP_DIR", "/tmp/feedling_chat_images"))
SCREEN_CONTEXT_MODE = os.environ.get("SCREEN_CONTEXT_MODE", "on_mention").strip().lower()
SCREEN_CONTEXT_MAX_AGE_SEC = int(os.environ.get("SCREEN_CONTEXT_MAX_AGE_SEC", "300"))
SCREEN_CONTEXT_INCLUDE_IMAGE = _env_bool("SCREEN_CONTEXT_INCLUDE_IMAGE", True)
FALLBACK_REPLY = os.environ.get(
    "FALLBACK_REPLY", "（Agent 暂时无法响应，请稍后再试）"
)
SEND_FALLBACK_ON_AGENT_ERROR = _env_bool("SEND_FALLBACK_ON_AGENT_ERROR", False)
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "30"))

# Prompt routed only when an agent entry cannot receive a native image object.
# The consumer still extracts decrypted image bytes and passes them through
# the richest available channel:
#   - OpenAI-compatible HTTP gets a multimodal `image_url` content block.
#   - simple HTTP gets an `images` array.
#   - CLI gets local image file paths in the message or command template.
IMAGE_PLACEHOLDER = os.environ.get(
    "IMAGE_PLACEHOLDER",
    "[The user sent an image in IO Chat. Inspect the attached/local image "
    "before replying. If your current runtime cannot open the image, say "
    "plainly that this connector has not enabled image vision yet.]",
)

_SCREEN_CONTEXT_TRIGGER_RE = re.compile(
    r"(screen|broadcast|share|sharing|see\s+(my|the)|look\s+at|current\s+screen|"
    r"屏幕|共享|画面|看得到|看见|看到|能看|看一下|这张|这个|这里|当前)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Decrypt sources — at least one must be set for v1 encrypted backends.
#
# FEEDLING_ENCLAVE_URL: direct HTTP to the enclave decrypt proxy (fastest,
#   same value as FEEDLING_ENCLAVE_URL in mcp_server.py, e.g. https://127.0.0.1:5003).
#
# FEEDLING_MCP_URL: URL of the Feedling MCP server (e.g. https://mcp.feedling.app
#   or https://127.0.0.1:5002).  The consumer calls feedling_chat_get_history
#   via the MCP server, which runs inside the enclave and can decrypt.
#   Requires FEEDLING_MCP_TRANSPORT=streamable-http on the MCP server.
#
# WARNING: if neither is set, /v1/chat/poll returns content="" for all v1
# encrypted messages and the consumer will never be able to reply.
# ---------------------------------------------------------------------------
FEEDLING_ENCLAVE_URL = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
FEEDLING_MCP_URL = os.environ.get("FEEDLING_MCP_URL", "").rstrip("/")
FEEDLING_MCP_KEY = os.environ.get("FEEDLING_MCP_KEY", FEEDLING_API_KEY)

_HEADERS = {"X-API-Key": FEEDLING_API_KEY}

# Separate HTTP client for the enclave (self-signed TLS, verify=False).
_ENCLAVE_CLIENT: httpx.Client | None = (
    httpx.Client(timeout=20, verify=False) if FEEDLING_ENCLAVE_URL else None
)

# Optional FastMCP async client for the MCP-sourced decryption path.
_fastmcp_cls = None
try:
    import asyncio as _asyncio
    from fastmcp import Client as _FastMCPCls
    _fastmcp_cls = _FastMCPCls
except ImportError:
    pass

# Transport probe cache: base_url → working endpoint URL (or None if unreachable).
_mcp_transport_cache: dict[str, str | None] = {}


def _mcp_supports_headers() -> bool:
    """Return True if the installed fastmcp.Client.__init__ accepts headers=."""
    if _fastmcp_cls is None:
        return False
    try:
        return "headers" in inspect.signature(_fastmcp_cls.__init__).parameters
    except (ValueError, TypeError):
        return False


def _probe_mcp_transport_sync(base_url: str) -> str | None:
    """Probe MCP server for a working transport endpoint and cache the result.

    Tries streamable-HTTP (/mcp POST) first, then SSE (/sse GET).
    Returns the working endpoint URL, or None if neither responds.
    """
    if base_url in _mcp_transport_cache:
        return _mcp_transport_cache[base_url]

    url: str | None = None
    auth_headers = {"Authorization": f"Bearer {FEEDLING_MCP_KEY}"}

    # Probe streamable-HTTP
    try:
        resp = httpx.post(
            f"{base_url}/mcp",
            headers=auth_headers,
            content=b"{}",
            timeout=5,
            verify=False,
        )
        if resp.status_code != 404:
            url = f"{base_url}/mcp"
            log.info("MCP transport: streamable-HTTP at %s/mcp", base_url)
    except Exception as e:
        log.debug("MCP /mcp probe failed: %s", e)

    # Probe SSE if streamable-HTTP not found
    if url is None:
        sse_url = f"{base_url}/sse?key={FEEDLING_MCP_KEY}"
        try:
            with httpx.stream(
                "GET",
                sse_url,
                timeout=httpx.Timeout(connect=5.0, read=1.0, write=5.0, pool=5.0),
                verify=False,
            ) as resp:
                if resp.status_code == 200:
                    url = sse_url
                    log.info("MCP transport: SSE at %s/sse", base_url)
        except httpx.ReadTimeout:
            # ReadTimeout after connect = SSE stream started, endpoint is alive.
            url = sse_url
            log.info("MCP transport: SSE at %s/sse (streaming)", base_url)
        except Exception as e:
            log.debug("MCP /sse probe failed: %s", e)

    if url is None:
        log.warning("MCP probe: %s unreachable on /mcp and /sse", base_url)

    _mcp_transport_cache[base_url] = url
    return url

_decrypt_sources = (
    f"enclave={FEEDLING_ENCLAVE_URL}" if FEEDLING_ENCLAVE_URL else ""
    + (f" mcp={FEEDLING_MCP_URL}" if FEEDLING_MCP_URL else "")
).strip() or "NONE — replies will not work for v1 encrypted messages"

log.info(
    "Starting resident consumer — mode=%s api_url=%s decrypt_sources=%s key=%s",
    AGENT_MODE, FEEDLING_API_URL, _decrypt_sources, _mask(FEEDLING_API_KEY),
)

# ---------------------------------------------------------------------------
# Checkpoint (persist last processed message timestamp)
# ---------------------------------------------------------------------------

def _load_checkpoint() -> float:
    try:
        data = json.loads(CHECKPOINT_FILE.read_text())
        return float(data.get("last_ts", 0))
    except Exception:
        return 0.0


def _save_checkpoint(ts: float) -> None:
    try:
        CHECKPOINT_FILE.write_text(json.dumps({"last_ts": ts}))
    except Exception as e:
        log.warning("checkpoint write failed: %s", e)


# ---------------------------------------------------------------------------
# Message dedup
# ---------------------------------------------------------------------------

def _msg_key(msg: dict) -> str:
    """Stable identity key: prefer explicit id field, fall back to ts:role."""
    mid = str(msg.get("id") or msg.get("message_id") or "").strip()
    if mid:
        return mid
    ts = msg.get("ts", msg.get("timestamp", 0)) or 0
    return f"{ts}:{msg.get('role', '')}"


def _mark_seen(key: str) -> bool:
    """Mark key as seen. Returns True (new) or False (already processed)."""
    if key in _seen_ids:
        return False
    _seen_ids.add(key)
    _seen_ids_order.append(key)
    if len(_seen_ids_order) > _SEEN_MAX:
        _seen_ids.discard(_seen_ids_order.pop(0))
    return True


# ---------------------------------------------------------------------------
# Decrypt sources — plaintext content for v1 encrypted messages
# ---------------------------------------------------------------------------

def _filter_since(msgs: list, since: float) -> list:
    return [m for m in msgs if float(m.get("ts", m.get("timestamp", 0)) or 0) > since]


def _fetch_from_enclave(since: float, limit: int) -> list[dict] | None:
    """Direct HTTP to the enclave decrypt proxy.

    Returns list (possibly empty) on success, None on error or not configured.
    """
    if not FEEDLING_ENCLAVE_URL or _ENCLAVE_CLIENT is None:
        return None
    try:
        resp = _ENCLAVE_CLIENT.get(
            f"{FEEDLING_ENCLAVE_URL}/v1/chat/history",
            params={"limit": limit, "since": since},
            headers=_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        msgs = data.get("messages") or data.get("history") or []
        return _filter_since(msgs, since)
    except Exception as e:
        log.warning("enclave history fetch failed: %s", e)
        return None


def _fetch_from_mcp(since: float, limit: int) -> list[dict] | None:
    """Call feedling_chat_get_history via the MCP server.

    Supports both streamable-HTTP (/mcp) and SSE (/sse) transports, detected
    via _probe_mcp_transport_sync.  Handles older fastmcp versions that lack
    the headers= kwarg by embedding the key in the URL instead.

    Returns list on success, None on error or not configured.
    """
    if not FEEDLING_MCP_URL or _fastmcp_cls is None:
        return None

    transport_url = _probe_mcp_transport_sync(FEEDLING_MCP_URL)
    if transport_url is None:
        log.warning("MCP source unreachable — no working transport endpoint")
        return None

    supports_headers = _mcp_supports_headers()

    def _extract_messages_from_mcp_result(result_obj) -> list[dict]:
        """Parse FastMCP call_tool return shapes across client versions."""
        image_blocks: list[str] = []

        def _maybe_parse_json_text(text: str):
            if not text:
                return None
            try:
                data = json.loads(text)
            except Exception:
                return None
            if isinstance(data, dict):
                msgs = data.get("messages") or data.get("history")
                if isinstance(msgs, list):
                    return msgs
            return None

        def _maybe_image_b64(item: Any) -> str | None:
            data = getattr(item, "data", None)
            mime = getattr(item, "mimeType", None) or getattr(item, "mime_type", None)
            typ = getattr(item, "type", None)
            if isinstance(item, dict):
                data = data if data is not None else item.get("data")
                mime = mime or item.get("mimeType") or item.get("mime_type")
                typ = typ or item.get("type")
            if data is None:
                return None
            if typ not in (None, "image") and not str(mime or "").startswith("image/"):
                return None
            if isinstance(data, bytes):
                return base64.b64encode(data).decode("ascii")
            if isinstance(data, str) and data.strip():
                value = data.strip()
                return value.split(",", 1)[1] if value.startswith("data:") else value
            return None

        def _attach_image_blocks(msgs: list[dict]) -> list[dict]:
            if not image_blocks:
                return msgs
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                marker = msg.get("image_b64")
                if not isinstance(marker, str):
                    continue
                m = re.fullmatch(r"<vision_block:(\d+)>", marker.strip())
                if not m:
                    continue
                idx = int(m.group(1))
                if 0 <= idx < len(image_blocks):
                    msg["image_b64"] = image_blocks[idx]
            return msgs

        if result_obj is None:
            return []

        # Newer shape: CallToolResult(content=[...], structured_content=...)
        content_list = getattr(result_obj, "content", None)
        if isinstance(content_list, list):
            parsed_msgs: list[dict] | None = None
            for item in content_list:
                image_b64 = _maybe_image_b64(item)
                if image_b64:
                    image_blocks.append(image_b64)
                    continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parsed = _maybe_parse_json_text(text)
                    if parsed is not None:
                        parsed_msgs = parsed
            if parsed_msgs is not None:
                return _attach_image_blocks(parsed_msgs)

        structured = getattr(result_obj, "structured_content", None)
        if isinstance(structured, dict):
            msgs = structured.get("messages") or structured.get("history")
            if isinstance(msgs, list):
                return _attach_image_blocks(msgs)

        text_attr = getattr(result_obj, "text", None)
        if isinstance(text_attr, str):
            parsed = _maybe_parse_json_text(text_attr)
            if parsed is not None:
                return _attach_image_blocks(parsed)

        # Older shape: list[ContentLike]
        if isinstance(result_obj, list):
            parsed_msgs: list[dict] | None = None
            for item in result_obj:
                image_b64 = _maybe_image_b64(item)
                if image_b64:
                    image_blocks.append(image_b64)
                    continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parsed = _maybe_parse_json_text(text)
                    if parsed is not None:
                        parsed_msgs = parsed
                        continue
                parsed = _maybe_parse_json_text(str(item))
                if parsed is not None:
                    parsed_msgs = parsed
            if parsed_msgs is not None:
                return _attach_image_blocks(parsed_msgs)

        parsed = _maybe_parse_json_text(str(result_obj))
        return _attach_image_blocks(parsed) if parsed is not None else []

    async def _call():
        if supports_headers:
            client_ctx = _fastmcp_cls(
                transport_url,
                headers={"Authorization": f"Bearer {FEEDLING_MCP_KEY}"},
            )
        else:
            # Older fastmcp: embed auth in URL.  For SSE the key is already
            # in the URL from probe; for /mcp add it as a query param.
            if "?" not in transport_url:
                keyed_url = f"{transport_url}?key={FEEDLING_MCP_KEY}"
            else:
                keyed_url = transport_url  # SSE: ?key=… already present
            client_ctx = _fastmcp_cls(keyed_url)

        async with client_ctx as client:
            result = await client.call_tool(
                "feedling_chat_get_history", {"limit": limit}
            )
            msgs = _extract_messages_from_mcp_result(result)
            return _filter_since(msgs, since)

    try:
        return _asyncio.run(_call())
    except Exception as e:
        log.warning("MCP history fetch failed: %s", e)
        return None


def _verify_decrypt_sources() -> bool:
    """Probe all configured decrypt sources at startup.

    Returns True if at least one configured source is reachable.
    Each unreachable source is logged at ERROR level so the operator
    can distinguish "configured but broken" from "not configured at all".
    """
    any_ok = False

    if FEEDLING_ENCLAVE_URL:
        try:
            client = _ENCLAVE_CLIENT or httpx
            resp = client.get(
                f"{FEEDLING_ENCLAVE_URL}/v1/chat/history",
                params={"limit": 1},
                headers=_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            log.info("decrypt source OK: enclave at %s", FEEDLING_ENCLAVE_URL)
            any_ok = True
        except Exception as e:
            log.error(
                "decrypt source UNREACHABLE: enclave at %s — %s",
                FEEDLING_ENCLAVE_URL, e,
            )

    if FEEDLING_MCP_URL:
        transport_url = _probe_mcp_transport_sync(FEEDLING_MCP_URL)
        if transport_url:
            log.info("decrypt source OK: MCP at %s", transport_url)
            any_ok = True
        else:
            log.error(
                "decrypt source UNREACHABLE: MCP at %s — no working transport",
                FEEDLING_MCP_URL,
            )

    return any_ok


def get_decrypted_history(since: float, limit: int = 20) -> list[dict] | None:
    """Try all configured decrypt sources in priority order.

    Returns:
      list  — source was reachable; contains messages newer than `since`
              (may be empty if no new messages).
      None  — no source configured, or all configured sources failed.
    """
    if FEEDLING_ENCLAVE_URL:
        result = _fetch_from_enclave(since, limit)
        if result is not None:
            return result
        log.warning("enclave source failed; trying MCP source if configured")

    if FEEDLING_MCP_URL:
        result = _fetch_from_mcp(since, limit)
        if result is not None:
            return result
        log.warning("MCP source failed")

    return None  # no configured source succeeded


# ---------------------------------------------------------------------------
# Image message handling
# ---------------------------------------------------------------------------

def _decode_image_b64(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[1]
    if raw.startswith("<vision_block:"):
        return None
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:
        try:
            return base64.b64decode(raw)
        except Exception as e:
            log.warning("image_b64 decode failed: %s", e)
            return None


def _image_payloads_from_msg(msg: dict) -> list[dict[str, str]]:
    image_bytes = _decode_image_b64(msg.get("image_b64"))
    if not image_bytes:
        return []
    mime = msg.get("image_mime") or "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return [
        {
            "mime_type": str(mime),
            "data": b64,
            "data_url": f"data:{mime};base64,{b64}",
        }
    ]


def _image_file_paths_for_msg(msg: dict) -> list[str]:
    payloads = _image_payloads_from_msg(msg)
    if not payloads:
        return []
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", _msg_key(msg))[:96] or "image"
    IMAGE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for idx, payload in enumerate(payloads):
        ext = ".png" if payload.get("mime_type") == "image/png" else ".jpg"
        path = IMAGE_TEMP_DIR / f"{key}_{idx}{ext}"
        try:
            path.write_bytes(base64.b64decode(payload["data"]))
            paths.append(str(path))
        except Exception as e:
            log.warning("failed to write image temp file %s: %s", path, e)
    return paths


def _image_file_paths_from_payloads(prefix: str, payloads: list[dict[str, str]]) -> list[str]:
    if not payloads:
        return []
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix)[:96] or "image"
    IMAGE_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for idx, payload in enumerate(payloads):
        ext = ".png" if payload.get("mime_type") == "image/png" else ".jpg"
        path = IMAGE_TEMP_DIR / f"{key}_{idx}{ext}"
        try:
            path.write_bytes(base64.b64decode(payload["data"]))
            paths.append(str(path))
        except Exception as e:
            log.warning("failed to write image temp file %s: %s", path, e)
    return paths


def _message_for_agent(content: str, image_paths: list[str] | None = None) -> str:
    image_paths = image_paths or []
    if not image_paths:
        return content
    joined = ", ".join(image_paths)
    return (
        f"{content}\n\n"
        f"Decrypted image file(s) for this IO message: {joined}\n"
        "Open/inspect the image before replying if your runtime has local "
        "vision or file-image support. Do not ask the user to describe the "
        "image unless this runtime truly cannot inspect local image files."
    )


# ---------------------------------------------------------------------------
# Screen-sharing context
# ---------------------------------------------------------------------------

def _should_attach_screen_context(content: str) -> bool:
    mode = SCREEN_CONTEXT_MODE
    if mode in {"0", "false", "off", "none", "disabled"}:
        return False
    if mode in {"1", "true", "always", "on"}:
        return True
    return bool(_SCREEN_CONTEXT_TRIGGER_RE.search(content or ""))


def _fetch_screen_json(path: str) -> dict | None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.get(f"{FEEDLING_API_URL}{path}", headers=_HEADERS, timeout=20)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
                continue
    log.warning("screen context fetch failed path=%s error=%s", path, last_error)
    return None


def _screen_context_for_message(content: str) -> tuple[str, list[dict[str, str]], list[str]]:
    """Attach recent screen-sharing context for screen/deictic questions.

    The resident already has the Feedling API key, so it should decrypt the
    latest frame itself instead of making the agent run curl/MCP commands from a
    sandbox that may require user approval.
    """
    if not _should_attach_screen_context(content):
        return "", [], []

    latest = _fetch_screen_json("/v1/screen/frames/latest")
    if not latest:
        return "", [], []

    frame_id = str(latest.get("id") or "").strip()
    ts = float(latest.get("ts") or 0.0)
    if not frame_id:
        return "", [], []
    if ts and time.time() - ts > SCREEN_CONTEXT_MAX_AGE_SEC:
        log.info(
            "screen context skipped — latest frame is stale age=%.1fs id=%s",
            time.time() - ts,
            frame_id,
        )
        return "", [], []

    include_image = "true" if SCREEN_CONTEXT_INCLUDE_IMAGE else "false"
    decrypted = _fetch_screen_json(
        f"/v1/screen/frames/{frame_id}/decrypt?include_image={include_image}"
    )
    if not decrypted:
        return "", [], []

    app = decrypted.get("app") or latest.get("app") or "unknown"
    ocr_text = (decrypted.get("ocr_text") or "").strip()
    captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "unknown"

    payloads: list[dict[str, str]] = []
    image_b64 = decrypted.get("image_b64")
    if isinstance(image_b64, str) and image_b64.strip():
        raw_b64 = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
        mime = decrypted.get("image_mime") or "image/jpeg"
        payloads.append(
            {
                "mime_type": str(mime),
                "data": raw_b64,
                "data_url": f"data:{mime};base64,{raw_b64}",
            }
        )

    paths = _image_file_paths_from_payloads(f"screen_{frame_id}", payloads)
    parts = [
        "[Live Feedling screen-sharing context]",
        f"captured_at_utc: {captured_at}",
        f"app: {app}",
    ]
    if ocr_text:
        parts.append(f"ocr_text:\n{ocr_text[:2000]}")
    elif payloads:
        parts.append("ocr_text: empty; inspect the attached screenshot image if your runtime supports vision.")
    else:
        parts.append("ocr_text: empty and no screenshot image was available.")
    if paths:
        parts.append("screenshot_file: " + ", ".join(paths))

    return "\n".join(parts), payloads, paths


# ---------------------------------------------------------------------------
# Agent backends
# ---------------------------------------------------------------------------

# Decoration / system lines that are never part of the actual reply.
_NOISE_LINE_RE = re.compile(
    r"^\s*("
    r"session_id\s*:.*"      # hermes session footer
    r"|[↻⟳]?\s*(resumed|created|started)\s+session\b.*"  # hermes session banner
    r"|[A-Za-z0-9_\-]{8,}\s*\(\d+\s+user\s+messages?,\s*\d+\s+total\s+messages?\)"
    r"|---+|={3,}|[-–—_]{3,}" # separator lines
    r"|\[.*\]\s*$"           # [bracket] meta lines
    r"|💭.*"                 # hermes thinking-emoji prefix
    r"|\*\*[^*]+\*\*\s*$"   # **standalone bold header**
    r"|</?think>"            # <think> XML tags
    r"|Reasoning:\s*$"       # bare "Reasoning:" label
    r"|[✵✦✧★☆※].*"          # decorative symbol lines
    r")",
    re.IGNORECASE,
)

# Internal/system identity tokens that must never leak to end-user chat.
_IDENTITY_LEAK_RE = re.compile(r"\b(hermes|reasoning|chain\s*of\s*thought)\b", re.IGNORECASE)

# Typical leaked planning / chain-of-thought lead-ins from agent UIs.
_REASONING_LINE_RE = re.compile(
    r"^\s*(i\s+need\s+to|i\'?m\s+thinking|the\s+user\s+wrote|the\s+user\s+wants|"
    r"this\s+(means|doesn\'?t)|i\s+think|i\s+should|i\'ll|let\s+me\s+|my\s+plan\s+is)",
    re.IGNORECASE,
)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")

_LEADING_META_PREAMBLE_RE = re.compile(
    r"\b("
    r"(no\s+)?specific\s+tool\s+is\s+required"
    r"|factual\s+question"
    r"|rely\s+on\s+my\s+(memory|general\s+knowledge)"
    r"|general\s+knowledge\s+up\s+to"
    r"|i\s+remember\b"
    r"|i'?m\s+uncertain\b"
    r"|i'?ll\s+craft\b"
    r"|i\s+will\s+craft\b"
    r")",
    re.IGNORECASE,
)


def _strip_leading_meta_preamble(lines: list[str]) -> list[str]:
    """Drop leaked English planning before a CJK final answer.

    Hermes can sometimes emit an unlabelled preamble such as
    "specific tool is required..." before the actual Chinese reply. Treat that
    as internal setup only when it appears at the very start and a CJK answer
    follows; plain English replies remain untouched.
    """
    if not lines or not any(_CJK_RE.search(ln) for ln in lines):
        return lines

    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return lines
    if not _LEADING_META_PREAMBLE_RE.search(lines[first]):
        return lines

    first_cjk = next((i for i, ln in enumerate(lines[first:], start=first) if _CJK_RE.search(ln)), None)
    if first_cjk is None:
        return lines
    return lines[first_cjk:]


def _strip_reasoning_sections(raw: str) -> str:
    """Remove explicit reasoning/code sections while preserving final answer.

    Hermes/OpenClaw UIs often print a visible block like:

      💭 Reasoning:
      ```copy
      **Doing work**
      I need to ...
      ```

    The older consumer avoided this by keeping only the last CJK paragraph,
    which also destroyed normal multi-paragraph answers. This keeps the full
    answer and removes only the declared reasoning block.
    """
    lines = raw.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r"(^|\b|💭)\s*Reasoning\s*:", line, re.IGNORECASE):
            i += 1
            # Skip optional language/copy marker before a fenced block.
            while i < len(lines) and lines[i].strip().lower() in {"copy", ""}:
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    i += 1
                if i < len(lines):
                    i += 1
                continue
            # Unfenced reasoning: skip until a blank line, then resume.
            while i < len(lines) and lines[i].strip():
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _extract_text_from_cli_output(raw: str) -> str:
    """Best-effort extraction from raw CLI stdout.

    1. Try JSON parse first (hermes --output-mode json gives a clean field).
    2. Remove explicit reasoning/code sections.
    3. Strip known headers/footers.
    4. Return the full remaining answer, preserving multi-paragraph replies.
    """
    raw = raw.strip()
    if not raw:
        return ""

    # JSON path
    try:
        obj = json.loads(raw)
        for field in ("response", "content", "text", "message", "reply"):
            if isinstance(obj.get(field), str) and obj[field].strip():
                return obj[field].strip()
    except (json.JSONDecodeError, TypeError):
        pass

    raw = _strip_reasoning_sections(raw)
    clean = [ln.rstrip() for ln in raw.splitlines() if not _NOISE_LINE_RE.match(ln)]
    text = "\n".join(clean).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text


def _agent_http_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if AGENT_HTTP_TOKEN:
        headers["Authorization"] = f"Bearer {AGENT_HTTP_TOKEN}"
    return headers


def _agent_session_key() -> str:
    if AGENT_HTTP_SESSION_KEY.strip():
        return AGENT_HTTP_SESSION_KEY.strip()
    user_id = (_whoami_cache.get("user_id") or "").strip()
    if user_id:
        return f"feedling:{user_id}"
    digest = hashlib.sha1(FEEDLING_API_KEY.encode()).hexdigest()[:12]
    return f"feedling:{digest}"


def _extract_openai_reply(body: dict) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    raise ValueError("OpenAI-compatible response has no usable reply text")


def _remember_http_session(resp: httpx.Response) -> None:
    sid = (resp.headers.get(AGENT_HTTP_SESSION_HEADER) or "").strip()
    if sid:
        _save_agent_session_id(sid)


def _call_agent_http_simple(message: str, images: list[dict[str, str]] | None = None) -> str:
    headers = _agent_http_headers()
    payload = {"message": message}
    if images:
        payload["images"] = images
    resp = httpx.post(AGENT_HTTP_URL, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    _remember_http_session(resp)
    body = resp.json()
    if isinstance(body, dict):
        for field in (AGENT_HTTP_FIELD, "response", "content", "text", "reply"):
            if isinstance(body.get(field), str) and body[field].strip():
                return body[field].strip()
        raise ValueError(f"response field not found in: {list(body.keys())}")
    if isinstance(body, str):
        return body.strip()
    raise ValueError(f"unexpected response type: {type(body)}")


def _call_agent_http_openai(message: str, images: list[dict[str, str]] | None = None) -> str:
    headers = _agent_http_headers()
    sid = _load_agent_session_id()
    if sid:
        headers[AGENT_HTTP_SESSION_HEADER] = sid
    session_key = _agent_session_key()
    if session_key:
        headers[AGENT_HTTP_SESSION_KEY_HEADER] = session_key

    content: Any = message
    if images:
        blocks: list[dict[str, Any]] = [{"type": "text", "text": message}]
        for image in images:
            data_url = image.get("data_url")
            if data_url:
                blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        content = blocks

    payload = {
        "model": AGENT_HTTP_MODEL,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
    }
    resp = httpx.post(AGENT_HTTP_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    _remember_http_session(resp)
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError(f"unexpected OpenAI response type: {type(body)}")
    return _extract_openai_reply(body)


def call_agent_http(message: str, images: list[dict[str, str]] | None = None) -> str:
    if not AGENT_HTTP_URL:
        raise ValueError("AGENT_HTTP_URL is not set for http mode")
    if AGENT_HTTP_PROTOCOL in {"openai", "hermes", "chat_completions", "chat-completions"}:
        return _call_agent_http_openai(message, images=images)
    if AGENT_HTTP_PROTOCOL in {"simple", "generic", "json"}:
        return _call_agent_http_simple(message, images=images)
    raise ValueError(f"unknown AGENT_HTTP_PROTOCOL: {AGENT_HTTP_PROTOCOL!r}")


def _agent_session_file_for_user() -> Path:
    user_id = (_whoami_cache.get("user_id") or "unknown").strip() or "unknown"
    path = AGENT_SESSION_FILE_TEMPLATE.replace("{user_id}", user_id)
    return Path(path)


def _load_agent_session_id() -> str:
    user_id = (_whoami_cache.get("user_id") or "unknown").strip() or "unknown"
    cached = _agent_session_id_cache.get(user_id)
    if cached:
        return cached

    f = _agent_session_file_for_user()
    try:
        sid = f.read_text(encoding="utf-8").strip()
        if sid:
            _agent_session_id_cache[user_id] = sid
            return sid
    except Exception:
        pass
    return ""


def _save_agent_session_id(sid: str) -> None:
    sid = (sid or "").strip()
    if not sid:
        return

    user_id = (_whoami_cache.get("user_id") or "unknown").strip() or "unknown"
    _agent_session_id_cache[user_id] = sid

    f = _agent_session_file_for_user()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(sid + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("failed to persist agent session id: %s", e)


def _extract_session_id(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"session_id\s*:\s*([A-Za-z0-9_\-]+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"Resumed session\s+([A-Za-z0-9_\-]+)", raw)
    if m:
        return m.group(1)
    return ""


def _resolve_cli_executable(cmd: list[str]) -> list[str]:
    if not cmd:
        return cmd

    executable = cmd[0]
    if os.path.sep in executable:
        return cmd

    search_parts: list[str] = []
    if AGENT_CLI_PATH:
        search_parts.extend(p for p in AGENT_CLI_PATH.split(os.pathsep) if p)
    if os.environ.get("PATH"):
        search_parts.extend(p for p in os.environ["PATH"].split(os.pathsep) if p)

    home = Path.home()
    search_parts.extend(
        [
            str(home / ".local" / "bin"),
            str(home / ".hermes" / "hermes-agent" / "venv" / "bin"),
            str(home / ".hermes" / "bin"),
            str(home / ".cargo" / "bin"),
            "/usr/local/bin",
            "/opt/homebrew/bin",
        ]
    )
    search_path = os.pathsep.join(dict.fromkeys(search_parts))
    resolved = shutil.which(executable, path=search_path)
    if not resolved:
        raise FileNotFoundError(
            f"CLI executable {executable!r} was not found. Use an absolute path "
            "in AGENT_CLI_CMD or set AGENT_CLI_PATH for the systemd service."
        )

    if resolved != executable:
        log.debug("resolved cli executable %s -> %s", executable, resolved)
    return [resolved, *cmd[1:]]


def _is_hermes_chat_cmd(cmd: list[str]) -> bool:
    return len(cmd) >= 2 and Path(cmd[0]).name == "hermes" and cmd[1] == "chat"


def _cli_flag_value(cmd: list[str], flag: str) -> str:
    try:
        idx = cmd.index(flag)
    except ValueError:
        return ""
    if idx + 1 >= len(cmd):
        return ""
    return cmd[idx + 1]


def _warn_if_agent_entry_may_drift() -> None:
    """Log non-fatal warnings for common context/persona drift configs.

    The resident consumer should call the user's real runtime entry. It should
    not invent a mini persona prompt or a shallow throwaway session just for IO.
    We keep this as diagnostics instead of hard failure because non-Hermes
    runtimes legitimately vary, but the warnings make bad configs visible in
    systemd logs before users experience a strange persona shift.
    """
    if AGENT_MODE != "cli" or not AGENT_CLI_CMD:
        return

    lower_template = AGENT_CLI_CMD.lower()
    if re.search(r"\b(you are|user message|reply naturally|same style|persona)\b", lower_template):
        log.warning(
            "AGENT_CLI_CMD appears to wrap {message} in an identity/persona "
            "prompt. For continuity, call the real agent entry directly and "
            "let the runtime's own profile/memory shape the reply."
        )

    try:
        cmd = shlex.split(AGENT_CLI_CMD.replace("{message}", "__MSG__"))
    except ValueError as e:
        log.warning("AGENT_CLI_CMD could not be parsed for drift checks: %s", e)
        return

    if not _is_hermes_chat_cmd(cmd):
        return

    if not os.environ.get("HERMES_HOME"):
        log.warning(
            "Hermes/OpenClaw CLI is configured without HERMES_HOME. systemd may "
            "use a different profile than the user's resident agent. Set "
            "HERMES_HOME to the real profile, for example "
            "/home/openclaw/.hermes/profiles/daily."
        )

    if "--source" not in cmd:
        log.warning(
            "Hermes/OpenClaw CLI has no --source flag. Use --source tool so IO "
            "messages enter the normal tool-origin conversation path."
        )

    turns_raw = _cli_flag_value(cmd, "--max-turns")
    if turns_raw:
        try:
            turns = int(turns_raw)
            if turns < 20:
                log.warning(
                    "Hermes/OpenClaw CLI uses --max-turns %d. Very small turn "
                    "limits often produce short/template replies. Prefer "
                    "--max-turns 60 for IO chat unless your runtime has a "
                    "stronger native session endpoint.",
                    turns,
                )
        except ValueError:
            pass


def _strip_hermes_continue(cmd: list[str]) -> tuple[list[str], bool]:
    """Remove Hermes --continue/-c from resident-owned commands.

    The resident owns continuity by persisting the first Hermes session_id and
    injecting --resume <session_id> on later turns. --continue means "latest
    local session" and can attach Feedling to the wrong conversation.
    """
    out: list[str] = []
    i = 0
    removed = False
    while i < len(cmd):
        token = cmd[i]
        if token in {"--continue", "-c"}:
            removed = True
            i += 1
            # Hermes accepts an optional session name after --continue. Drop it
            # only when it is clearly not another flag.
            if i < len(cmd) and not cmd[i].startswith("-"):
                i += 1
            continue
        out.append(token)
        i += 1
    return out, removed


def _has_hermes_resume(cmd: list[str]) -> bool:
    return "--resume" in cmd or "-r" in cmd


def _render_cli_template(message: str, sid: str, image_paths: list[str] | None = None) -> list[str]:
    image_paths = image_paths or []
    msg_token = "__FEEDLING_MESSAGE__"
    sid_token = "__FEEDLING_SESSION_ID__"
    image_path_token = "__FEEDLING_IMAGE_PATH__"
    image_paths_token = "__FEEDLING_IMAGE_PATHS__"
    template = (
        AGENT_CLI_CMD
        .replace("{message}", msg_token)
        .replace("{session_id}", sid_token)
        .replace("{image_path}", image_path_token)
        .replace("{image_paths}", image_paths_token)
    )
    cmd = shlex.split(template)
    first_image = image_paths[0] if image_paths else ""
    all_images = " ".join(image_paths)
    return [
        part
        .replace(msg_token, message)
        .replace(sid_token, sid)
        .replace(image_path_token, first_image)
        .replace(image_paths_token, all_images)
        for part in cmd
    ]


def _prepare_cli_command(message: str, image_paths: list[str] | None = None) -> list[str]:
    sid = _load_agent_session_id()
    rendered_message = message
    if image_paths and "{image_path" not in AGENT_CLI_CMD:
        rendered_message = _message_for_agent(message, image_paths)
    cmd = _render_cli_template(rendered_message, sid, image_paths=image_paths)

    if _is_hermes_chat_cmd(cmd):
        cmd, removed_continue = _strip_hermes_continue(cmd)
        if removed_continue:
            log.warning(
                "removed Hermes --continue from AGENT_CLI_CMD; resident "
                "continuity uses stored session_id plus --resume"
            )
        if sid and not _has_hermes_resume(cmd):
            cmd = [cmd[0], cmd[1], "--resume", sid, *cmd[2:]]

    return _resolve_cli_executable(cmd)


def call_agent_cli(message: str, image_paths: list[str] | None = None) -> str:
    if not AGENT_CLI_CMD:
        raise ValueError("AGENT_CLI_CMD is not set for cli mode")

    cmd = _prepare_cli_command(message, image_paths=image_paths)
    log.debug("running cli agent: %s", cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    observed_sid = _extract_session_id((result.stdout or "") + "\n" + (result.stderr or ""))
    if observed_sid:
        _save_agent_session_id(observed_sid)

    if result.returncode != 0:
        raise RuntimeError(
            f"cli agent exited {result.returncode}: {(result.stderr or '')[:300]}"
        )
    raw = result.stdout
    text = _extract_text_from_cli_output(raw)
    if not text:
        raise ValueError(
            f"cli agent produced no usable output (exit={result.returncode})"
        )
    return text


def _sanitize_reply_text(text: str) -> str:
    """Strip formatting/system leakage and collapse accidental duplication."""
    if not isinstance(text, str):
        return ""

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return ""

    kept: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if _NOISE_LINE_RE.match(ln):
            continue
        if _IDENTITY_LEAK_RE.search(ln):
            continue
        if _REASONING_LINE_RE.match(ln):
            continue
        # Remove markdown-ish wrappers/bullets and decorative prefixes.
        ln = re.sub(r"^[`#>*\-\s]+", "", ln).strip()
        ln = re.sub(r"^[—–-]+\s*", "", ln).strip()
        if not ln:
            continue
        kept.append(ln)

    if not kept:
        return ""

    kept = _strip_leading_meta_preamble(kept)
    if not kept:
        return ""

    # Dedup consecutive identical lines.
    deduped: list[str] = []
    for ln in kept:
        if not deduped or deduped[-1] != ln:
            deduped.append(ln)

    return "\n".join(deduped).strip()


def _normalize_agent_replies(raw_reply: str) -> list[str]:
    """Convert agent output into one or more chat bubbles.

    Supported shapes:
    - Plain text -> one bubble after sanitization.
    - JSON string with {"messages": ["...", "..."]} -> multiple bubbles.

    We keep policy minimal here: resident should not force one-to-one turn mapping;
    agent-side logic decides whether to return one or many messages.
    """
    if not isinstance(raw_reply, str):
        return []

    raw_reply = raw_reply.strip()
    if not raw_reply:
        return []

    # Optional structured multi-message output from agent.
    try:
        obj = json.loads(raw_reply)
        if isinstance(obj, dict) and isinstance(obj.get("messages"), list):
            out: list[str] = []
            for item in obj["messages"]:
                if isinstance(item, str):
                    clean = _sanitize_reply_text(item)
                    if clean:
                        out.append(clean)
            return out
    except (json.JSONDecodeError, TypeError):
        pass

    clean = _sanitize_reply_text(raw_reply)
    return [clean] if clean else []


def call_agent(
    message: str,
    images: list[dict[str, str]] | None = None,
    image_paths: list[str] | None = None,
) -> list[str]:
    if AGENT_MODE == "http":
        raw = call_agent_http(message, images=images)
    elif AGENT_MODE == "cli":
        raw = call_agent_cli(message, image_paths=image_paths)
    else:
        raise ValueError(f"unknown AGENT_MODE: {AGENT_MODE!r}")

    replies = _normalize_agent_replies(raw)
    if replies:
        return replies
    if SEND_FALLBACK_ON_AGENT_ERROR:
        return [FALLBACK_REPLY]
    raise ValueError("agent produced no usable reply after sanitization")


# ---------------------------------------------------------------------------
# Feedling API helpers
# ---------------------------------------------------------------------------

# Cached from /v1/users/whoami at startup. Refreshed on 401/encryption error.
_whoami_cache: dict = {"user_id": "", "user_pk": None, "enclave_pk": None}

# Fallback deduplication — don't flood the user if the agent repeatedly fails.
FALLBACK_COOLDOWN = int(os.environ.get("FALLBACK_COOLDOWN", "60"))
_last_fallback_ts: float = 0.0

# Message dedup — rolling window prevents reprocessing the same message on
# restart with a stale checkpoint or if poll races with checkpoint save.
_seen_ids: set[str] = set()
_seen_ids_order: list[str] = []
_SEEN_MAX = 500

# Persisted agent conversation session id (for CLI agents like Hermes), keyed by user_id.
_agent_session_id_cache: dict[str, str] = {}


def _load_whoami() -> bool:
    """Fetch encryption keys from /v1/users/whoami and cache them.

    Returns True if both the user pubkey and enclave pubkey were obtained.
    A missing enclave pubkey is still usable (visibility falls back to
    local_only), but shared-visibility envelopes require it.
    """
    try:
        resp = httpx.get(
            f"{FEEDLING_API_URL}/v1/users/whoami", headers=_HEADERS, timeout=10
        )
        resp.raise_for_status()
        info = resp.json()
    except Exception as e:
        log.warning("whoami fetch failed: %s", e)
        return False

    user_id = info.get("user_id", "") or ""
    user_pk_b64 = (info.get("public_key") or "").strip()
    enc_pk_hex = (info.get("enclave_content_public_key_hex") or "").strip()

    try:
        user_pk = base64.b64decode(user_pk_b64) if user_pk_b64 else None
        if user_pk is not None and len(user_pk) != 32:
            user_pk = None
    except Exception:
        user_pk = None

    try:
        enc_pk = bytes.fromhex(enc_pk_hex) if enc_pk_hex else None
        if enc_pk is not None and len(enc_pk) != 32:
            enc_pk = None
    except Exception:
        enc_pk = None

    _whoami_cache.update(user_id=user_id, user_pk=user_pk, enclave_pk=enc_pk)
    log.info(
        "whoami loaded — user_id=%s user_pk=%s enclave_pk=%s",
        user_id,
        "ok" if user_pk else "MISSING",
        "ok" if enc_pk else "missing (local_only fallback)",
    )
    return bool(user_id and user_pk)


def poll_chat(since: float) -> dict:
    url = f"{FEEDLING_API_URL}/v1/chat/poll"
    params = {"since": since, "timeout": POLL_TIMEOUT}
    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=POLL_TIMEOUT + 10)
    resp.raise_for_status()
    return resp.json()


def post_reply(content: str) -> None:
    """Post agent reply as a v1 ciphertext envelope.

    Falls back to plaintext only when encryption is unavailable — this will
    return 400 on v1 backends and is logged as an error so it's visible.

    Handles `bootstrap_incomplete` 409 by logging the structured error
    (stage, memory_count, required) and returning without raising — the
    user-side agent skipped bootstrap, and re-raising would cause the
    daemon to loop on this dead-end forever. The operator sees what's
    wrong in the log instead.
    """
    url = f"{FEEDLING_API_URL}/v1/chat/response"

    user_id = _whoami_cache["user_id"]
    user_pk: bytes | None = _whoami_cache["user_pk"]
    enc_pk: bytes | None = _whoami_cache["enclave_pk"]

    if _ENCRYPTION_AVAILABLE and user_id and user_pk:
        visibility = "shared" if enc_pk else "local_only"
        envelope = _build_envelope(
            plaintext=content.encode("utf-8"),
            owner_user_id=user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enc_pk,
            visibility=visibility,
        )
        resp = httpx.post(
            url, json={"envelope": envelope}, headers=_HEADERS, timeout=15
        )
        _handle_post_reply_response(resp)
        return

    # Encryption unavailable — plaintext path (will 400 on v1 backends).
    log.error(
        "ENCRYPTION UNAVAILABLE — posting plaintext will fail on v1 backends. "
        "Ensure content_encryption.py is importable and whoami succeeded."
    )
    resp = httpx.post(
        url, json={"content": content, "push_live_activity": False},
        headers=_HEADERS, timeout=15,
    )
    _handle_post_reply_response(resp)


def _handle_post_reply_response(resp) -> None:
    """Inspect a /v1/chat/response response. Re-raises 4xx/5xx EXCEPT for
    the structured `bootstrap_incomplete` 409, which we want to surface in
    operator logs without crashing the daemon (a crash would put the
    process into an restart-loop trying the same dead-end content forever).
    """
    if resp.status_code == 409:
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") == "bootstrap_incomplete":
            log.error(
                "chat_response rejected: bootstrap_incomplete stage=%s "
                "memory_count=%s identity_written=%s — the upstream agent "
                "skipped Pass 1-3 / Step 5. Have the user re-run "
                "bootstrap from the start prompt; until then this user's "
                "Feedling chat is dead-ended.",
                body.get("stage"),
                body.get("memory_count"),
                body.get("identity_written"),
            )
            return
    resp.raise_for_status()


def get_latest_ts() -> float:
    url = f"{FEEDLING_API_URL}/v1/chat/history"
    resp = httpx.get(url, params={"limit": 1}, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    messages = data.get("messages") or data.get("history") or []
    if messages:
        m = messages[-1]
        return float(m.get("ts", m.get("timestamp", 0)) or 0)
    return 0.0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(signum, _frame):
    global _running
    log.info("received signal %d — shutting down", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _process_messages(messages: list) -> float:
    """Process a batch of messages, return the highest timestamp seen."""
    global _last_fallback_ts
    latest = 0.0
    for msg in messages:
        # Tolerate both "ts" and "timestamp" key names across API versions.
        ts = float(msg.get("ts", msg.get("timestamp", 0)) or 0)
        role = msg.get("role", "")
        if role != "user":
            latest = max(latest, ts)
            continue

        # Idempotency — skip messages already processed in this session.
        key = _msg_key(msg)
        if not _mark_seen(key):
            log.debug("skipping already-processed message key=%s", key)
            latest = max(latest, ts)
            continue

        content = msg.get("content", "").strip()
        content_type = msg.get("content_type", "text")
        image_payloads: list[dict[str, str]] = []
        image_paths: list[str] = []

        if content_type == "image":
            # Image messages legitimately have content == "" — the JPEG
            # lives in image_b64. Extract it here so the agent entry receives
            # real image context instead of only a vague "image arrived" hint.
            log.info(
                "image message [ts=%.3f] — preparing image context for agent",
                ts,
            )
            image_payloads = _image_payloads_from_msg(msg)
            image_paths = _image_file_paths_for_msg(msg) if image_payloads else []
            if not image_payloads:
                log.warning(
                    "image message [ts=%.3f] has no decrypted image_b64; "
                    "routing honest image-unavailable prompt",
                    ts,
                )
            content = IMAGE_PLACEHOLDER
        elif not content:
            # Genuinely empty text — decrypt source missing or failed.
            # Never send a fallback for content we cannot read.
            log.warning(
                "user message has no plaintext content ts=%.3f content_type=%s "
                "— skipping (set FEEDLING_ENCLAVE_URL or FEEDLING_MCP_URL to "
                "enable decryption)",
                ts, content_type,
            )
            latest = max(latest, ts)
            continue
        else:
            log.info("user message [ts=%.3f]: %s", ts, content[:80])

        screen_text, screen_payloads, screen_paths = _screen_context_for_message(content)
        if screen_text:
            content = f"{content}\n\n{screen_text}"
            image_payloads.extend(screen_payloads)
            image_paths.extend(screen_paths)
            log.info(
                "attached screen context to agent message ts=%.3f images=%d",
                ts,
                len(screen_payloads),
            )

        try:
            if image_payloads or image_paths:
                replies = call_agent(
                    content,
                    images=image_payloads,
                    image_paths=image_paths,
                )
            else:
                replies = call_agent(content)
        except Exception as e:
            log.error("agent call failed; not posting user-visible fallback: %s", e)
            if SEND_FALLBACK_ON_AGENT_ERROR:
                now = time.time()
                if now - _last_fallback_ts >= FALLBACK_COOLDOWN:
                    replies = [FALLBACK_REPLY]
                    _last_fallback_ts = now
                    log.warning("sending opt-in fallback reply (cooldown starts)")
                else:
                    log.warning(
                        "fallback suppressed — cooldown active (last sent %.0fs ago)",
                        now - _last_fallback_ts,
                    )
                    latest = max(latest, ts)
                    continue
            else:
                # Mark this message seen for this process so a broken agent entry
                # does not create a visible template loop. The error stays in logs.
                latest = max(latest, ts)
                continue

        if isinstance(replies, str):
            replies = [replies]
        elif not isinstance(replies, list):
            replies = [str(replies)]

        for reply in replies:
            try:
                post_reply(reply)
                log.info("reply sent: %s", reply[:80])
            except Exception as e:
                log.error("failed to post reply: %s", e)

        latest = max(latest, ts)

    return latest


def run() -> None:
    # Hard auth check before entering the poll loop.
    # A missing user_id or public_key means every encrypted reply will fail;
    # exit now so the operator sees an immediate error instead of silent no-ops.
    if not _ENCRYPTION_AVAILABLE:
        log.critical(
            "content_encryption module not found — v1 envelope posting disabled. "
            "Make sure the consumer runs from the feedling-mcp repo root."
        )
        sys.exit(1)

    if not _load_whoami():
        log.critical(
            "whoami failed at startup — cannot obtain user_id or public_key. "
            "Check FEEDLING_API_URL and FEEDLING_API_KEY, then restart."
        )
        sys.exit(1)

    _warn_if_agent_entry_may_drift()

    if FEEDLING_ENCLAVE_URL or FEEDLING_MCP_URL:
        if not _verify_decrypt_sources():
            log.critical(
                "All configured decrypt sources are unreachable "
                "(enclave=%s mcp=%s). Cannot decrypt user messages — exiting.",
                FEEDLING_ENCLAVE_URL or "unset",
                FEEDLING_MCP_URL or "unset",
            )
            sys.exit(1)
    else:
        log.warning(
            "⚠️  No decryption source configured (FEEDLING_ENCLAVE_URL and "
            "FEEDLING_MCP_URL are both unset). "
            "User messages in v1 encrypted mode have content=\"\" and will be "
            "silently skipped — the consumer will never send replies. "
            "Set FEEDLING_ENCLAVE_URL (direct enclave) or FEEDLING_MCP_URL "
            "(via MCP server) to fix this."
        )

    last_ts = _load_checkpoint()

    if last_ts == 0.0:
        try:
            last_ts = get_latest_ts()
            log.info("no checkpoint — seeding from history ts=%.3f", last_ts)
        except Exception as e:
            log.warning("could not seed from history: %s", e)

    _save_checkpoint(last_ts)
    log.info("starting poll loop — last_ts=%.3f poll_timeout=%ds", last_ts, POLL_TIMEOUT)

    consecutive_errors = 0

    while _running:
        try:
            result = poll_chat(last_ts)
            consecutive_errors = 0

            if result.get("timed_out"):
                continue

            poll_messages = result.get("messages") or []
            if not poll_messages:
                continue

            # poll is used only as a trigger — its content fields are "" for
            # v1 encrypted envelopes. Fetch actual plaintext from a decrypt source.
            if FEEDLING_ENCLAVE_URL or FEEDLING_MCP_URL:
                decrypted = get_decrypted_history(since=last_ts, limit=20)
                if decrypted is None:
                    # All configured sources failed — skip this cycle, keep checkpoint.
                    log.warning(
                        "poll triggered but all decrypt sources failed; "
                        "skipping cycle (messages not processed)"
                    )
                    continue
                if not decrypted:
                    # Sources OK but no new messages — advance from poll timestamps.
                    log.debug("poll triggered but decrypt sources returned no new messages")
                    for m in poll_messages:
                        pts = float(m.get("ts", m.get("timestamp", 0)) or 0)
                        if pts > last_ts:
                            last_ts = pts
                            _save_checkpoint(last_ts)
                    continue
                messages = decrypted
            else:
                # No decrypt source — fall through with poll content (will be
                # empty for v1 encrypted messages, skipped in _process_messages).
                messages = poll_messages

            new_ts = _process_messages(messages)
            if new_ts > last_ts:
                last_ts = new_ts
                _save_checkpoint(last_ts)

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            log.error("HTTP %d on poll: %s", status, e)
            if status == 401:
                log.warning("401 on poll — API key may have changed; refreshing whoami")
                if not _load_whoami():
                    log.critical(
                        "whoami returned 401 — API key is invalid. "
                        "Update FEEDLING_API_KEY and restart the service."
                    )
                    sys.exit(1)
            consecutive_errors += 1
            time.sleep(min(2 ** consecutive_errors, 60))
        except Exception as e:
            log.error("poll error: %s", e)
            consecutive_errors += 1
            time.sleep(min(2 ** consecutive_errors, 60))

    log.info("resident consumer stopped")


if __name__ == "__main__":
    run()

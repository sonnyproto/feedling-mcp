"""Agent-runner supervisor — one resident consumer process per user.

The canonical consumer is ``tools/chat_resident_consumer.py`` (the VPS resident
consumer). This supervisor hosts it multi-tenant in the CVM: it holds a DB-backed
lease per user (``agent_runtime_instances``) so exactly one supervisor runs a
given user's consumer across workers/processes, spawns one resident consumer per
active user (driven in cli mode against claude/codex), heartbeats leases, and
reaps dead children.

``Supervisor.tick`` takes injected ``spawn_fn``/``alive_fn``/``kill_fn`` so the
orchestration is testable against the real lease table without spawning
processes. ``main()`` wires the real spawner + run loop.

Roster (P1): the users to service come from ``AGENT_RUNTIME_USERS`` (inline JSON)
or ``AGENT_RUNTIME_ROSTER`` (file). Each entry: a Feedling API key + a provider
key (plaintext, or ``provider_key_envelope`` decrypted JIT via the enclave) +
optional ``driver``/``cli_cmd``/``model``.

Env:
  DATABASE_URL          lease table                                  [required]
  FEEDLING_API_URL      backend base url
  FEEDLING_ENCLAVE_URL  enclave decrypt proxy (for provider-key envelope)
  AGENT_RUNTIME_USERS   inline JSON roster, or
  AGENT_RUNTIME_ROSTER  path to a JSON roster file
  AGENT_DATA_ROOT       per-user home root (default /agent-data)
  AGENT_LEASE_TTL_SEC   lease TTL / heartbeat budget (default 60)
  AGENT_TICK_INTERVAL_SEC  loop interval (default 15)
  AGENT_RUNTIME_ISOLATION  process (default) | container
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

# Put the backend dir on sys.path BEFORE importing backend modules. When this is
# launched as a script (``python backend/agent_runtime/supervisor.py`` from /app
# in the CVM image), sys.path[0] is the script's own dir, NOT backend/ — so
# ``import db`` / ``from core import …`` would fail unless we insert it first.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import db
from core import runtime_token
from agent_runtime import leases, litellm_gateway, spawners

log = logging.getLogger("feedling.agent_runtime.supervisor")


def parse_roster(raw) -> list[dict]:
    """Parse a roster (JSON string or list) into entries that have an api_key."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and str(e.get("api_key") or "").strip()]


class Supervisor:
    def __init__(
        self,
        *,
        owner: str,
        lease_ttl: float,
        data_root: str,
        spawn_fn,
        alive_fn,
        kill_fn=spawners._signal_kill,
        now=time.time,
        token_writer=None,
    ) -> None:
        self.owner = owner
        self.lease_ttl = lease_ttl
        self.data_root = data_root.rstrip("/")
        self.spawn_fn = spawn_fn      # (entry, user_id, home) -> pid
        self.alive_fn = alive_fn      # (pid) -> bool
        self.kill_fn = kill_fn        # (pid) -> None
        self.token_writer = token_writer  # (user_id, home) -> None, or None (Stage D off)
        self._now = now
        self.children: dict[str, dict] = {}  # user_id -> {pid, entry, home}

    def _write_token(self, user_id: str, home: str) -> None:
        if self.token_writer is None:
            return
        try:
            self.token_writer(user_id, home)
        except Exception as e:  # noqa: BLE001 — a token-refresh failure must not crash the tick
            log.warning("runtime-token refresh failed for %s: %s", user_id, e)

    def _home(self, user_id: str) -> str:
        return f"{self.data_root}/users/{user_id}"

    def tick(self, roster: list[dict]) -> None:
        """One supervision pass: heartbeat live children, reap dead ones, drop
        children whose user left the roster, and acquire+spawn for any user we
        don't already run."""
        # Reap children whose user is no longer in the (re-derived) roster — e.g.
        # a user who disabled hosting. Kill + release so disable takes effect this
        # tick rather than lingering until lease expiry.
        roster_uids = {str(e.get("user_id") or "") for e in roster}
        for user_id in list(self.children):
            if user_id not in roster_uids:
                log.info("user %s left the roster; terminating its consumer", user_id)
                self.kill_fn(self.children[user_id]["pid"])
                leases.release(user_id, self.owner, now=self._now())
                self.children.pop(user_id, None)

        for entry in roster:
            user_id = str(entry.get("user_id") or "")
            if not user_id:
                continue
            child = self.children.get(user_id)

            if child and self.alive_fn(child["pid"]):
                if not leases.renew(user_id, self.owner, ttl=self.lease_ttl,
                                    pid=child["pid"], status="running", now=self._now()):
                    # Lost the lease (another supervisor took over after an expiry
                    # window). Kill our now-orphaned child so we don't double-run
                    # alongside the new owner ("exactly one consumer per user").
                    log.warning("lost lease for %s; terminating orphaned child", user_id)
                    self.kill_fn(child["pid"])
                    self.children.pop(user_id, None)
                elif _spawn_identity(entry) != _spawn_identity(child["entry"]):
                    # Config changed for a still-running user (driver/provider/model/
                    # key) — the consumer's env + home are stale. Respawn in place;
                    # we keep our live lease (just renewed) so no reacquire needed.
                    log.info("config changed for %s; respawning consumer", user_id)
                    self.kill_fn(child["pid"])
                    home = child["home"]
                    pid = self.spawn_fn(entry, user_id, home)
                    self._write_token(user_id, home)
                    leases.renew(user_id, self.owner, ttl=self.lease_ttl, pid=pid,
                                 status="running", driver=entry.get("driver"),
                                 now=self._now())
                    self.children[user_id] = {"pid": pid, "entry": entry, "home": home}
                else:
                    self._write_token(user_id, child["home"])  # refresh short-lived token
                continue

            if child:  # tracked but dead → reap
                log.info("child for %s exited; releasing lease", user_id)
                leases.release(user_id, self.owner, now=self._now())
                self.children.pop(user_id, None)

            if not _genesis_ready_to_spawn(user_id):
                # "先 genesis 后 spawn" (spec §5): don't boot a blank consumer while
                # an import genesis is still distilling persona/facts. Fresh-start
                # (no genesis) and done/failed both fall through to spawn.
                log.info("genesis in progress for %s; deferring spawn this tick", user_id)
                continue
            home = self._home(user_id)
            if not leases.acquire(user_id, driver=entry.get("driver", "claude"),
                                  runtime_home=home, lease_owner=self.owner,
                                  ttl=self.lease_ttl, now=self._now()):
                continue  # another supervisor holds a live lease
            pid = self.spawn_fn(entry, user_id, home)
            self._write_token(user_id, home)  # initial short-lived token for the child
            leases.renew(user_id, self.owner, ttl=self.lease_ttl, pid=pid,
                         status="running", now=self._now())
            self.children[user_id] = {"pid": pid, "entry": entry, "home": home}
            log.info("spawned resident consumer for %s (pid=%s, home=%s)", user_id, pid, home)

    def shutdown(self) -> None:
        for user_id, child in list(self.children.items()):
            self.kill_fn(child["pid"])
            leases.release(user_id, self.owner)
        self.children.clear()


# ---- real-process wiring ----


def _load_roster() -> list[dict]:
    inline = os.environ.get("AGENT_RUNTIME_USERS", "").strip()
    if inline:
        return parse_roster(inline)
    path = os.environ.get("AGENT_RUNTIME_ROSTER", "").strip()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return parse_roster(fh.read())
    return []


def _whoami(api_url: str, api_key: str) -> str:
    """Resolve a roster entry's user_id (lease key) via /v1/users/whoami."""
    try:
        resp = httpx.get(f"{api_url.rstrip('/')}/v1/users/whoami",
                         headers={"X-API-Key": api_key}, timeout=10)
        resp.raise_for_status()
        return str(resp.json().get("user_id") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("whoami failed for a roster entry: %s", e)
        return ""


def _auth_headers(*, api_key: str = "", runtime_token: str = "") -> dict:
    """Auth header for a backend/enclave call. A runtime token (Stage D) is
    preferred when present — it lets the supervisor act for a DB-discovered user
    WITHOUT holding that user's api_key (zero-roster host-all). Both the backend
    whoami and the enclave ``/v1/envelope/decrypt`` accept either credential."""
    if runtime_token:
        return {"X-Feedling-Runtime-Token": runtime_token}
    return {"X-API-Key": api_key}


def _decrypt_provider_key(enclave_url: str, api_key: str = "", envelope: dict | None = None,
                          *, runtime_token: str = "") -> str:
    """JIT-decrypt a provider-key envelope via the enclave (purpose reuses the
    model_api config scheme). Plaintext is handed to the child via env. Auth is
    the api_key, or a runtime token (Stage D zero-roster) when provided."""
    try:
        resp = httpx.post(f"{enclave_url.rstrip('/')}/v1/envelope/decrypt",
                          headers=_auth_headers(api_key=api_key, runtime_token=runtime_token),
                          json={"envelope": envelope, "purpose": "model_api_provider_key"},
                          timeout=20, verify=False)
        resp.raise_for_status()
        return base64.b64decode(resp.json()["plaintext_b64"]).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        log.error("provider key decrypt failed for a roster entry: %s", e)
        return ""


def _fetch_key_envelope(api_url: str, api_key: str = "", *, runtime_token: str = "") -> dict | None:
    """Fetch the user's OWN provider-key envelope ciphertext from the backend
    (Stage B: ``GET /v1/model_api/key_envelope``), so a roster need only carry
    api_keys (or, Stage D, nothing — a runtime token authenticates instead).
    Returns the envelope dict, or None if unconfigured/unreachable."""
    try:
        resp = httpx.get(f"{api_url.rstrip('/')}/v1/model_api/key_envelope",
                         headers=_auth_headers(api_key=api_key, runtime_token=runtime_token), timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        env = resp.json().get("api_key_envelope")
        return env if isinstance(env, dict) else None
    except Exception as e:  # noqa: BLE001
        log.warning("key_envelope fetch failed for a roster entry: %s", e)
        return None


def _resolve_roster(roster: list[dict]) -> list[dict]:
    """Fill each entry's ``user_id`` (whoami) and resolve its provider key.

    Precedence: an explicit roster ``provider_key`` (dev) > a roster-supplied
    ``provider_key_envelope`` > self-fetching the user's own envelope from the
    backend (Stage B). The envelope is enclave-decrypted JIT; plaintext is handed
    to the child via env and never persisted."""
    api_url = os.environ.get("FEEDLING_API_URL", "http://localhost:5001")
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "")
    resolved = []
    for entry in roster:
        user_id = entry.get("user_id") or _whoami(api_url, entry["api_key"])
        if not user_id:
            log.warning("skipping roster entry: could not resolve user_id")
            continue
        out = {**entry, "user_id": user_id}
        if not out.get("provider_key"):
            env = entry.get("provider_key_envelope")
            if not env:
                env = _fetch_key_envelope(api_url, entry["api_key"])
            if env and enclave_url:
                key = _decrypt_provider_key(enclave_url, entry["api_key"], env)
                if key:
                    out["provider_key"] = key
            out.pop("provider_key_envelope", None)
        resolved.append(out)
    return resolved


def _discover_enabled(include_gateway: bool = False, host_all: bool = False) -> dict[str, dict]:
    """Map ``user_id -> {"driver", "provider"}`` for users opted into the hosted
    runtime (Stage C), read straight from the DB the supervisor already connects
    to for leases. The provider + model + base_url ride along so a codex user can
    be wired native (openai) vs LiteLLM-gateway (gemini/openrouter/…) at spawn, and
    the gateway routing can be built per user. ``include_gateway`` mirrors whether
    the LiteLLM gateway is running — when off, gateway-only providers are excluded
    so they aren't spawned against a proxy that isn't there. ``host_all`` mirrors
    FEEDLING_HOST_ALL: it discovers every configured user (no per-user flag), so the
    set matches what the backend cutover routes to hosted — otherwise unflagged
    users would be routed to a consumer that was never spawned."""
    return {u["user_id"]: {"driver": u["driver"], "provider": u.get("provider", ""),
                           "model": u.get("model", ""), "base_url": u.get("base_url", "")}
            for u in db.list_agent_runtime_enabled_users(include_gateway=include_gateway,
                                                         host_all=host_all)}


def _apply_discovery(roster: list[dict], enabled: dict[str, dict]) -> list[dict]:
    """Filter the (credential-bearing) roster to users the backend has enabled,
    taking driver/provider/model/base_url from the backend flag — the
    /v1/model_api/driver setter is the gradual-migration control plane. Roster
    still supplies api_keys until Stage D's runtime-token. Entries must already
    have ``user_id`` (whoami)."""
    out = []
    for entry in roster:
        uid = entry.get("user_id")
        info = enabled.get(uid)
        if info is not None:
            out.append({**entry, "driver": info["driver"],
                        "provider": info.get("provider", ""),
                        "model": info.get("model", ""),
                        "base_url": info.get("base_url", "")})
    return out


def _resolve_discovered(enabled: dict, *, mint_token, api_url: str, enclave_url: str,
                        cache: dict) -> list[dict]:
    """Stage D zero-roster: build credential-bearing entries for DB-discovered
    users WITHOUT any api_key. For each user_id the supervisor mints a short-lived
    runtime token, self-fetches the provider-key envelope and enclave-decrypts it
    JIT (auth = token). ``cache`` (user_id -> {"sig", "provider_key"}) avoids
    re-decrypting an unchanged envelope every tick. Entries carry no api_key — the
    consumer authenticates with the token file the supervisor writes per tick."""
    out = []
    for uid, info in enabled.items():
        tok = mint_token(uid)
        env = _fetch_key_envelope(api_url, runtime_token=tok)
        cached = cache.get(uid)
        provider_key = ""
        if env and enclave_url:
            sig = json.dumps(env, sort_keys=True)
            if cached and cached.get("sig") == sig:
                provider_key = cached["provider_key"]
            else:
                decrypted = _decrypt_provider_key(enclave_url, envelope=env, runtime_token=tok)
                if decrypted:
                    provider_key = decrypted
                    cache[uid] = {"sig": sig, "provider_key": decrypted}
                elif cached:
                    # Decrypt failed (transient) on a changed envelope — keep the last
                    # good key so a healthy consumer isn't respawned keyless this tick.
                    provider_key = cached["provider_key"]
        elif cached:
            # Envelope fetch failed (transient) — fall back to the last good key
            # rather than dropping it (which _spawn_identity reads as a config change).
            provider_key = cached["provider_key"]
        entry = {"user_id": uid, "driver": info["driver"], "provider": info.get("provider", ""),
                 "model": info.get("model", ""), "base_url": info.get("base_url", "")}
        if provider_key:
            entry["provider_key"] = provider_key
        # Carry the freshly-minted runtime token so ProcessSpawner.spawn can decrypt
        # the genesis_persona blob at spawn time (zero-roster has no api_key — cutover
        # gate 3 P0). Minted before spawn (this tick), so timing is solved. Excluded
        # from _spawn_identity, so per-tick rotation does not bounce the consumer.
        if tok:
            entry["runtime_token"] = tok
        out.append(entry)
    return out


def _post_verify_loop(api_url: str, headers: dict) -> bool:
    """POST /v1/chat/verify_loop — a synthetic ping the consumer answers, which
    flips the user's ``chat_loop_verified`` and opens the bootstrap gate. Returns
    whether the verify passed (an agent reply landed)."""
    try:
        resp = httpx.post(f"{api_url.rstrip('/')}/v1/chat/verify_loop",
                          headers=headers, json={"timeout_sec": 30}, timeout=40)
        resp.raise_for_status()
        return bool(resp.json().get("passing"))
    except Exception as e:  # noqa: BLE001
        log.warning("autoverify post failed: %s", e)
        return False


# Backoff (seconds) between verify_loop attempts, indexed by consecutive failure
# count (capped at the last entry). A user that cannot pass yet — e.g. still at
# `needs_identity`, where the verify reply is gate-rejected — must NOT be re-probed
# every tick; this spaces retries out so a genuinely-advancing user still gets
# verified soon while a stuck one generates little traffic.
_AUTOVERIFY_BACKOFF = [0.0, 30.0, 120.0, 600.0, 1800.0]


def _maybe_autoverify(user_id: str, *, mint_token, api_url: str, state: dict,
                      post_verify=_post_verify_loop, now=time.time) -> None:
    """Open the bootstrap gate for a freshly-hosted user by running verify_loop.
    ``state`` (user_id -> {"done", "fails", "next"}) makes it idempotent AND backed
    off: a passed user is marked ``done`` and never re-probed; a failed attempt
    schedules the next try with increasing backoff instead of every tick."""
    s = state.get(user_id)
    if s is not None and s.get("done"):
        return
    t = now()
    if s is not None and t < s.get("next", 0.0):
        return                                  # within the backoff window
    if post_verify(api_url, _auth_headers(runtime_token=mint_token(user_id))):
        state[user_id] = {"done": True}
        return
    fails = (s.get("fails", 0) if s else 0) + 1
    delay = _AUTOVERIFY_BACKOFF[min(fails, len(_AUTOVERIFY_BACKOFF) - 1)]
    state[user_id] = {"done": False, "fails": fails, "next": t + delay}


# A host user is blocked from spawning only while an import genesis is actively
# running. No genesis_state blob = fresh start (never uploaded) → allow. done/failed
# → allow (failed degrades to a normal spawn, never deadlocks the user). Coarse
# status is maintained by Codex's genesis via db.set_blob(user_id,'genesis_state').
_GENESIS_BLOCKING_STATUS = frozenset({"uploaded", "finalizing", "processing"})


def _genesis_status_blocks_spawn(blob) -> bool:
    """Pure gate logic (no DB) — True only when genesis is actively in progress."""
    if not isinstance(blob, dict):
        return False  # no genesis underway → fresh start, allow
    return str(blob.get("status") or "").strip().lower() in _GENESIS_BLOCKING_STATUS


def _genesis_ready_to_spawn(user_id: str) -> bool:
    """Gate read: block spawn only while this user's import genesis is in progress.
    Read errors → allow (don't wedge spawning on a transient DB hiccup). Local ``db``
    import keeps this module pure-unit importable without a DB/PG dependency."""
    try:
        import db  # local import: avoid a module-level DB dep for pure-unit tests
        blob = db.get_blob(user_id, "genesis_state")
    except Exception as e:
        log.warning("genesis_state read failed for %s; allowing spawn: %s", user_id, e)
        return True
    return not _genesis_status_blocks_spawn(blob)


def _spawn_identity(entry: dict) -> tuple:
    """The spawn-determining fields of a roster entry — when any changes, the
    running consumer's env/home is stale and it must be respawned. Mirrors what
    ``spawners.consumer_env`` / ``agent_home_files`` consume. The upstream
    ``provider_key`` is EXCLUDED for gateway users (it goes to LiteLLM, not the
    consumer env), so rotating it doesn't bounce the consumer."""
    driver = (entry.get("driver") or "claude").strip().lower()
    gateway = spawners._codex_transport(entry) == "gateway"
    return (
        entry.get("api_key") or "",
        driver,
        entry.get("cli_cmd") or "",
        entry.get("provider") or "",
        entry.get("model") or "",
        "" if gateway else (entry.get("provider_key") or ""),
    )


def _gateway_entries(roster: list[dict]) -> list[dict]:
    """Codex users that must be bridged through the in-CVM LiteLLM gateway
    (gemini/openrouter/openai_compatible). Each carries the REAL upstream
    provider/model/key for building the per-user LiteLLM routing."""
    out = []
    for e in roster:
        if spawners._codex_transport(e) == "gateway":
            out.append({
                "user_id": e["user_id"],
                "provider": e.get("provider", ""),
                "model": e.get("model") or "",
                "base_url": e.get("base_url") or "",
                "provider_key": e.get("provider_key") or "",
            })
    return out


def _drop_gateway_users(roster: list[dict]) -> list[dict]:
    """Remove codex users that would need the LiteLLM gateway — used when the
    gateway is DISABLED. Without a running proxy, spawning them as gateway codex
    yields a config pointing at nothing (and usually no gateway key), so they must
    be dropped rather than half-spawned. Native (openai) + claude users stay."""
    kept = [e for e in roster if spawners._codex_transport(e) != "gateway"]
    dropped = [e.get("user_id") for e in roster if spawners._codex_transport(e) == "gateway"]
    if dropped:
        log.warning("litellm gateway disabled — dropping %d gateway codex user(s): %s",
                    len(dropped), dropped)
    return kept


def _wire_gateway_models(roster: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (roster, gateway_entries). Gateway codex entries are rewritten so
    codex REQUESTS the ``gw-<uid>`` model (which LiteLLM maps to the real upstream
    model+key); the returned gateway_entries retain the real model for building
    the LiteLLM config. Non-gateway entries pass through unchanged."""
    gateways = _gateway_entries(roster)
    if not gateways:
        return roster, []
    gateway_uids = {g["user_id"] for g in gateways}
    wired = [
        {**e, "model": litellm_gateway.gateway_model_id(e["user_id"])}
        if e.get("user_id") in gateway_uids and spawners._codex_transport(e) == "gateway"
        else e
        for e in roster
    ]
    return wired, gateways


def _effective_roster(base_roster: list[dict], *, autodiscover: bool,
                      gateway_enabled: bool,
                      host_all_discovered: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Compute the roster to actually run this tick + the gateway routing entries.

    ``base_roster`` carries credentials (resolved once at boot). Each tick we:
    optionally intersect it with the backend-enabled set (autodiscover — the
    gradual-migration control plane); drop gateway-only codex users when the
    gateway is off (no proxy to reach); and when the gateway is on, rewrite those
    users' requested model to ``gw-<uid>`` (returning the real models as gateway
    entries for the LiteLLM config). An empty result is fine — the supervisor
    idles rather than exiting.

    ``host_all_discovered`` (Stage D host-all) supplies pre-credentialed entries
    resolved from the DB via runtime token — they BECOME the roster (no api_key
    roster needed). A ``base_roster`` entry with the same user_id still wins (dev
    override / pinned local credential)."""
    if host_all_discovered is not None:
        by_uid = {e["user_id"]: e for e in host_all_discovered if e.get("user_id")}
        for e in base_roster:
            if e.get("user_id"):
                by_uid[e["user_id"]] = e        # dev override wins
        roster = list(by_uid.values())
    elif autodiscover:
        enabled = _discover_enabled(include_gateway=gateway_enabled)
        roster = _apply_discovery(base_roster, enabled)
    else:
        roster = base_roster
    if not gateway_enabled:
        return _drop_gateway_users(roster), []
    return _wire_gateway_models(roster)


_GENESIS_TICK_DEFAULT_SEC = 30
_GENESIS_TOKEN_TTL_DEFAULT_SEC = 7200  # a large import can spend >15min in LLM calls


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _genesis_worker_should_start(*, enabled: str, secret: str, enclave_url: str) -> bool:
    """Activate the in-CVM genesis worker only when explicitly enabled AND its
    prerequisites are present (runtime-token secret to mint scoped tokens, enclave
    URL to decrypt chunks). Default OFF — landing the hook must not run genesis
    until the env opts in; missing prereqs stay dormant rather than fail jobs."""
    if not _truthy(enabled):
        return False
    return bool(str(secret or "").strip()) and bool(str(enclave_url or "").strip())


def _genesis_worker_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event):
    """Background daemon: poll genesis.worker.tick. Separate from sup.tick so a
    long genesis run never blocks chat-consumer supervision. Worker claim is
    FOR UPDATE SKIP LOCKED, so multiple supervisors/threads are safe."""
    from genesis import worker as genesis_worker
    while not stop_event.is_set():
        try:
            genesis_worker.tick(api_url=api_url, enclave_url=enclave_url,
                                mint_runtime_token=mint_genesis, max_jobs=1)
        except Exception as e:  # noqa: BLE001
            log.exception("genesis worker tick failed: %s", e)
        stop_event.wait(interval)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    owner = f"{socket.gethostname()}:{os.getpid()}"
    lease_ttl = float(os.environ.get("AGENT_LEASE_TTL_SEC", "60"))
    interval = float(os.environ.get("AGENT_TICK_INTERVAL_SEC", "15"))
    data_root = os.environ.get("AGENT_DATA_ROOT", "/agent-data")
    isolation = os.environ.get("AGENT_RUNTIME_ISOLATION", "process").strip().lower()

    gateway_enabled = os.environ.get("FEEDLING_LITELLM_ENABLE", "").strip().lower() in ("1", "true", "yes")
    autodiscover = os.environ.get("AGENT_RUNTIME_AUTODISCOVER", "").strip().lower() in ("1", "true", "yes")
    # Stage D host-all: every configured user is hosted with NO api_key roster —
    # the supervisor mints a runtime token per DB-discovered user and resolves the
    # provider key with it. Requires FEEDLING_RUNTIME_TOKEN_SECRET (set below);
    # without the secret there is no token to authenticate with, so host-all is inert.
    host_all = os.environ.get("FEEDLING_HOST_ALL", "").strip().lower() in ("1", "true", "yes")
    api_url = os.environ.get("FEEDLING_API_URL", "http://localhost:5001")
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "")

    # Credentials resolved once (whoami + envelope decrypt are network calls). The
    # backend-enabled intersection + gateway wiring re-run each tick off this base,
    # so enabling/disabling a user (or the gateway) takes effect without a redeploy.
    base_roster = _resolve_roster(_load_roster())
    if not base_roster and not autodiscover and not host_all:
        log.warning("no roster and autodiscover off — supervisor will idle "
                    "(set AGENT_RUNTIME_USERS or AGENT_RUNTIME_AUTODISCOVER=1)")

    spawn_fn, alive_fn, kill_fn = spawners.get_spawner(isolation)

    # Stage D: if the shared HMAC secret is set, the supervisor mints a per-user,
    # short-lived runtime token and writes it to the user's home, refreshed each
    # tick. The consumer authenticates with the token instead of the long-term
    # API key. Secret unset → no token files → consumer stays on the API key.
    token_writer = None
    mint_token = None
    secret_raw = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    if secret_raw:
        secret = secret_raw.encode("utf-8")
        ttl = float(os.environ.get("AGENT_RUNTIME_TOKEN_TTL_SEC", "900"))
        scopes = ["chat", "memory", "identity", "perception", "envelope_decrypt"]

        def mint_token(user_id):
            return runtime_token.mint(secret, user_id=user_id, runtime_instance_id=owner,
                                      scope=scopes, ttl=ttl)

        def _mint_and_write(user_id, home):
            spawners.write_runtime_token(home, mint_token(user_id))

        token_writer = _mint_and_write

    # in-CVM LiteLLM gateway (codex non-openai providers). Off by default: only
    # when FEEDLING_LITELLM_ENABLE is set do we run a LiteLLM proxy that holds the
    # gateway users' upstream keys (in the proxy's env, never on disk) and rewrite
    # those users to the gw-<uid> model. Codex reaches it at 127.0.0.1:<port>.
    gateway_mgr = None
    if gateway_enabled:
        port = int(os.environ.get("FEEDLING_LITELLM_PORT", "4000"))
        cfg_path = os.environ.get("FEEDLING_LITELLM_CONFIG", f"{data_root}/litellm.yaml")
        os.environ.setdefault("FEEDLING_LITELLM_BASE_URL", f"http://127.0.0.1:{port}/v1")
        gateway_mgr = litellm_gateway.GatewayManager(config_path=cfg_path, port=port)
        log.info("litellm gateway enabled — config=%s port=%d", cfg_path, port)

    if host_all and mint_token is None:
        log.warning("FEEDLING_HOST_ALL set but FEEDLING_RUNTIME_TOKEN_SECRET is not — "
                    "host-all is inert (no token to authenticate zero-roster users)")
    host_all_active = host_all and mint_token is not None
    cred_cache: dict = {}
    autoverify_state: dict = {}    # user_id -> {done, fails, next} (backed-off gate-open)
    autoverify_inflight: set = set()

    sup = Supervisor(
        owner=owner, lease_ttl=lease_ttl, data_root=data_root,
        spawn_fn=spawn_fn, alive_fn=alive_fn, kill_fn=kill_fn,
        token_writer=token_writer,
    )
    log.info("supervisor up — owner=%s base_users=%d autodiscover=%s host_all=%s gateway=%s ttl=%.0fs",
             owner, len(base_roster), autodiscover, host_all_active, gateway_enabled, lease_ttl)

    # Genesis CVM worker — runs in its own daemon thread (never inline in the tick
    # loop). Default OFF; activates only when FEEDLING_GENESIS_WORKER_ENABLED is set
    # AND a runtime-token secret + enclave URL are present (else dormant + warn).
    genesis_enabled = os.environ.get("FEEDLING_GENESIS_WORKER_ENABLED", "")
    genesis_stop = threading.Event()
    if _genesis_worker_should_start(enabled=genesis_enabled, secret=secret_raw, enclave_url=enclave_url):
        g_secret = secret_raw.encode("utf-8")
        genesis_ttl = float(os.environ.get(
            "FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC", str(_GENESIS_TOKEN_TTL_DEFAULT_SEC)))
        g_interval = float(os.environ.get(
            "FEEDLING_GENESIS_WORKER_INTERVAL_SEC", str(_GENESIS_TICK_DEFAULT_SEC)))

        def mint_genesis(user_id, scopes=None):
            return runtime_token.mint(
                g_secret, user_id=user_id, runtime_instance_id=owner,
                scope=list(scopes or ["envelope_decrypt", "genesis"]), ttl=genesis_ttl)

        threading.Thread(
            target=_genesis_worker_loop, daemon=True,
            kwargs={"api_url": api_url, "enclave_url": enclave_url,
                    "mint_genesis": mint_genesis, "interval": g_interval,
                    "stop_event": genesis_stop},
        ).start()
        log.info("genesis worker enabled — interval=%.0fs token_ttl=%.0fs", g_interval, genesis_ttl)
    elif _truthy(genesis_enabled):
        log.warning("FEEDLING_GENESIS_WORKER_ENABLED set but prerequisites missing "
                    "(need FEEDLING_RUNTIME_TOKEN_SECRET + FEEDLING_ENCLAVE_URL) — genesis worker dormant")

    try:
        while True:
            try:
                # Re-derive the live roster each tick so enabling/disabling a user
                # (or the gateway) takes effect without restarting the supervisor.
                discovered = None
                if host_all_active:
                    enabled = _discover_enabled(include_gateway=gateway_enabled, host_all=True)
                    discovered = _resolve_discovered(
                        enabled, mint_token=mint_token, api_url=api_url,
                        enclave_url=enclave_url, cache=cred_cache)
                roster, gateways = _effective_roster(
                    base_roster, autodiscover=autodiscover, gateway_enabled=gateway_enabled,
                    host_all_discovered=discovered)
                if gateway_mgr is not None:
                    gateway_mgr.reconcile(gateways)
                sup.tick(roster)
                # Stage D host-all: a freshly-hosted user is dead-ended at
                # needs_live_connection until verify_loop runs once. Open the gate
                # in the background (the POST blocks ~30s for the consumer's reply).
                # Only for users THIS supervisor owns a running consumer for (sup.children)
                # — so the verify ping can actually be answered — and backed off so a
                # user stuck earlier in bootstrap isn't re-probed every tick.
                if host_all_active:
                    for uid in list(sup.children):
                        if uid in autoverify_inflight or autoverify_state.get(uid, {}).get("done"):
                            continue
                        autoverify_inflight.add(uid)

                        def _autoverify(uid=uid):
                            try:
                                _maybe_autoverify(uid, mint_token=mint_token, api_url=api_url,
                                                  state=autoverify_state)
                            finally:
                                autoverify_inflight.discard(uid)

                        threading.Thread(target=_autoverify, daemon=True).start()
            except Exception as e:  # noqa: BLE001
                log.exception("supervisor tick failed: %s", e)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted; releasing leases")
        return 0
    finally:
        genesis_stop.set()
        sup.shutdown()
        if gateway_mgr is not None:
            gateway_mgr.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

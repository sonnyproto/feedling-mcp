"""ASGI-specific env knobs (ASGI-migration plan §5.4).

Read once at process start. Kept separate from ``core.config`` so the ASGI
capacity/tuning surface is discoverable in one place and does not entangle with
the legacy Flask config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Bounded sync->thread bridge for sync DB / blocking calls. anyio's default
    # global thread limiter is only 40 tokens — well under the legacy 128-thread
    # gunicorn worker — so we MUST raise it explicitly in the lifespan or heavy
    # routes get silently throttled (plan §5.2 / §7.2).
    db_threads: int = _int("FEEDLING_ASGI_DB_THREADS", 64)

    # Process-lifetime httpx.AsyncClient pool sizing (plan §5.6). Two clients:
    # internal (backend<->enclave/service) and provider (LLM), so a slow
    # provider can't starve internal calls.
    http_max_connections: int = _int("FEEDLING_HTTP_MAX_CONNECTIONS", 200)
    http_max_keepalive: int = _int("FEEDLING_HTTP_MAX_KEEPALIVE", 50)

    # Structured ASGI access log (plan §5.9). On by default.
    access_log: bool = _bool("FEEDLING_ASGI_ACCESS_LOG", True)

    # Async long-poll waiter caps (plan §5.4 / §9). Global ceiling + per-user
    # per-channel ceiling bound the failure domain: over the cap a poll sheds to
    # an immediate timed-out response instead of parking a waiter.
    poller_max_active: int = _int("FEEDLING_POLLER_MAX_ACTIVE", 5000)
    poller_max_per_user_chat: int = _int("FEEDLING_POLLER_MAX_PER_USER_CHAT", 2)
    poller_max_per_user_proactive: int = _int("FEEDLING_POLLER_MAX_PER_USER_PROACTIVE", 2)

    # Whether the lifespan starts the external-binding background services —
    # the cross-worker wake-bus LISTEN loop and the :9998 screen-WS leader
    # election. OFF by default so the dev-time parallel instance (:5005, plan
    # §8) sharing the live backend's DB does not spin up a second WS-leader
    # contender / duplicate listeners during pure skeleton validation. Flipped
    # ON at cutover, when the ASGI app IS the backend. init_schema, the
    # threadpool limiter, and the httpx clients are always started (they are
    # safe and required) — only these two DB/port-binding services are gated.
    start_background: bool = _bool("FEEDLING_ASGI_BACKGROUND", False)


settings = Settings()

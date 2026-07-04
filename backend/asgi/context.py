"""Request-scoped context vars — the ASGI equivalent of Flask ``g`` (plan §5.9).

``current_user_id`` is set by the auth dependency/middleware once a request is
authenticated and read by the access-log middleware, so both live off one
source (mirroring how the Flask wrapper writes ``g.user_id``). ContextVars are
the correct primitive here: they are isolated per asyncio task, so concurrent
requests on one worker never see each other's identity.
"""

from __future__ import annotations

import contextvars

# "-" mirrors the Flask access log's default when no user is resolved.
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_id", default="-"
)

# Verified runtime-token claims for the authenticated request (None for api-key
# auth or when the runtime-token feature is off) — the flask-free successor to
# ``flask.g.runtime_token_claims``, read by scope re-checks off the event loop.
current_runtime_claims: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_runtime_claims", default=None
)

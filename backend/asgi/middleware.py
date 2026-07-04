"""Access-log middleware + exception handlers (ASGI-migration plan §5.9 / §3.1).

The access log is a pure-ASGI middleware (not Starlette ``BaseHTTPMiddleware``)
so it can observe client-disconnect **cancellation** and still emit a line — the
exact blind spot that misled the 2026-07-02 long-poll investigation, where
Flask's ``after_request`` only logged requests that returned normally.

Three hard parity/security requirements from the Flask ``after_request`` (§5.9):
1. ``?key=`` is REDACTED case-insensitively — legacy auth allows the API key in
   the URL and it must never reach the logs.
2. Cancelled / errored requests get a line too (``status=cancelled`` / ``500``).
3. (follow-up) periodic dump of slow in-flight requests — UvicornWorker has no
   per-request timeout to reap a wedged handler (§5.2). Deferred until there are
   real long-running native routes (PR 5+); the redact + cancellation lines are
   the load-bearing parity items and are done here.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qsl, urlencode

from accounts import auth_core
from asgi import responses
from asgi.context import current_user_id
from asgi.settings import settings

try:  # Starlette re-exports the same class; import defensively.
    from starlette.exceptions import HTTPException as StarletteHTTPException
except Exception:  # pragma: no cover
    from fastapi import HTTPException as StarletteHTTPException  # type: ignore


def _display_path(scope) -> str:
    """path (+ query), with any ``key`` param redacted case-insensitively."""
    path = scope.get("path", "")
    qs = scope.get("query_string", b"").decode("latin-1")
    if not qs:
        return path
    pairs = parse_qsl(qs, keep_blank_values=True)
    if any(k.lower() == "key" for k, _ in pairs):
        redacted = [(k, "REDACTED" if k.lower() == "key" else v) for k, v in pairs]
        return f"{path}?{urlencode(redacted)}"
    return f"{path}?{qs}"


class AccessLogMiddleware:
    """One structured ``[req]`` line per request (handler time + on-wire size).

    Mirrors app.py's ``_access_log_end`` format so ``phala cvms logs`` reads the
    same across both backends; skips ``/healthz`` (probe noise) like Flask does.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not settings.access_log or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        info = {"status": 0, "bytes": "?", "enc": "-"}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                info["status"] = message["status"]
                for key, value in message.get("headers", []):
                    kl = key.decode("latin-1").lower()
                    if kl == "content-length":
                        info["bytes"] = value.decode("latin-1")
                    elif kl == "content-encoding":
                        info["enc"] = value.decode("latin-1")
            await send(message)

        method = scope.get("method", "-")
        disp = _display_path(scope)

        def _emit(status_field) -> None:
            dur_ms = int((time.monotonic() - start) * 1000)
            print(
                f"[req] uid={current_user_id.get()} {method} {disp} "
                f"status={status_field} bytes={info['bytes']} enc={info['enc']} dur_ms={dur_ms}",
                flush=True,
            )

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:  # includes asyncio.CancelledError
            # CancelledError (client disconnect / shutdown) is a BaseException,
            # not Exception — catch broadly, log, and re-raise so cancellation
            # semantics are preserved.
            import asyncio

            _emit("cancelled" if isinstance(exc, asyncio.CancelledError) else "500")
            raise
        else:
            _emit(info["status"])


def register_exception_handlers(app) -> None:
    """Map typed auth errors + bare HTTP errors to the fixed Flask JSON bodies."""

    @app.exception_handler(auth_core.AuthError)
    async def _auth_error(request, exc: auth_core.AuthError):
        return responses.json_error(exc.status_code, {"error": exc.code})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request, exc: StarletteHTTPException):
        body = responses.ERROR_BODIES.get(exc.status_code)
        if body is not None:
            return responses.json_error(exc.status_code, body)
        return responses.json_error(exc.status_code, {"detail": exc.detail})

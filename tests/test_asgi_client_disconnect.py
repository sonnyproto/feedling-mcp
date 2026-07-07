"""Client-disconnect parity for ``read_json_silent`` (2026-07-05 prod noise).

iOS background uploads (``/v1/perception/report``, ``/v1/device/events``) can
drop the connection mid-body. Under WSGI/Flask that surfaced as a truncated
body -> JSON parse failure -> ``get_json(silent=True)`` returns None. Under
Starlette, ``request.body()`` raises ``ClientDisconnect`` instead, and an
uncaught one bubbles to ServerErrorMiddleware as a 500 + traceback — pure log
noise, since the client is already gone. Parity: ``read_json_silent`` must
swallow it and return None, exactly like a parse failure.

Routes that read the body/form directly (copytext, diagnostics, proactive,
genesis) bypass ``read_json_silent``, so the app-level exception handler
(``register_exception_handlers``) must also absorb ``ClientDisconnect`` — as a
nginx-style 499 the peer never sees — instead of a 500 traceback.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from fastapi import FastAPI  # noqa: E402
from starlette.requests import Request  # noqa: E402

from asgi import middleware  # noqa: E402
from asgi.http import read_json_silent  # noqa: E402

_SCOPE = {
    "type": "http",
    "method": "POST",
    "path": "/v1/perception/report",
    "headers": [(b"content-type", b"application/json")],
    "query_string": b"",
}


def test_disconnect_before_body_returns_none():
    async def receive():
        return {"type": "http.disconnect"}

    result = asyncio.run(read_json_silent(Request(dict(_SCOPE), receive=receive)))
    assert result is None


def test_app_handler_absorbs_disconnect_from_raw_body_read():
    """A route calling ``await request.body()`` directly (no read_json_silent)
    must not 500 when the client is gone — the registered handler turns the
    ClientDisconnect into a 499 that never leaves the server."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)

    @app.post("/raw")
    async def raw(request: Request):  # pragma: no cover - body read raises
        await request.body()
        return {"ok": True}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/raw",
        "raw_path": b"/raw",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/octet-stream")],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    async def receive():
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    # Without the handler this raises ClientDisconnect out of the app after
    # ServerErrorMiddleware has already emitted a 500 + traceback.
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 499


def test_disconnect_mid_body_returns_none():
    messages = iter(
        [
            {"type": "http.request", "body": b'{"event": "sc', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    result = asyncio.run(read_json_silent(Request(dict(_SCOPE), receive=receive)))
    assert result is None

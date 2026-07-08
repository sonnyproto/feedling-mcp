"""request_id：access-log 中间件生成 → contextvar → 响应头（spec Phase A / A2）。

Run:  python -m pytest tests/test_request_id.py -q
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import replace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi import context as asgi_context  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi.settings import settings  # noqa: E402
from fastapi import FastAPI  # noqa: E402

# NOTE: Settings is a frozen dataclass (backend/asgi/settings.py), so
# `monkeypatch.setattr(settings, "access_log", True)` raises FrozenInstanceError.
# Patch the module-level name middleware.py actually reads instead.


def _force_access_log_on(monkeypatch):
    monkeypatch.setattr(middleware, "settings", replace(settings, access_log=True))


def _build_app():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/echo-rid")
    async def echo_rid():
        return {"rid": asgi_context.current_request_id.get()}

    return middleware.AccessLogMiddleware(app)


def _get(app, path):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(path)
    return asyncio.run(go())


def test_request_id_format():
    rid = asgi_context.new_request_id()
    assert re.fullmatch(r"req_[0-9a-f]{8}", rid)
    assert asgi_context.new_request_id() != rid


def test_header_and_contextvar_agree(monkeypatch):
    _force_access_log_on(monkeypatch)
    resp = _get(_build_app(), "/echo-rid")
    rid_header = resp.headers.get("x-request-id", "")
    assert re.fullmatch(r"req_[0-9a-f]{8}", rid_header)
    assert resp.json()["rid"] == rid_header   # handler 里读到的和头上回带的是同一个


def test_two_requests_get_distinct_ids(monkeypatch):
    _force_access_log_on(monkeypatch)
    app = _build_app()
    a = _get(app, "/echo-rid").headers["x-request-id"]
    b = _get(app, "/echo-rid").headers["x-request-id"]
    assert a != b

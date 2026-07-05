from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from enclave import backend_client  # noqa: E402


def test_forward_auth_headers_priority():
    assert backend_client.forward_auth_headers("ak", "rt") == {"X-Feedling-Runtime-Token": "rt"}
    assert backend_client.forward_auth_headers("ak", "") == {"X-API-Key": "ak"}
    assert backend_client.forward_auth_headers("", "") == {}


def test_backend_get_roundtrip(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"user_id": "usr_1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(backend_client, "_client", client)
    out = asyncio.run(backend_client.backend_get(
        "/v1/users/whoami", {"X-API-Key": "k"}, params={"a": "1"}))
    assert out == {"user_id": "usr_1"}
    assert seen["url"].endswith("/v1/users/whoami?a=1")
    assert seen["headers"]["x-api-key"] == "k"


def test_backend_get_raises_on_http_status(monkeypatch):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    monkeypatch.setattr(backend_client, "_client", client)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(backend_client.backend_get("/v1/users/whoami", {}))


def test_aclose_resets_singleton(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    monkeypatch.setattr(backend_client, "_client", client)
    asyncio.run(backend_client.aclose())
    assert backend_client._client is None
    assert client.is_closed

"""Regression tests for the enclave decrypt-and-serve routes' error mapping.

Background (2026-06-03 IO-messages-not-connecting incident): the consumer
reported the enclave returning a bare 500 on /v1/chat/history ("all decrypt
sources failed"). Two unguarded infra calls in the route handlers turn a
transient dependency failure into an unhandled exception (HTTP 500):

  1. `_whoami_cached(api_key)` is wrapped only by `except httpx.HTTPStatusError`.
     A backend connect/timeout raises `httpx.ConnectError` / `ReadTimeout`
     (subclasses of httpx.HTTPError but NOT HTTPStatusError) → uncaught → 500.
     This is the reentrant whoami bottleneck (gunicorn -w 1 backend +
     backend->enclave->backend whoami + slow CVM egress) surfacing as a 500.

  2. `_get_or_derive_content_sk()` (the only runtime dstack round-trip) is
     called unguarded in every route → a dstack hiccup → uncaught → 500.

A 500 is wrong on both counts: it's an unhandled exception (opaque to the
caller and the consumer, which only sees "500" with no body), and it implies
an enclave bug rather than an upstream dependency being briefly unavailable.
These should map to 502 (bad gateway: backend unreachable) / 503 (service
unavailable: key material not derivable right now), which are retryable and
self-describing.

ASGI-era note: `enclave.auth.resolve_read_caller` now owns the
httpx.HTTPError -> 502 / HTTPStatusError(401) -> 401 mapping centrally (it is
shared by every decrypt-and-serve route), and each route wraps its own
`keys.get_content_sk()` call to map any exception to 503. This file drives
those mappings through the real routes via `backend_client.backend_get` /
`keys.get_content_sk` fakes rather than poking removed Flask-era internals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


_FRAME_ID = "ab" * 8  # 16 hex chars, matches the route's ^[a-f0-9]{16,64}$


# Routes that resolve the caller via `auth.resolve_read_caller` (the cached
# resolver used by the read-only decrypt-and-serve handlers). /v1/envelope/decrypt
# is excluded: it resolves live via `auth.whoami_live` and already maps
# httpx.HTTPError to 502 the same way.
_WHOAMI_CACHED_ROUTES = [
    "/v1/chat/history",
    "/v1/memory/list",
    "/v1/identity/get",
    f"/v1/screen/frames/{_FRAME_ID}/decrypt",
    f"/v1/screen/frames/{_FRAME_ID}/image",
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.mark.parametrize("path", _WHOAMI_CACHED_ROUTES)
def test_whoami_connect_error_maps_to_bad_gateway_not_500(client, monkeypatch, path):
    """A backend connect/timeout during whoami must not surface as a bare 500."""
    async def _boom(path, headers, params=None):
        raise httpx.ConnectError("backend unreachable")
    monkeypatch.setattr(backend_client, "backend_get", _boom)

    resp = client.get(path, headers={"X-API-Key": "testkey"})

    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code in (502, 503), resp.get_data(as_text=True)


@pytest.mark.parametrize("path", _WHOAMI_CACHED_ROUTES)
def test_key_derivation_failure_maps_to_503_not_500(client, monkeypatch, path):
    """A dstack/key-derivation failure must not surface as a bare 500."""
    async def fake_backend_get(_path, headers, params=None):
        if _path == "/v1/users/whoami":
            return {"user_id": "usr_test"}
        if _path == "/v1/identity/get":
            # A non-null, non-local_only identity so the route proceeds to
            # content_sk derivation instead of short-circuiting on `identity
            # is None` before ever calling get_content_sk().
            return {"identity": {"v": 1, "visibility": "shared",
                                 "created_at": None, "updated_at": None}}
        # chat history / memory list / frame envelope: the route reaches
        # content_sk derivation unconditionally after this fetch succeeds, so
        # the exact payload shape doesn't matter here.
        return {"messages": [], "total": 0, "moments": [],
                "v": 1, "id": _FRAME_ID, "K_enclave": "x"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def _boom():
        raise RuntimeError("dstack socket unavailable")
    monkeypatch.setattr(keys, "get_content_sk", _boom)

    resp = client.get(path, headers={"X-API-Key": "testkey"})

    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code == 503, resp.get_data(as_text=True)


def test_envelope_decrypt_key_derivation_failure_maps_to_503_not_500(client, monkeypatch):
    """The live-resolver route also guards its unguarded content_sk derivation."""
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_test"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def _boom():
        raise RuntimeError("dstack socket unavailable")
    monkeypatch.setattr(keys, "get_content_sk", _boom)

    resp = client.post(
        "/v1/envelope/decrypt",
        headers={"X-API-Key": "testkey"},
        json={"envelope": {"id": "x", "v": 1}},
    )

    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code == 503, resp.get_data(as_text=True)

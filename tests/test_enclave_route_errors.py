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
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import enclave_app  # noqa: E402


_FRAME_ID = "ab" * 8  # 16 hex chars, matches the route's ^[a-f0-9]{16,64}$


# Routes that resolve the caller via `_whoami_cached` (the cached resolver used
# by the read-only decrypt-and-serve handlers). /v1/envelope/decrypt is
# excluded: it resolves live via `_flask_get` and already maps httpx.HTTPError
# to 502.
_WHOAMI_CACHED_ROUTES = [
    ("/v1/chat/history", {"messages": [], "total": 0}),
    ("/v1/memory/list", {"moments": [], "total": 0}),
    ("/v1/identity/get", {"identity": {"v": 1, "visibility": "shared",
                                       "created_at": None, "updated_at": None}}),
    (f"/v1/screen/frames/{_FRAME_ID}/decrypt", {"v": 1, "id": _FRAME_ID}),
    (f"/v1/screen/frames/{_FRAME_ID}/image", {"v": 1, "id": _FRAME_ID}),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_app._state, "ready", True)
    monkeypatch.setitem(enclave_app._state, "error", None)
    enclave_app.app.config.update(TESTING=False)  # let the app map errors, not re-raise
    with enclave_app.app.test_client() as c:
        yield c


@pytest.mark.parametrize("path,_flask_payload", _WHOAMI_CACHED_ROUTES)
def test_whoami_connect_error_maps_to_bad_gateway_not_500(client, monkeypatch, path, _flask_payload):
    """A backend connect/timeout during whoami must not surface as a bare 500."""
    def _boom(api_key):
        raise httpx.ConnectError("backend unreachable")

    monkeypatch.setattr(enclave_app, "_whoami_cached", _boom)

    resp = client.get(path, headers={"X-API-Key": "testkey"})

    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code in (502, 503), resp.get_data(as_text=True)


@pytest.mark.parametrize("path,flask_payload", _WHOAMI_CACHED_ROUTES)
def test_key_derivation_failure_maps_to_503_not_500(client, monkeypatch, path, flask_payload):
    """A dstack/key-derivation failure must not surface as a bare 500."""
    monkeypatch.setattr(enclave_app, "_whoami_cached", lambda api_key: {"user_id": "usr_test"})
    monkeypatch.setattr(enclave_app, "_flask_get", lambda *a, **k: flask_payload)

    def _boom():
        raise RuntimeError("dstack socket unavailable")

    monkeypatch.setattr(enclave_app, "_get_or_derive_content_sk", _boom)

    resp = client.get(path, headers={"X-API-Key": "testkey"})

    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code == 503, resp.get_data(as_text=True)


def test_envelope_decrypt_key_derivation_failure_maps_to_503_not_500(client, monkeypatch):
    """The live-resolver route also guards its unguarded content_sk derivation."""
    monkeypatch.setattr(enclave_app, "_flask_get", lambda *a, **k: {"user_id": "usr_test"})

    def _boom():
        raise RuntimeError("dstack socket unavailable")

    monkeypatch.setattr(enclave_app, "_get_or_derive_content_sk", _boom)

    resp = client.post(
        "/v1/envelope/decrypt",
        headers={"X-API-Key": "testkey"},
        json={"envelope": {"id": "x", "v": 1}},
    )

    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code == 503, resp.get_data(as_text=True)

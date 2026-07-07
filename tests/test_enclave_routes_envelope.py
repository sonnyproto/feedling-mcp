from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _authed(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_missing_credentials_underscore_spelling(client):
    r = client.post("/v1/envelope/decrypt", json={"envelope": {}})
    assert r.status_code == 401
    assert r.get_json() == {"error": "missing_api_key"}  # 下划线拼法，勿改


def test_envelope_required(client, _authed):
    r = client.post("/v1/envelope/decrypt", json={},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "envelope required"}


def test_non_dict_body_normalized_to_400(client, _authed):
    r = client.post("/v1/envelope/decrypt", json=[1, 2, 3],
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "envelope required"}


def test_text_plain_json_body_rejected(client, _authed):
    """content-type 非 JSON(text/plain)的 JSON body → 当空负载 → 400，复刻旧 Flask
    get_json(silent=True) 的 content-type 门槛（P3）。Starlette 的 request.json() 本会
    忽略 content-type 照解，导致接受旧 Flask 会拒的输入。真实调用方都用
    httpx json=（application/json），不受影响。"""
    import json as _json
    body = _json.dumps({"envelope": {"id": "i", "v": 1}})
    r = client.post("/v1/envelope/decrypt", data=body, content_type="text/plain",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "envelope required"}


def test_application_json_body_still_works(client, _authed, monkeypatch):
    """对照：正确的 application/json body 仍正常处理（content-type 门槛不误伤真实调用方）。"""
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"ok")
    r = client.post("/v1/envelope/decrypt", json={"envelope": {"id": "i", "v": 1}},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert base64.b64decode(r.get_json()["plaintext_b64"]) == b"ok"


def test_decrypt_failure_maps_403(client, _authed, monkeypatch):
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("owner mismatch: x")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.post("/v1/envelope/decrypt",
                    json={"envelope": {"id": "i", "v": 1}},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 403
    assert r.get_json()["error"].startswith("decrypt_failed: owner mismatch")


def test_success_returns_plaintext_b64(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"secret!")
    r = client.post("/v1/envelope/decrypt",
                    json={"envelope": {"id": "itm", "v": 2}},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert base64.b64decode(body["plaintext_b64"]) == b"secret!"
    assert body == {"owner_user_id": "usr_a", "id": "itm", "v": 2,
                    "plaintext_b64": body["plaintext_b64"]}


def test_backend_401_maps_unauthorized(client, monkeypatch):
    req = httpx.Request("GET", "http://b/v1/users/whoami")
    err = httpx.HTTPStatusError("e", request=req,
                                response=httpx.Response(401, request=req))
    async def fake_backend_get(path, headers, params=None):
        raise err
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    r = client.post("/v1/envelope/decrypt", json={"envelope": {}},
                    headers={"X-API-Key": "bad"})
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}


def test_no_cache_every_call_resolves_live(client, monkeypatch):
    calls = []
    async def fake_backend_get(path, headers, params=None):
        calls.append(path)
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    from enclave import envelope as e2
    monkeypatch.setattr(e2, "decrypt_envelope", lambda e, u, s: b"x")
    for _ in range(3):
        client.post("/v1/envelope/decrypt", json={"envelope": {"id": "i"}},
                    headers={"X-API-Key": "k"})
    assert len(calls) == 3  # 敏感 unwrap 路由绝不走缓存（spec §2/§4）

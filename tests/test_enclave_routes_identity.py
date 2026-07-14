# tests/test_enclave_routes_identity.py
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

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


def _wire(monkeypatch, identity):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == "/v1/identity/get"
        return {"identity": identity}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_identity_get_head_supported(client, monkeypatch):
    # Flask 自动挂 HEAD 的 parity（同 chat/history），405 即回归。
    _wire(monkeypatch, None)
    r = client.open("/v1/identity/get", method="HEAD",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == b""


def test_identity_none_passthrough(client, monkeypatch):
    _wire(monkeypatch, None)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    assert r.get_json() == {"identity": None, "user_id": "usr_a"}


def test_identity_decrypt_with_live_days_anchor(client, monkeypatch):
    anchor = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    _wire(monkeypatch, {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                        "owner_user_id": "usr_a",
                        "relationship_started_at": anchor,
                        "created_at": "c", "updated_at": "u"})
    inner = json.dumps({"agent_name": "枫", "self_introduction": "hi",
                        "dimensions": [], "days_with_user": 999}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()["identity"]
    assert body["agent_name"] == "枫"
    assert body["days_with_user"] == 10  # 服务端锚点覆盖信封内旧值
    assert body["decrypt_status"] == "ok"


def test_identity_forwards_persona_voice_fields_when_present(client, monkeypatch):
    anchor = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    _wire(monkeypatch, {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                        "owner_user_id": "usr_a",
                        "relationship_started_at": anchor,
                        "created_at": "c", "updated_at": "u"})
    inner = json.dumps({
        "agent_name": "枫", "self_introduction": "hi",
        "dimensions": [], "days_with_user": 999,
        "tone_style": "warm and a little teasing",
        "agent_role": "creative co-pilot",
        "do_not_say": ["never mention pricing"],
        "boundaries": ["no medical advice"],
    }).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()["identity"]
    assert body["tone_style"] == "warm and a little teasing"
    assert body["agent_role"] == "creative co-pilot"
    assert body["do_not_say"] == ["never mention pricing"]
    assert body["boundaries"] == ["no medical advice"]


def test_identity_omits_persona_voice_fields_when_absent(client, monkeypatch):
    # Older cards without these fields must not get empty keys injected —
    # response shape stays additive (guards the resident consumer's
    # "existing card" merge from treating absence as an explicit reset).
    anchor = (dt.date.today() - dt.timedelta(days=10)).isoformat()
    _wire(monkeypatch, {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                        "owner_user_id": "usr_a",
                        "relationship_started_at": anchor,
                        "created_at": "c", "updated_at": "u"})
    inner = json.dumps({"agent_name": "枫", "self_introduction": "hi",
                        "dimensions": [], "days_with_user": 999}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()["identity"]
    for key in ("tone_style", "agent_role", "do_not_say", "boundaries"):
        assert key not in body


def test_identity_local_only(client, monkeypatch):
    _wire(monkeypatch, {"v": 1, "visibility": "local_only",
                        "created_at": "c", "updated_at": "u"})
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()["identity"]
    assert body["decrypt_status"] == "local_only_agent_cannot_read"


def test_identity_decrypt_error_shape(client, monkeypatch):
    _wire(monkeypatch, {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                        "owner_user_id": "usr_a", "created_at": "c",
                        "updated_at": "u"})
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("bad tag")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.get("/v1/identity/get", headers={"X-API-Key": "k"})
    body = r.get_json()
    assert body["identity"]["decrypt_status"] == "error: bad tag"
    assert body["decrypt_errors"] == [{"reason": "bad tag"}]

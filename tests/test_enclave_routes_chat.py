from __future__ import annotations

import base64
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


def _wire(monkeypatch, messages, moments=None):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": messages, "total": len(messages)}
        if path == "/v1/memory/list":
            return {"moments": moments or [], "total": 0}
        raise AssertionError(path)
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def test_history_decrypts_text_messages(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "m1", "role": "user", "ts": 1.0, "v": 1, "source": "ios",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: b"hello")
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    m = body["messages"][0]
    assert m["content"] == "hello"
    assert m["decrypt_status"] == "ok"
    assert body["decrypt_errors"] == []
    assert body["user_id"] == "usr_a"
    assert "context_memories" in body


def test_local_only_placeholder(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "m1", "role": "user", "ts": 1.0, "v": 1,
         "visibility": "local_only", "content_type": "text"},
    ])
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    m = r.get_json()["messages"][0]
    assert m["content"] is None
    assert m["decrypt_status"] == "local_only_agent_cannot_read"


def test_per_item_decrypt_error_not_500(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "bad", "role": "user", "ts": 1.0, "v": 1,
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a"},
    ])
    def boom(env, uid, sk):
        raise envmod.DecryptFailure("AEAD verify: nope")
    monkeypatch.setattr(envmod, "decrypt_envelope", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["messages"][0]["decrypt_status"].startswith("error: AEAD")
    assert body["decrypt_errors"][0]["id"] == "bad"


def test_image_message_with_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "img1", "role": "user", "ts": 1.0, "v": 1, "content_type": "image",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "image_mime": "image/png",
         "caption_body_ct": "y", "caption_nonce": "y", "caption_K_enclave": "y"},
    ])
    jpeg = b"\x89PNG fake"
    def fake_decrypt(env, uid, sk):
        return b"what is this?" if env.get("body_ct") == "y" else jpeg
    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    m = r.get_json()["messages"][0]
    assert base64.b64decode(m["image_b64"]) == jpeg
    assert m["image_mime"] == "image/png"
    assert m["content"] == "what is this?"


def test_context_memories_best_effort_on_failure(client, monkeypatch):
    _wire(monkeypatch, [])
    from enclave import readside
    def boom(*a, **kw):
        raise RuntimeError("selector exploded")
    monkeypatch.setattr(readside, "moments_to_cards", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200  # context_memories 失败绝不 500
    assert r.get_json()["context_memories"] == []

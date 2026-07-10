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


def test_memory_list_fetch_overlaps_history_decrypt(client, monkeypatch):
    """/v1/memory/list 拉取不依赖 history 解密结果，async 化后应与解密并行
    （旧同步 Flask 只能串行；串行让每个请求多付一次 backend RTT）。
    测法：解密故意放慢，断言 memory/list 的回环在解密结束前就已发出。"""
    import threading
    import time as _time

    order = []

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [
                {"id": "m1", "role": "user", "ts": 1.0, "v": 1, "source": "ios",
                 "K_enclave": "x", "body_ct": "x", "nonce": "x",
                 "owner_user_id": "usr_a"},
            ], "total": 1}
        if path == "/v1/memory/list":
            order.append("memlist_fetch")
            return {"moments": [], "total": 0}
        raise AssertionError(path)
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    def slow_decrypt(e, u, s):
        _time.sleep(0.2)  # 在 to_thread 里跑，不堵事件循环
        order.append("decrypt_end")
        return b"hello"
    monkeypatch.setattr(envmod, "decrypt_envelope", slow_decrypt)

    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.get_json()["messages"][0]["content"] == "hello"
    assert "memlist_fetch" in order and "decrypt_end" in order
    assert order.index("memlist_fetch") < order.index("decrypt_end"), \
        f"memory/list 没有与解密并行: {order}"


def test_history_head_supported(client, monkeypatch):
    # Flask 给每个 GET 路由自动挂 HEAD；FastAPI 的 APIRoute 不会。HEAD 必须
    # 返回与 GET 相同的状态/头、空 body（HeadBodyStripMiddleware 负责剥体），
    # 而不是 405。
    _wire(monkeypatch, [])
    r = client.open("/v1/chat/history", method="HEAD",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == b""


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


def test_file_message_with_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "f1", "role": "user", "ts": 1.0, "v": 1, "content_type": "file",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "file_mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "file_name": "报告.docx",
         "caption_body_ct": "y", "caption_nonce": "y", "caption_K_enclave": "y"},
    ])
    raw = b"raw doc bytes"
    def fake_decrypt(env, uid, sk):
        return b"summarize this?" if env.get("body_ct") == "y" else raw
    monkeypatch.setattr(envmod, "decrypt_envelope", fake_decrypt)
    m = client.get("/v1/chat/history", headers={"X-API-Key": "k"}).get_json()["messages"][0]
    import base64 as _b64
    assert _b64.b64decode(m["file_b64"]) == raw
    assert m["file_mime"].endswith("wordprocessingml.document")
    assert m["file_name"] == "报告.docx"
    assert m["content"] == "summarize this?"


def test_file_message_without_caption(client, monkeypatch):
    _wire(monkeypatch, [
        {"id": "f2", "role": "user", "ts": 1.0, "v": 1, "content_type": "file",
         "K_enclave": "x", "body_ct": "x", "nonce": "x", "owner_user_id": "usr_a",
         "file_mime": "text/markdown", "file_name": "notes.md"},
    ])
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e,u,s: b"# notes\n")
    m = client.get("/v1/chat/history", headers={"X-API-Key": "k"}).get_json()["messages"][0]
    import base64 as _b64
    assert _b64.b64decode(m["file_b64"]) == b"# notes\n"
    assert m["content"] == ""


def test_context_memories_best_effort_on_failure(client, monkeypatch):
    _wire(monkeypatch, [])
    from enclave import readside
    def boom(*a, **kw):
        raise RuntimeError("selector exploded")
    monkeypatch.setattr(readside, "moments_to_cards", boom)
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200  # context_memories 失败绝不 500
    assert r.get_json()["context_memories"] == []

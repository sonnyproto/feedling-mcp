# tests/test_enclave_cpu_offload.py
"""单事件循环下的 CPU 重活必须离开事件循环（迁移审查 CONFIRMED 项）：

解密已经在 to_thread 里，但大响应的 json.dumps（图片聊天史 / frames decrypt
的 ~470KB image_b64）和 gzip 中间件的 gzip.compress 仍内联在唯一的事件循环上
——几个图片重请求并发时 /healthz 也排队，网关超时 502。旧 Flask 模型
（32 gthreads 各自阻塞）没有这个队头阻塞。

测法：spy 记录 render/compress 实际运行的线程，断言不是事件循环所在线程。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import _json as json_offload  # noqa: E402
from enclave.routes import build_app  # noqa: E402

FRAME_ID = "ab" * 8
JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 4


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _wired(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        if path == "/v1/memory/list":
            return {"moments": [], "total": 0}
        return {"v": 1, "K_enclave": "x", "body_ct": "x", "nonce": "x",
                "owner_user_id": "usr_a", "ts": 1.0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    inner = {"image": base64.b64encode(JPEG).decode(), "image_mime": "image/jpeg",
             "ocr_text": "text on screen", "app": "Safari", "w": 100, "h": 200}
    monkeypatch.setattr(envmod, "decrypt_envelope",
                        lambda e, u, s: json.dumps(inner).encode())


@pytest.fixture()
def _render_spy(monkeypatch):
    seen = {}
    real = json_offload._render

    def spy(payload):
        seen["thread"] = threading.current_thread()
        return real(payload)

    monkeypatch.setattr(json_offload, "_render", spy)
    return seen


def test_json_response_offthread_renders_in_worker_thread(_render_spy):
    payload = {"user_id": "u", "blob": "x" * 1000, "nested": [1, 2, {"a": None}]}

    async def main():
        return await json_offload.json_response_offthread(payload)

    resp = asyncio.run(main())
    assert _render_spy["thread"] is not threading.current_thread()
    # 字节与 content-type 同 Starlette JSONResponse 完全一致（parity）
    ref = JSONResponse(payload)
    assert resp.body == ref.body
    assert resp.headers["content-type"] == ref.headers["content-type"]


def test_frame_decrypt_renders_json_off_event_loop(client, _wired, _render_spy):
    r = client.get(f"/v1/screen/frames/{FRAME_ID}/decrypt",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert base64.b64decode(r.get_json()["image_b64"]) == JPEG
    assert _render_spy.get("thread") is not None, "路由没有走离线程渲染"
    assert _render_spy["thread"] is not threading.current_thread()


def test_chat_history_renders_json_off_event_loop(client, _wired, _render_spy):
    r = client.get("/v1/chat/history", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.get_json()["messages"] == []
    assert _render_spy.get("thread") is not None, "路由没有走离线程渲染"
    assert _render_spy["thread"] is not threading.current_thread()


def test_gzip_compress_runs_off_event_loop(client, monkeypatch):
    import gzip as gzip_mod
    seen = {}
    real = gzip_mod.compress

    def spy(data, *a, **kw):
        seen["thread"] = threading.current_thread()
        return real(data, *a, **kw)

    monkeypatch.setattr(gzip_mod, "compress", spy)

    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 1.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "ab" * 8000,  # 16KB，远超 500B 阈值
        "event_log_json": "[]", "measurements": {}, "compose_hash": "h",
        "app_id": "a", "instance_id": "i",
    })
    r = client.get("/attestation", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert seen.get("thread") is not None
    assert seen["thread"] is not threading.current_thread()

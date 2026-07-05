"""Enclave server-side perf knobs (ASGI era).

The Flask-era pooled-httpx-client and gunicorn-worker-count knobs this file
used to cover are superseded:
  - the pooled enclave->backend client is now `enclave.backend_client`'s
    async singleton (see test_enclave_backend_client.py's
    test_aclose_resets_singleton / test_backend_get_roundtrip);
  - the env-driven worker count is `enclave.config.enclave_worker_count()`
    (see test_enclave_config.py's test_enclave_worker_count).

What remains genuinely enclave-server-shaped is spec §4's concurrency
invariant: decrypt batches must run in a thread (`anyio.to_thread`), not
inline on the event loop, so a slow decrypt batch for one caller does not
stall unrelated requests (e.g. /healthz) on the same worker.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


def test_healthz_responsive_during_slow_decrypt(monkeypatch):
    """spec §7：大批量解密进行中 /healthz 仍及时响应（解密在 to_thread，
    事件循环不被阻塞）。"""
    import asyncio, time
    import httpx
    from enclave import auth, backend_client, keys, state
    from enclave.routes import build_app, chat

    monkeypatch.setitem(state._state, "ready", True)
    monkeypatch.setitem(state._state, "error", None)
    auth.reset_cache()

    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        if path == "/v1/chat/history":
            return {"messages": [], "total": 0}
        return {"moments": [], "total": 0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)

    def slow_decrypt(messages, uid, sk):
        time.sleep(1.0)  # 模拟重解密批（在 to_thread 里跑才不会卡 loop）
        return [], []
    monkeypatch.setattr(chat, "_decrypt_history_items", slow_decrypt)

    app = build_app()

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            slow = asyncio.create_task(
                c.get("/v1/chat/history", headers={"X-API-Key": "k"}))
            await asyncio.sleep(0.1)  # 确保慢请求已进入解密阶段
            t0 = time.monotonic()
            h = await c.get("/healthz")
            dt = time.monotonic() - t0
            r = await slow
            return h.status_code, dt, r.status_code

    h_status, dt, slow_status = asyncio.run(main())
    assert h_status == 200
    assert slow_status == 200
    assert dt < 0.5, f"/healthz took {dt:.2f}s while decrypt batch was running"

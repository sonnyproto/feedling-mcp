# tests/test_enclave_auth_async.py
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from core import runtime_token as rt_token  # noqa: E402
from enclave import auth, backend_client, config  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    auth.reset_cache()
    yield
    auth.reset_cache()


def _patch_backend(monkeypatch, calls, result=None, delay=0.0, exc=None):
    async def fake_backend_get(path, headers, params=None):
        calls.append({"path": path, "headers": dict(headers or {})})
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return result if result is not None else {"user_id": "usr_1"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)


def test_singleflight_collapses_concurrent_misses(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls, delay=0.05)
    ctx = auth.AuthContext(api_key="k1", runtime_token="")

    async def main():
        return await asyncio.gather(*[auth.whoami_cached(ctx) for _ in range(10)])

    results = asyncio.run(main())
    assert all(r == {"user_id": "usr_1"} for r in results)
    assert len(calls) == 1  # 10 并发冷 miss 收敛为 1 次回环


def test_singleflight_no_cross_loop_future_error(monkeypatch):
    """两个并发请求命中同一冷凭证、但运行在不同事件循环上（多 loop 线程化嵌入，
    或两个各自 asyncio.run 的线程在 in-flight 窗口内重叠）时，inflight Future 必须
    按 loop 隔离——否则第二个调用会去 await 第一个 loop 建的 Future，抛
    `RuntimeError: got Future attached to a different loop`，而不是正常返回鉴权。
    回归 Codex P2（旧 threading.Lock singleflight 无此问题，ASGI 化后引入）。"""
    import threading
    import time as _time

    calls = []
    _patch_backend(monkeypatch, calls, delay=0.3)  # 制造 in-flight 重叠窗口
    ctx = auth.AuthContext(api_key="cold", runtime_token="")

    results: dict = {}

    def worker(tag):
        try:
            results[tag] = asyncio.run(auth.whoami_cached(ctx))
        except BaseException as e:  # noqa: BLE001 — 捕获 RuntimeError 以便断言
            results[tag] = e

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    _time.sleep(0.05)  # 确保 B 在 A 的 in-flight 窗口内、且在不同的 loop 上启动
    t2.start()
    t1.join()
    t2.join()

    for tag in ("A", "B"):
        assert results[tag] == {"user_id": "usr_1"}, f"{tag} 失败: {results[tag]!r}"


def test_cache_hit_and_ttl_expiry(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls)
    ctx = auth.AuthContext(api_key="k1", runtime_token="")
    asyncio.run(auth.whoami_cached(ctx))
    asyncio.run(auth.whoami_cached(ctx))
    assert len(calls) == 1
    monkeypatch.setattr(auth, "WHOAMI_CACHE_TTL", 0.0)
    asyncio.run(auth.whoami_cached(ctx))
    assert len(calls) == 2


def test_local_runtime_token_fast_path(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls)
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", b"s3cret")
    tok = rt_token.mint(b"s3cret", user_id="usr_9",
                        runtime_instance_id="ri_1", scope=["read"])
    ctx = auth.AuthContext(api_key="", runtime_token=tok)
    assert asyncio.run(auth.whoami_cached(ctx)) == {"user_id": "usr_9"}
    assert asyncio.run(auth.whoami_live(ctx)) == {"user_id": "usr_9"}
    assert calls == []  # 全程零回环


def test_bad_local_token_falls_back_to_backend(monkeypatch):
    calls = []
    _patch_backend(monkeypatch, calls)
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", b"s3cret")
    ctx = auth.AuthContext(api_key="", runtime_token="not-a-valid-token")
    assert asyncio.run(auth.whoami_cached(ctx)) == {"user_id": "usr_1"}
    assert len(calls) == 1
    assert calls[0]["headers"] == {"X-Feedling-Runtime-Token": "not-a-valid-token"}


def test_error_flight_not_cached(monkeypatch):
    calls = []
    req = httpx.Request("GET", "http://b/v1/users/whoami")
    err = httpx.HTTPStatusError("e", request=req,
                                 response=httpx.Response(500, request=req))
    _patch_backend(monkeypatch, calls, exc=err)
    ctx = auth.AuthContext(api_key="k1", runtime_token="")
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(auth.whoami_cached(ctx))
    assert len(calls) == 2  # 失败不落缓存，下次重试


def test_resolve_read_caller_error_strings(monkeypatch):
    from enclave import state
    monkeypatch.setitem(state._state, "ready", True)
    ctx = auth.AuthContext(api_key="", runtime_token="")
    user_id, error = asyncio.run(auth.resolve_read_caller(ctx))
    assert user_id is None
    assert error == ({"error": "missing api_key"}, 401)  # 空格拼法，勿改

    calls = []
    _patch_backend(monkeypatch, calls, result={"user_id": ""})
    ctx = auth.AuthContext(api_key="k", runtime_token="")
    user_id, error = asyncio.run(auth.resolve_read_caller(ctx))
    assert error == ({"error": "cannot resolve user_id"}, 401)

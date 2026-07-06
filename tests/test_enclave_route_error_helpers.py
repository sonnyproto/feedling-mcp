# tests/test_enclave_route_error_helpers.py
"""路由层公共错误映射 helper（enclave/routes/_errors.py）的契约钉子。

迁移把同一个 httpx→(401 unauthorized / 502 backend_error) 映射块复制了 5 份、
`get_content_sk → 503 key_derivation_unavailable` 复制了 10 份；本 helper 把
它们收敛为一处。错误串与状态码必须逐字保持旧路由行为（unauthorized 空格拼法
体系、backend_error 前缀、404 可选映射——frames 的 "frame not found"）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import httpx  # noqa: E402
import json  # noqa: E402

from enclave import keys  # noqa: E402
from enclave.routes import _errors  # noqa: E402


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://b/x")
    return httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(code, request=req))


def _run_call(exc=None, result=None, **kw):
    async def op():
        if exc is not None:
            raise exc
        return result

    return asyncio.run(_errors.backend_call_or_error(op(), **kw))


def _body(resp) -> dict:
    return json.loads(bytes(resp.body))


def test_success_passthrough():
    value, err = _run_call(result={"ok": 1})
    assert value == {"ok": 1}
    assert err is None


def test_401_maps_to_unauthorized():
    value, err = _run_call(exc=_status_error(401))
    assert value is None
    assert err.status_code == 401
    assert _body(err) == {"error": "unauthorized"}


def test_other_status_maps_to_backend_error_502():
    value, err = _run_call(exc=_status_error(500))
    assert err.status_code == 502
    assert _body(err)["error"].startswith("backend_error: ")


def test_network_error_maps_to_backend_error_502():
    value, err = _run_call(
        exc=httpx.ConnectError("boom", request=httpx.Request("GET", "http://b/x")))
    assert err.status_code == 502
    assert _body(err)["error"].startswith("backend_error: ")


def test_404_unmapped_falls_into_backend_error():
    value, err = _run_call(exc=_status_error(404))
    assert err.status_code == 502  # 默认不特判 404（chat/memory/identity 行为）


def test_404_mapped_when_requested():
    value, err = _run_call(exc=_status_error(404),
                           not_found_error="frame not found")
    assert err.status_code == 404
    assert _body(err) == {"error": "frame not found"}


def test_content_sk_or_503(monkeypatch):
    async def ok():
        return "SK"
    monkeypatch.setattr(keys, "get_content_sk", ok)
    sk, err = asyncio.run(_errors.content_sk_or_503())
    assert sk == "SK" and err is None

    async def boom():
        raise RuntimeError("dstack socket hiccup")
    monkeypatch.setattr(keys, "get_content_sk", boom)
    sk, err = asyncio.run(_errors.content_sk_or_503())
    assert sk is None
    assert err.status_code == 503
    assert _body(err)["error"].startswith("key_derivation_unavailable: ")

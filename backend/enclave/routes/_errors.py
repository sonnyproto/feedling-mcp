"""路由层共享错误映射（收敛迁移时复制的 15 个块）。

httpx → 错误响应的映射逐字沿用旧路由：401 → {"error": "unauthorized"}（whoami
可能缓存过、key 其后被吊销，这里补成 401 而不是笼统 502）；404 仅在调用方给了
``not_found_error`` 时特判（frames 的 "frame not found"）；其余 HTTPStatusError
与网络层 HTTPError 一律 502 backend_error。get_content_sk 是唯一的运行时
dstack 回环——socket 抖动属瞬时基建故障，统一回可重试的 503。

注意 auth.resolve_read_caller 的映射（backend_unreachable 拼法）是历史并存的
另一套，不并入这里（spec §2 禁止统一）。"""

from __future__ import annotations

from typing import Any, Awaitable

import httpx
from starlette.responses import JSONResponse

from enclave import keys


async def backend_call_or_error(
    call: Awaitable[Any], not_found_error: str | None = None,
) -> tuple[Any, JSONResponse | None]:
    """await 一个 backend 回环，httpx 异常映射为错误响应。
    返回 (result, None) 或 (None, JSONResponse)。"""
    try:
        return await call, None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return None, JSONResponse({"error": "unauthorized"}, status_code=401)
        if not_found_error is not None and e.response.status_code == 404:
            return None, JSONResponse({"error": not_found_error}, status_code=404)
        return None, JSONResponse({"error": f"backend_error: {e}"}, status_code=502)
    except httpx.HTTPError as e:
        return None, JSONResponse({"error": f"backend_error: {e}"}, status_code=502)


async def content_sk_or_503() -> tuple[Any, JSONResponse | None]:
    """派生 content key，失败回可重试的 503。返回 (content_sk, None) 或
    (None, JSONResponse)。"""
    try:
        return await keys.get_content_sk(), None
    except Exception as e:
        return None, JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)

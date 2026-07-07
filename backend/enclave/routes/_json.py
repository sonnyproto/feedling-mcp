"""大 JSON 响应的离线程渲染。

解密在 to_thread（spec §4），但 Starlette ``JSONResponse.__init__`` 的
``json.dumps`` 是内联的——图片聊天史 / frames decrypt 的 ~470KB image_b64
每次序列化都占住唯一的事件循环几十 ms，并发几个就把 /healthz 也堵在后面
（旧 Flask 32 gthreads 各自阻塞没有这个队头阻塞）。大 payload 路由用
``json_response_offthread`` 把 dumps 挪进解密同一个线程池。

``_render`` 逐字复刻 ``JSONResponse.render``（compact separators +
ensure_ascii=False + allow_nan=False），响应字节与 JSONResponse 完全一致。"""

from __future__ import annotations

import json

import anyio.to_thread
from starlette.responses import Response


def _render(payload) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


async def json_response_offthread(payload) -> Response:
    body = await anyio.to_thread.run_sync(_render, payload)
    return Response(body, media_type="application/json")

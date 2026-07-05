"""共享请求体读取：复刻旧 Flask ``get_json(silent=True)`` 的语义。

Starlette 的 ``request.json()`` 不看 Content-Type——任何 body 都会 json.loads，所以
一个 ``Content-Type: text/plain`` 的 JSON body 也会被解析。旧 Flask 的
``get_json(silent=True)`` 只在 Content-Type 是 ``application/json`` 或 ``*+json`` 时才
解析，否则返回 None（路由据此按"空负载"走 400）。为保持行为对齐（不接受错
content-type 的 JSON），这里先判 content-type 再解析；非 dict / 解析失败 / 非 JSON
类型一律归一为 ``{}``（与各路由既有的"非对象 body → 空负载 → 400"一致）。
"""

from __future__ import annotations

from starlette.requests import Request


def _is_json_content_type(request: Request) -> bool:
    mime = (request.headers.get("content-type", "").split(";", 1)[0].strip().lower())
    return mime == "application/json" or mime.endswith("+json")


async def read_json_payload(request: Request) -> dict:
    """读请求体为 JSON dict。Content-Type 非 JSON、解析失败、或结果非 dict → ``{}``
    （复刻旧 Flask ``get_json(silent=True)`` 的 content-type 门槛 + 各路由的非对象归一）。"""
    if not _is_json_content_type(request):
        return {}
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}

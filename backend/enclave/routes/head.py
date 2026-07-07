"""HEAD 请求响应体剥离中间件。

Starlette/FastAPI 的 Response 不看请求方法——对 HEAD 也会把完整 body 写进 ASGI
`http.response.body`。生产的 uvicorn 会在协议层把 HEAD 的 body 剥掉
（h11_impl: `data = b"" if scope["method"] == "HEAD" else body`），所以线上不泄露、
不占带宽；但那把"HEAD 不带 body"的正确性押在了 server 的行为上。本中间件在 app 层
显式剥掉 HEAD 的 body（保留状态码与全部响应头，含 Content-Length / ETag /
Content-Encoding），让 enclave 无论被哪种 ASGI server 承载都符合 HEAD 语义——与旧
Flask/Werkzeug 的行为一致。

装在最外层：GET 会产生的头（如 gzip 之后的 Content-Length）原样保留，仅 body 清空。
"""

from __future__ import annotations


class HeadBodyStripMiddleware:
    """纯 ASGI 中间件：HEAD 请求的响应头原样透传，body 清空为单个空终止分片。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "HEAD":
            await self.app(scope, receive, send)
            return

        body_sent = False

        async def _send(message):
            nonlocal body_sent
            mtype = message["type"]
            if mtype == "http.response.body":
                # 只发一个空的终止 body 分片，丢弃后续所有 body 分片（防多分片流式
                # 响应发出多个"终止"消息）。响应头在 http.response.start 里已原样透传，
                # 所以 Content-Length/ETag/Content-Encoding 与 GET 完全一致。
                if not body_sent:
                    body_sent = True
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            await send(message)

        await self.app(scope, receive, _send)

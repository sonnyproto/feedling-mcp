# backend/enclave/routes/gzip.py
"""Content-type-scoped gzip middleware — flask-compress equivalent (spec §6 fix).

Starlette's stock ``GZipMiddleware`` compresses ANY response >= ``minimum_size``
bytes, with no regard for content-type or status code. That broke two things
when it replaced flask-compress in the Flask->FastAPI migration:

  1. Binary responses (``image/jpeg`` from ``/v1/screen/frames/{id}/image``)
     got ``Content-Encoding: gzip`` slapped on them, which the old
     flask-compress never did (it only compressed a text/JSON MIME allowlist).
  2. 206 Partial Content responses got gzip-compressed too. ``Content-Range``
     is computed on the *decoded* byte offsets, so a gzip-compressed body
     under a byte-range header is malformed — this breaks the parallel-chunk
     image download the consumer relies on (dstack-gateway throttles each TCP
     connection to ~1 Mbps, so the client fetches images in parallel Range
     chunks; spec §6).

This middleware restores flask-compress's actual behaviour: only compress
status-200 responses whose Content-Type (ignoring any ``;charset=...``
suffix) is in an explicit text/JSON allowlist, only when the client sent
``Accept-Encoding: gzip``, and only when the buffered body is >= a minimum
size. Everything else (images, 206/304/other statuses, small bodies, clients
that didn't advertise gzip) passes through byte-for-byte untouched.

Deliberately NOT a Starlette ``GZipMiddleware`` subclass — this is a small,
independently-reasoned pure-ASGI middleware so the compress decision stays
easy to audit against the flask-compress semantics it replaces.
"""

from __future__ import annotations

import gzip

import anyio.to_thread

# Mirrors flask-compress's default COMPRESS_MIMETYPES allowlist. Notably
# excludes anything binary (image/*, application/octet-stream, ...).
_COMPRESSIBLE_CONTENT_TYPES = frozenset({
    "text/html",
    "text/css",
    "text/xml",
    "text/plain",
    "application/json",
    "application/javascript",
    "application/xml",
})


class ContentTypeGZipMiddleware:
    """Pure-ASGI gzip middleware scoped to status 200 + an allowlisted
    Content-Type, matching flask-compress instead of Starlette's blanket
    ``GZipMiddleware``.

    Buffers the response (start + body) before deciding — every compressible
    response in this app is a bounded, fully-built ``JSONResponse``/
    ``Response`` (not a stream), so buffering is safe and simple.
    """

    def __init__(self, app, minimum_size: int = 500):
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _accepts_gzip(scope):
            await self.app(scope, receive, send)
            return

        start_message: dict | None = None
        body_chunks: list[bytes] = []

        async def wrapped_send(message):
            nonlocal start_message
            if message["type"] == "http.response.start":
                # Hold it — we don't know the final headers (Content-Encoding/
                # Content-Length) until the body is fully buffered.
                start_message = message
                return
            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                await _flush_response(start_message, body_chunks, send, self.minimum_size)
                return
            await send(message)

        await self.app(scope, receive, wrapped_send)


def _accepts_gzip(scope) -> bool:
    for key, value in scope.get("headers", ()):
        if key == b"accept-encoding":
            return b"gzip" in value.lower()
    return False


def _content_type_without_params(headers) -> str:
    for key, value in headers:
        if key == b"content-type":
            return value.decode("latin-1").split(";", 1)[0].strip().lower()
    return ""


async def _flush_response(start_message, body_chunks, send, minimum_size: int):
    body = b"".join(body_chunks)
    status = start_message["status"]
    headers = list(start_message.get("headers", []))

    should_compress = (
        status == 200
        and _content_type_without_params(headers) in _COMPRESSIBLE_CONTENT_TYPES
        and len(body) >= minimum_size
    )

    if not should_compress:
        # Pass through byte-for-byte, headers untouched — this is what keeps
        # a 206 Range response (and any binary body) correct.
        await send(start_message)
        await send({"type": "http.response.body", "body": body, "more_body": False})
        return

    # 压缩离事件循环：/decrypt 的 ~470KB JSON 这类大 body 内联压缩会占住唯一
    # 的事件循环几十 ms，并发下把 /healthz 等小请求堵成队头阻塞（spec §4 同因）。
    compressed = await anyio.to_thread.run_sync(gzip.compress, body)
    new_headers = [
        (key, value) for key, value in headers
        if key not in (b"content-length", b"content-encoding", b"vary")
    ]
    new_headers.append((b"content-encoding", b"gzip"))
    new_headers.append((b"vary", b"accept-encoding"))
    new_headers.append((b"content-length", str(len(compressed)).encode("latin-1")))

    await send({"type": "http.response.start", "status": status, "headers": new_headers})
    await send({"type": "http.response.body", "body": compressed, "more_body": False})

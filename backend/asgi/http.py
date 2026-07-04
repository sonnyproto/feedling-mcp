"""ASGI request helpers with Flask-parity semantics.

``read_json_silent`` mirrors Flask ``request.get_json(silent=True)`` — including
its **content-type gate**, which the bare Starlette ``request.json()`` lacks:
Flask returns ``None`` when the Content-Type is not JSON *even if the body is
valid JSON*, so a ``text/plain`` body carrying JSON is ignored (`… or {}` → {}),
never acted on. Migrated write routes must preserve this so a legacy/odd client
can't trigger an action under ASGI that Flask would have dropped (plan §19.4
"legacy-client lax payloads").

Callers use ``payload = (await read_json_silent(request)) or {}`` to reproduce
``get_json(silent=True) or {}`` exactly (falsy → {}, truthy non-dict passes
through unchanged).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import Request
from starlette.requests import ClientDisconnect


def _is_json_content_type(content_type: str) -> bool:
    # Flask's request.is_json: mimetype == application/json or a +json suffix.
    mimetype = content_type.split(";", 1)[0].strip().lower()
    return mimetype == "application/json" or (
        mimetype.startswith("application/") and mimetype.endswith("+json")
    )


async def read_json_silent(request: Request) -> Optional[Any]:
    """Parsed JSON body, or None when the content-type isn't JSON, the body is
    empty, or parsing fails — matching Flask ``request.get_json(silent=True)``."""
    if not _is_json_content_type(request.headers.get("content-type", "")):
        return None
    try:
        body = await request.body()
    except ClientDisconnect:
        # Client dropped mid-upload (iOS background reports). Flask saw this as
        # a truncated body -> parse failure -> None; an uncaught raise here is a
        # 500 + traceback to a peer that's already gone.
        return None
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None

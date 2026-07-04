"""Native ASGI tracking-event ingestion (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/track/event`` route: it requires an authenticated user
(``Depends(require_auth)`` — same as the Flask ``auth.require_user()`` gate),
decodes the JSON body with the same tolerance as Flask's
``request.get_json(silent=True) or {}`` (a malformed/empty body degrades to an
empty dict, never a 400), and delegates to the framework-neutral
``tracking.tracking_core`` so the response body is identical to Flask's.

The sanitize + DB write is a blocking hop, so it runs through
``threadpool.run_db`` off the event loop (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from asgi import http as asgi_http
from tracking import tracking_core

router = APIRouter()


@router.post("/v1/track/event")
async def track_event(request: Request, auth: AuthResult = Depends(require_auth)):
    # Flask's ``request.get_json(silent=True) or {}`` incl. the content-type gate
    # (asgi.http.read_json_silent): non-JSON content-type -> {}; truthy non-dict
    # (e.g. a JSON array) passes through unchanged.
    body = (await asgi_http.read_json_silent(request)) or {}
    return await threadpool.run_db(tracking_core.track_event, auth.store, body_dict=body)


def register_asgi(app) -> None:
    app.include_router(router)

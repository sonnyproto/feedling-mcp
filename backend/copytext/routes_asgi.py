"""Native ASGI /v1/copytext routes (ASGI-migration plan §5.3).

GET is a public read (ETag / If-None-Match / 304); POST is admin-token gated
(``FEEDLING_ADMIN_TOKEN``) — the same admin auth the Flask route enforces via
``admin.data_track.require_admin``, replicated here as an ``HTTPException`` so the
registered exception handler renders the identical fixed 401/503 bodies
(``asgi.responses.ERROR_BODIES``). The payload/status shape is built by the
framework-neutral ``copytext.copytext_core`` (byte-for-byte the Flask output);
both DB-touching cores run through ``threadpool.run_db`` off the event loop.
"""

from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from asgi import threadpool
from copytext import copytext_core, service

router = APIRouter()


def _extract_admin_token(request: Request) -> str:
    # Mirror admin.data_track._extract_admin_token (header, bearer, then query).
    key = (request.headers.get("X-Admin-Token") or "").strip()
    if key:
        return key
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("admin_key") or "").strip()


def _require_admin(request: Request) -> None:
    # Mirror admin.data_track.require_admin: 503 when unconfigured, 401 on
    # missing/mismatched token. The exception handler maps these to the same
    # fixed bodies Flask's errorhandler(401/503) returns.
    configured = os.environ.get("FEEDLING_ADMIN_TOKEN", "").strip()
    if not configured:
        raise HTTPException(status_code=503)
    supplied = _extract_admin_token(request)
    if not supplied or not hmac.compare_digest(supplied, configured):
        raise HTTPException(status_code=401)


async def _read_json_silent(request: Request):
    """Mirror Flask ``request.get_json(silent=True)``: parse the JSON body, or
    return None when the content-type isn't JSON, the body is empty, or parsing
    fails — so ``(... or {})`` in the core matches the Flask route exactly."""
    ct = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not (ct == "application/json" or ct.endswith("+json")):
        return None
    raw = await request.body()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@router.get("/v1/copytext")
async def get_copytext(request: Request):
    result = await threadpool.run_db(
        copytext_core.copytext_get_payload,
        service.store,
        if_none_match=request.headers.get("If-None-Match", ""),
    )
    return JSONResponse(
        result["body"],
        status_code=result["status"],
        headers={"ETag": result["etag"]},
    )


@router.post("/v1/copytext")
async def post_copytext(request: Request):
    _require_admin(request)
    payload = (await _read_json_silent(request)) or {}
    result = await threadpool.run_db(copytext_core.copytext_post, service.store, body=payload)
    return JSONResponse(result["body"], status_code=result["status"])


def register_asgi(app) -> None:
    app.include_router(router)

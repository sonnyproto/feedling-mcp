# backend/enclave/routes/memory.py
"""memory 读侧三路由（旧 enclave_app L1302-1360 + L1601-1697）。
错误串空格拼法；解密批处理经 to_thread（spec §4）。"""

from __future__ import annotations

import json

import anyio.to_thread
import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, backend_client, envelope, keys, readside, state
from enclave.routes._body import read_json_payload

router = APIRouter()


@router.post("/v1/memory/index")
async def v1_memory_index(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)
    payload = await read_json_payload(request)
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return JSONResponse({"error": "moments must be a list"}, status_code=400)
    effective_limit = readside.memory_readside_effective_limit(payload.get("limit"))

    def _work():
        items, unavailable_ids = readside.decrypt_readside_items(
            moments[:effective_limit], user_id or "", content_sk,
            item_builder=readside.build_memory_index_item)
        items = readside.memory_index_filter_items(items, payload)
        if not bool(payload.get("include_sensitive", False)):
            items = [i for i in items if not i.get("is_sensitive")]
        return items, unavailable_ids

    items, unavailable_ids = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
    })


@router.post("/v1/memory/fetch")
async def v1_memory_fetch(request: Request):
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)
    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)
    payload = await read_json_payload(request)
    moments = payload.get("moments")
    if not isinstance(moments, list):
        return JSONResponse({"error": "moments must be a list"}, status_code=400)
    effective_limit = readside.memory_readside_effective_limit(payload.get("limit"))

    def _work():
        items, unavailable_ids = readside.decrypt_readside_items(
            moments[:effective_limit], user_id or "", content_sk,
            item_builder=readside.build_memory_fetch_item)
        blocked_sensitive_ids: list[str] = []
        if not bool(payload.get("include_sensitive", False)):
            allowed = []
            for item in items:
                if item.get("is_sensitive"):
                    blocked_sensitive_ids.append(str(item.get("id") or ""))
                else:
                    allowed.append(item)
            items = allowed
        return items, unavailable_ids, blocked_sensitive_ids

    items, unavailable_ids, blocked_sensitive_ids = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "items": items,
        "unavailable_ids": unavailable_ids,
        "blocked_sensitive_ids": [mid for mid in blocked_sensitive_ids if mid],
    })


@router.get("/v1/memory/list")
async def v1_memory_list(request: Request):
    """Decrypt-and-serve memory garden for the authenticated user.

    Query params:
      since (ISO string, optional): pass-through to /v1/memory/list
      limit (int, default 50, max 200)
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    limit = request.query_params.get("limit", "50")
    since = request.query_params.get("since", "")
    params = {"limit": limit}
    if since:
        params["since"] = since
    try:
        listing = await backend_client.backend_get(
            "/v1/memory/list", ctx.forward_headers, params=params)
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)

    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)

    def _work():
        decrypted = []
        errors = []
        for m in listing.get("moments", []):
            v = int(m.get("v", 0))
            base = {
                "id": m["id"],
                "occurred_at": m.get("occurred_at"),
                "created_at": m.get("created_at"),
                "source": m.get("source"),
                "v": v,
            }
            if m.get("visibility") == "local_only":
                base.update({
                    "title": None, "description": None, "type": None,
                    "visibility": "local_only",
                    "decrypt_status": "local_only_agent_cannot_read",
                })
                decrypted.append(base)
                continue
            try:
                plaintext = envelope.decrypt_envelope(m, user_id, content_sk)
                inner = json.loads(plaintext.decode("utf-8"))
                base.update({
                    "title": inner.get("title"),
                    "description": inner.get("description"),
                    "type": inner.get("type"),
                    "visibility": m.get("visibility", "shared"),
                    "decrypt_status": "ok",
                })
            except (envelope.DecryptFailure, json.JSONDecodeError) as e:
                reason = e.reason if isinstance(e, envelope.DecryptFailure) else f"json: {e}"
                errors.append({"id": m.get("id"), "reason": reason})
                base.update({
                    "title": None, "description": None, "type": None,
                    "decrypt_status": f"error: {reason}",
                })
            decrypted.append(base)
        return decrypted, errors

    decrypted, errors = await anyio.to_thread.run_sync(_work)
    return JSONResponse({
        "user_id": user_id,
        "moments": decrypted,
        "total": listing.get("total", len(decrypted)),
        "decrypt_errors": errors,
    })

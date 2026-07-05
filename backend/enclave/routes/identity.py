# backend/enclave/routes/identity.py
"""身份卡 decrypt-and-serve（旧 enclave_app L1700-1799，模式同 Task 10 memory）。
days_with_user 从服务端锚点实时计算，覆盖信封内旧值；单条解密经 to_thread。"""

from __future__ import annotations

import datetime as _dt
import json

import anyio.to_thread
import httpx
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, backend_client, envelope, keys

router = APIRouter()


def _parse_iso_calendar_date(value: str) -> _dt.date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        norm = raw.replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm + "T00:00:00"
        return _dt.datetime.fromisoformat(norm).date()
    except Exception:
        return None


@router.get("/v1/identity/get")
async def v1_identity_get(request: Request):
    """Decrypt-and-serve the identity card for the authenticated user.

    Returns the same shape as /v1/identity/get (agent_name, self_introduction,
    dimensions[]), assembled from decrypted ciphertext when stored as v1.
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    try:
        resp = await backend_client.backend_get(
            "/v1/identity/get", ctx.forward_headers)
    except httpx.HTTPStatusError as e:
        # whoami may have been cached, so a key revoked since then surfaces here;
        # keep it a 401, not a generic 502.
        if e.response.status_code == 401:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"backend_error: {e}"}, status_code=502)

    identity = resp.get("identity")
    if identity is None:
        return JSONResponse({"identity": None, "user_id": user_id})

    v = int(identity.get("v", 0))
    base = {
        "v": v,
        "created_at": identity.get("created_at"),
        "updated_at": identity.get("updated_at"),
    }
    if identity.get("visibility") == "local_only":
        base.update({
            "visibility": "local_only",
            "decrypt_status": "local_only_agent_cannot_read",
        })
        return JSONResponse({"identity": base, "user_id": user_id})

    try:
        content_sk = await keys.get_content_sk()
    except Exception as e:
        # The only runtime dstack round-trip. A socket hiccup deriving the
        # content key is a transient infra failure, not an enclave bug — return
        # a retryable 503 rather than a bare 500 the consumer can't interpret.
        return JSONResponse(
            {"error": f"key_derivation_unavailable: {e}"}, status_code=503)

    def _work():
        try:
            plaintext = envelope.decrypt_envelope(identity, user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))

            # days_with_user is computed live from the server-side anchor.
            # This makes the count auto-increment daily without the agent ever
            # writing it again (the old envelope-embedded value is ignored).
            # Legacy fallback: if no anchor on file, use the embedded value
            # so users that bootstrapped before this migration still see something.
            anchor = identity.get("relationship_started_at")
            if anchor:
                started = _parse_iso_calendar_date(anchor)
                live_days = (
                    max(0, (_dt.datetime.now().date() - started).days)
                    if started else inner.get("days_with_user", 0)
                )
            else:
                live_days = inner.get("days_with_user", 0)

            base.update({
                "agent_name": inner.get("agent_name"),
                "self_introduction": inner.get("self_introduction"),
                "dimensions": inner.get("dimensions", []),
                "days_with_user": live_days,
                "category": inner.get("category", ""),
                "signature": inner.get("signature", []),
                "visibility": identity.get("visibility", "shared"),
                "decrypt_status": "ok",
            })
            return {"identity": base, "user_id": user_id}
        except (envelope.DecryptFailure, json.JSONDecodeError) as e:
            reason = e.reason if isinstance(e, envelope.DecryptFailure) else f"json: {e}"
            base.update({"decrypt_status": f"error: {reason}"})
            return {"identity": base, "user_id": user_id,
                     "decrypt_errors": [{"reason": reason}]}

    result = await anyio.to_thread.run_sync(_work)
    return JSONResponse(result)

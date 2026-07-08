"""GET /v1/notices — 快照式通知中心读端点（spec Phase B / B2）。

require_auth（无 scope，与其它用户面端点一致）；include_resolved 为字符串
query，在此解析成 bool 再转发给 core。"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from notices import core as notices_core

router = APIRouter()


@router.get("/v1/notices")
async def list_notices(request: Request, auth: AuthResult = Depends(require_auth)):
    raw = str(request.query_params.get("include_resolved", "true")).lower()
    include_resolved = raw not in {"0", "false", "no"}
    body, status = await threadpool.run_db(
        notices_core.list_notices, auth.store, include_resolved=include_resolved)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)

"""Native ASGI bootstrap routes (ASGI-migration plan §9.4 / §5.3).

Payload built by the framework-neutral ``bootstrap.status_core`` so the body is
identical to the Flask route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from bootstrap import bootstrap_core
from bootstrap import status_core

router = APIRouter()


@router.get("/v1/bootstrap/status")
async def bootstrap_status(auth: AuthResult = Depends(require_auth)):
    return await threadpool.run_db(status_core.bootstrap_status_payload, auth.store)


@router.post("/v1/bootstrap")
async def bootstrap(auth: AuthResult = Depends(require_auth)):
    # Flask's ``auth.require_user()`` — same gate, no scope (mirrors the Flask
    # route). The Flask route ignores the request body, so the core takes only
    # the store; the blob write + registry read are blocking, hence run_db.
    return await threadpool.run_db(bootstrap_core.bootstrap_payload, auth.store)


def register_asgi(app) -> None:
    app.include_router(router)

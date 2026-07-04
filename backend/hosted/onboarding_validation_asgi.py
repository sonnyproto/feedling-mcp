"""Native ASGI onboarding-validation surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``GET /v1/onboarding/validate`` route: it requires an
authenticated user (``Depends(require_auth)`` — the ASGI equivalent of
``auth.require_user()``; no runtime-token scope, matching Flask) and delegates to
the framework-neutral ``hosted.onboarding_validation_core`` so the body is
byte-identical. The whole payload builder stays in ``hosted.onboarding_validation``
and is injected (``_onboarding_validation_payload``) so both frameworks compute the
same artifact-based validation state. It aggregates blocking store reads, so it runs
off the event loop through ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth
from hosted import onboarding_validation as onboarding_validation_flask
from hosted import onboarding_validation_core

router = APIRouter()


@router.get("/v1/onboarding/validate")
async def onboarding_validate(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(
        onboarding_validation_core.validate,
        auth.store,
        build_payload=onboarding_validation_flask._onboarding_validation_payload,
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)

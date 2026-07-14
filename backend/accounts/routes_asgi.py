"""Native ASGI accounts routes (ASGI-migration plan §9.4 / §5.3).

First real domain router on FastAPI. Establishes the ``register_asgi(app)``
pattern that every migrated package follows (mirrors app.py's ``register(app)``
for Flask). The payload is built by the framework-neutral ``accounts.whoami_core``
so it is byte-for-byte the same body the Flask route returns.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import accounts_core, whoami_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_api_key, require_auth

router = APIRouter()


@router.get("/v1/users/whoami")
async def whoami(auth: AuthResult = Depends(require_auth)):
    # whoami_core does a blocking enclave fetch (cached) — run off the loop.
    return await threadpool.run_db(whoami_core.whoami_payload, auth.store)


# --------------------------------------------------------------------------- #
# Access modes — all require an authenticated user (Flask: auth.require_user()).
# The core returns (body, status); every branch touches sync db.py, so it runs
# on the threadpool (plan §5.2), and non-200 statuses are rendered explicitly.
# --------------------------------------------------------------------------- #


@router.get("/v1/access/modes")
async def access_modes(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(accounts_core.access_modes_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/access/modes/switch")
async def access_modes_switch(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.access_modes_switch, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/access/link-token")
async def access_link_token_create(request: Request, auth: AuthResult = Depends(require_api_key)):
    # A runtime token must never be exchangeable for a new long-lived API key.
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.access_link_token_create, auth.store, payload)
    return JSONResponse(body, status_code=status)


# Claim is PUBLIC (Flask: no auth.require_user()) — the bearer proves possession
# by presenting the one-time link token in the body, not an api key / user auth.
@router.post("/v1/access/claim-token")
async def access_link_token_claim(request: Request):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.access_link_token_claim, payload)
    return JSONResponse(body, status_code=status)


# --------------------------------------------------------------------------- #
# Register + keypair recovery — PUBLIC / PRE-AUTH (Flask: no auth.require_user()).
# Register creates a new account (no user yet); recover is a challenge/response
# proof-of-possession that predates any api key. None of these use require_auth.
# --------------------------------------------------------------------------- #


@router.post("/v1/users/register")
async def users_register(request: Request):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.users_register, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/account/recover/challenge")
async def account_recover_challenge(request: Request):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.account_recover_challenge, payload)
    return JSONResponse(body, status_code=status)


@router.post("/v1/account/recover/verify")
async def account_recover_verify(request: Request):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.account_recover_verify, payload)
    return JSONResponse(body, status_code=status)


# --------------------------------------------------------------------------- #
# Preferences + onboarding route — require an authenticated user.
# --------------------------------------------------------------------------- #


@router.post("/v1/users/preferences")
async def users_set_preferences(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.users_set_preferences, auth.store, payload)
    return JSONResponse(body, status_code=status)


@router.get("/v1/onboarding/route")
async def onboarding_route_get(auth: AuthResult = Depends(require_auth)):
    body, status = await threadpool.run_db(accounts_core.onboarding_route_get, auth.store)
    return JSONResponse(body, status_code=status)


@router.post("/v1/onboarding/route")
async def onboarding_route_post(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(accounts_core.onboarding_route_post, auth.store, payload)
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)

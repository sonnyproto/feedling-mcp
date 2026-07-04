"""Native ASGI hosted chat send (ASGI-migration plan §11.1).

``POST /v1/model_api/chat/send`` — the hosted Model-API main path. The Flask
route parked a gunicorn thread inside ``agent_runtime_cutover.handle_send`` while
polling the store for the agent's reply (up to ~8s of ``time.sleep``); this async
route hands the ENTIRE route body — provider-config load, envelope build, chat
append, supervisor wedge guard, debug traces, AND that blocking wait — to the
bounded threadpool via ``run_db`` in a single hop, so the 202 contract, every
debug/action trace, the 400/409/413/503 branches, and the single (non-double)
append are byte-identical to Flask. The route body itself lives in the
framework-neutral ``hosted.chat_send_core``; only auth + credential extraction +
the response wrapper differ here.

No scope: the Flask route only calls ``auth.require_user()`` (no
``runtime_auth.authorize_scope``), so this uses plain ``require_auth``.

Credentials mirror Flask exactly: ``api_key`` = ``auth_core.extract_api_key(
headers, query)`` (the framework-neutral twin of ``auth._extract_api_key()``,
None on the runtime-token path); ``runtime_tok`` = the verified runtime token
forwarded only when no api_key is present, matching
``"" if api_key else (runtime_auth.extract_runtime_token() or "")``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth
from hosted import chat_send_core

router = APIRouter()


@router.post("/v1/model_api/chat/send")
async def model_api_chat_send(request: Request, auth: AuthResult = Depends(require_auth)):
    store = auth.store
    # Mirror Flask ``auth._extract_api_key()`` (re-read from headers/query, not the
    # resolved AuthResult) so a request carrying BOTH a runtime token and an
    # X-API-Key forwards the api_key exactly as Flask did.
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    runtime_tok = "" if api_key else (auth_core.extract_runtime_token(request.headers) or "")
    payload = (await asgi_http.read_json_silent(request)) or {}
    # The whole route body — including the blocking reply-wait inside handle_send —
    # runs on the bounded threadpool (plan §5.2). Never call it on the event loop.
    body, status = await threadpool.run_db(
        chat_send_core.model_api_chat_send_core,
        store,
        api_key=api_key,
        runtime_tok=runtime_tok,
        payload=payload,
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)

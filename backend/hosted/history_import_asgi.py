"""Native ASGI history-import surface (ASGI-migration plan §5.3 / §9).

Mirrors the Flask ``/v1/history_import/*`` routes: both require an authenticated
user (``Depends(require_auth)`` — the ASGI equivalent of ``auth.require_user()``;
neither route gates on a runtime-token scope, matching Flask) and delegate to the
framework-neutral ``hosted.history_import_core`` so the bodies are byte-identical.

Enqueue-not-inline (plan §5.7): ``/upload`` accepts the JSON payload and ENQUEUES
the distill/import via the routes-resident ``_start_history_import_job`` daemon
thread (injected so the seam is the SAME monkeypatchable one Flask uses); the heavy
``_run_history_import_job`` never runs on the event loop. Credential forwarding
mirrors Flask's ``auth._extract_api_key()`` via
``auth_core.extract_api_key(headers, query_params)`` so the caller's key reaches
the enclave-owned worker unchanged (no server-side decrypt here). All store / db
work is blocking, so it runs off the loop through ``threadpool.run_db`` (plan §5.2).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from accounts import auth_core
from accounts.auth_core import AuthResult
from asgi import http as asgi_http
from asgi import threadpool
from asgi.deps import require_auth
from hosted import history_import as history_import_flask
from hosted import history_import_core

router = APIRouter()


@router.post("/v1/history_import/upload")
async def history_import_upload(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    body, status = await threadpool.run_db(
        history_import_core.upload,
        auth.store,
        payload,
        api_key=api_key,
        # Inject the routes-resident helpers so the enqueue seam
        # (``_start_history_import_job`` — a daemon thread) is the SAME as Flask
        # and stays monkeypatchable via ``history_import._…``.
        payload_hash=history_import_flask._history_import_payload_hash,
        client_job_id_fn=history_import_flask._history_import_client_job_id,
        find_reusable=history_import_flask._history_import_find_reusable_job,
        save_job=history_import_flask._save_history_job,
        start_job=history_import_flask._start_history_import_job,
        phase_fields=history_import_flask._history_import_phase_fields,
    )
    return JSONResponse(body, status_code=status)


@router.get("/v1/history_import/status/{job_id}")
async def history_import_status(
    job_id: str, request: Request, auth: AuthResult = Depends(require_auth)
):
    body, status = await threadpool.run_db(
        history_import_core.status,
        auth.store,
        job_id,
        job_kind=history_import_flask._history_job_kind,
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)

"""Native ASGI onboarding-archive route (ASGI-migration).

Mirrors the Flask ``onboarding_archive`` blueprint:
  - ``POST /v1/onboarding/archive`` — user auth (``Depends(require_auth)``, same
    as Flask ``auth.require_user()``); a multipart upload streamed to R2 + a
    Postgres index row.

The payload is built by the framework-neutral ``onboarding_archive_core`` (the
same body the Flask route returns). It streams the upload to boto3 R2 + writes
sync ``db.py``, so it runs through ``threadpool.run_db`` off the event loop (plan
§5.2). The 25 MiB body cap is enforced from Content-Length up front (413) — the
ASGI counterpart of Flask's ``request.max_content_length`` — and the multipart
parser's per-part size cap is raised to match so a legitimate large upload still
parses. The 400/413/502/503 bodies are not in ``ERROR_BODIES``, so they are
returned as explicit ``JSONResponse`` (verbatim).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from accounts.auth_core import AuthResult
from asgi import threadpool
from asgi.deps import require_auth

from . import onboarding_archive_core

router = APIRouter()


def _content_length(request: Request) -> int | None:
    """Parse the Content-Length header to int|None (Flask ``request.content_length``)."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _form_str(value) -> str | None:
    """A multipart *text* field value (str) or None — never an UploadFile."""
    return value if isinstance(value, str) else None


@router.post("/v1/onboarding/archive")
async def archive_onboarding_file(request: Request, auth: AuthResult = Depends(require_auth)):
    # Reject oversized bodies from Content-Length *before* buffering the multipart
    # body — the ASGI parity for Flask's request.max_content_length (413).
    if onboarding_archive_core.is_oversized(_content_length(request)):
        return JSONResponse({"error": "payload_too_large"}, status_code=413)

    # Raise the per-part cap off Starlette's 1 MiB default so a valid upload up to
    # the 25 MiB ceiling parses (the Content-Length guard above already bounds it).
    form = await request.form(max_part_size=onboarding_archive_core._MAX_REQUEST_BYTES)
    upload = form.get("file")
    present = isinstance(upload, UploadFile)

    # Second cap on the ACTUAL spooled size: Flask enforces max_content_length
    # while reading, so a chunked / absent / lying Content-Length can't smuggle an
    # oversized body past the up-front check. Starlette tracks the real size as it
    # spools the part, so reject here before we ever stream it to R2 (413), matching
    # Flask's reject-oversized semantics regardless of the declared Content-Length.
    if present and onboarding_archive_core.is_oversized(upload.size):
        return JSONResponse({"error": "payload_too_large"}, status_code=413)

    filename = _form_str(form.get("filename")) or (upload.filename if present else None) or ""
    content_type = (
        _form_str(form.get("content_type"))
        or (upload.content_type if present else None)
        or "application/octet-stream"
    )
    client_job_id = (_form_str(form.get("client_job_id")) or "").strip() or None
    size_hint = upload.size if present else None
    fileobj = upload.file if present else None

    body, status = await threadpool.run_db(
        onboarding_archive_core.archive_onboarding_payload,
        auth.store,
        fileobj=fileobj,
        filename=filename,
        content_type=content_type,
        client_job_id=client_job_id,
        size_hint=size_hint,
    )
    return JSONResponse(body, status_code=status)


def register_asgi(app) -> None:
    app.include_router(router)

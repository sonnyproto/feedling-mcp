"""Framework-neutral onboarding-archive payload builder (ASGI-migration).

Lifts the body of ``POST /v1/onboarding/archive`` out of the Flask request layer
so the native ASGI router reuses byte-identical logic. There is no Flask/FastAPI
request object here: the caller resolves auth + parses the multipart request (the
file stream, form fields, Content-Length) and hands the store + already-resolved
params in.

Unlike the diagnostics upload, the onboarding upload is **streamed** straight to
R2 (boto3 ``upload_fileobj``) — never read fully into memory, since files can be
up to 25 MiB — so this takes a sync file-like ``fileobj`` (Flask
``upload.stream`` / Starlette ``UploadFile.file``) rather than raw bytes. It
touches sync boto3 R2 + sync ``db.py`` (blocking I/O), so ASGI callers MUST run
it through ``asgi.threadpool.run_db`` — never on the event loop (plan §5.0/§5.2).
Returns a ``(body_dict, status)`` tuple so both backends emit the identical JSON
body + status code.
"""

from __future__ import annotations

import re
import time
import uuid

import db

from . import storage

_STREAM = "onboarding_archive"
_MAX_REQUEST_BYTES = 25 * 1024 * 1024


def _safe_filename(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (raw or "").strip())[:128]
    return cleaned or "file"


def is_oversized(content_length: int | None) -> bool:
    """True when the request body exceeds the 25 MiB ceiling — the ASGI caller
    uses this to reject (413) from Content-Length *before* buffering the multipart
    body, mirroring Flask's ``request.max_content_length`` enforcement."""
    return content_length is not None and content_length > _MAX_REQUEST_BYTES


def archive_onboarding_payload(store, *, fileobj, filename, content_type,
                               client_job_id, size_hint):
    """Body of ``POST /v1/onboarding/archive``. ``fileobj`` is a sync file-like
    positioned at 0 (or None when there is no ``file`` part); ``filename`` /
    ``content_type`` / ``client_job_id`` are the caller-resolved form values (with
    the upload-attr + default fallbacks already applied); ``size_hint`` is the
    upload's declared byte size or None. Streams to R2 then appends the Postgres
    index row. Blocking: boto3 R2 put + db write."""
    uid = store.user_id

    if fileobj is None:
        return {"error": "missing_file"}, 400

    safe = _safe_filename(filename)

    # Reject an empty upload by peeking one byte, then rewind for the stream upload.
    head = fileobj.read(1)
    if not head:
        return {"error": "empty_file"}, 400
    fileobj.seek(0)

    if not storage.enabled():
        return {"error": "archive_unavailable"}, 503

    archive_id = uuid.uuid4().hex
    try:
        key = storage.put_archive(uid, archive_id, safe, fileobj, content_type)
    except Exception as e:  # noqa: BLE001 — no inline fallback for large files
        storage.log.error("[onboarding_archive] R2 put failed for %s: %s", uid, e)
        return {"error": "archive_failed"}, 502

    ts = time.time()
    size_bytes = size_hint or 0
    if not size_bytes:
        try:
            fileobj.seek(0, 2)  # SEEK_END
            size_bytes = fileobj.tell()
        except Exception:  # noqa: BLE001
            size_bytes = 0
    doc = {
        "archive_id": archive_id,
        "r2_key": key,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "client_job_id": client_job_id,
        "ts": ts,
    }
    db.log_append(uid, _STREAM, doc, ts=ts)
    return {"status": "ok", "archive_id": archive_id, "key": key}, 201

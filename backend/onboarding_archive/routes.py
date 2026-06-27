"""Flask blueprint for onboarding original-file archival.

POST /v1/onboarding/archive (user auth) — a client uploads one onboarding
source file (multipart/form-data). We stream it to the io-user-logs R2 bucket
under onboarding/<uid>/<archive_id>/<file> and append a light index row to the
Postgres ``onboarding_archive`` log stream. No inline-Postgres fallback: files
can be large, so a missing/failed R2 returns 503/502 and the client (best-effort)
just skips.

Auth helper is imported lazily to avoid import cycles at app startup.
"""

from __future__ import annotations

import re
import time
import uuid

from flask import Blueprint, jsonify, request

import db

from . import storage

bp = Blueprint("onboarding_archive", __name__)

_STREAM = "onboarding_archive"
_MAX_REQUEST_BYTES = 25 * 1024 * 1024


def _safe_filename(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (raw or "").strip())[:128]
    return cleaned or "file"


@bp.route("/v1/onboarding/archive", methods=["POST"])
def archive_onboarding_file():
    from accounts.auth import require_user  # below this blueprint — no cycle

    store = require_user()  # aborts 401 on bad auth
    uid = store.user_id

    # Enforce the size cap on the stream itself: Werkzeug (>=3.1) counts bytes
    # during form parsing and raises 413 (RequestEntityTooLarge) when exceeded,
    # so a chunked / no-Content-Length upload can't bypass the limit. Must be set
    # before any request.files/request.form access triggers parsing.
    request.max_content_length = _MAX_REQUEST_BYTES

    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "missing_file"}), 400

    filename = request.form.get("filename") or upload.filename or ""
    safe = _safe_filename(filename)
    content_type = request.form.get("content_type") or upload.mimetype or "application/octet-stream"
    client_job_id = (request.form.get("client_job_id") or "").strip() or None

    # Reject an empty upload by peeking one byte, then rewind for the stream upload.
    head = upload.stream.read(1)
    if not head:
        return jsonify({"error": "empty_file"}), 400
    upload.stream.seek(0)

    if not storage.enabled():
        return jsonify({"error": "archive_unavailable"}), 503

    archive_id = uuid.uuid4().hex
    try:
        key = storage.put_archive(uid, archive_id, safe, upload.stream, content_type)
    except Exception as e:  # noqa: BLE001 — no inline fallback for large files
        storage.log.error("[onboarding_archive] R2 put failed for %s: %s", uid, e)
        return jsonify({"error": "archive_failed"}), 502

    ts = time.time()
    size_bytes = getattr(upload, "content_length", None) or 0
    if not size_bytes:
        try:
            upload.stream.seek(0, 2)  # SEEK_END
            size_bytes = upload.stream.tell()
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
    return jsonify({"status": "ok", "archive_id": archive_id, "key": key}), 201

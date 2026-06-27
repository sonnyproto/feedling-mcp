"""Flask blueprint for client diagnostic-log collection.

Two endpoints:
  - ``POST /v1/diagnostics/logs``            (user auth)  — a client uploads its
    ``diagnostics.log`` text + device/env metadata.
  - ``GET  /v1/admin/diagnostics/logs/<uid>`` (admin auth) — a developer pulls a
    user's recent uploads (presigned R2 download links, or inline content on the
    no-R2 fallback path).

Logs land in the ``io-user-logs`` R2 bucket (plaintext); a light index row per
upload goes into the Postgres ``client_diagnostics`` log stream so the admin
endpoint can enumerate without listing the bucket. When R2 is unconfigured the
text is stored inline in that same index row.

Auth helpers are imported lazily to avoid import cycles during app startup
(``accounts`` / ``admin`` sit below this blueprint).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import db

from . import storage

bp = Blueprint("diagnostics", __name__)

_STREAM = "client_diagnostics"
_MAX_BYTES = 512 * 1024  # mirror the iOS DiagnosticLog ring size
# Hard request-body ceiling, checked from Content-Length *before* the JSON body
# is read into memory — so an oversized (even authenticated) upload is rejected
# 413 rather than buffered. Headroom over _MAX_BYTES covers JSON string-escaping
# of the log text plus the meta object. Scoped to this route (not a global
# MAX_CONTENT_LENGTH) so it never clips the larger frame/photo upload paths.
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 10           # keep the newest N uploads per user
_PRESIGN_TTL = 3600


def _body() -> dict:
    return request.get_json(silent=True) or {}


@bp.route("/v1/diagnostics/logs", methods=["POST"])
def upload_logs():
    from accounts.auth import require_user  # below this blueprint — no cycle

    store = require_user()  # aborts 401 on bad auth
    uid = store.user_id

    # Reject oversized bodies from Content-Length before materializing the JSON.
    if request.content_length is not None and request.content_length > _MAX_REQUEST_BYTES:
        return jsonify({"error": "payload_too_large"}), 413

    payload = _body()
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return jsonify({"error": "empty_content"}), 400
    # Truncate to the same cap the client ring buffer holds (defensive).
    content = content[:_MAX_BYTES]
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    ts = time.time()
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")

    doc: dict = {"meta": meta, "ts": ts}
    if storage.enabled():
        try:
            doc["r2_key"] = storage.put_log(uid, ts_iso, content)
        except Exception as e:  # noqa: BLE001 — fall back to inline rather than lose the log
            storage.log.error("[diagnostics] R2 put failed for %s, storing inline: %s", uid, e)
            doc["content"] = content
    else:
        doc["content"] = content

    db.log_append(uid, _STREAM, doc, ts=ts)
    db.log_trim(uid, _STREAM, _MAX_ROWS)
    return jsonify({"status": "ok"}), 201


@bp.route("/v1/admin/diagnostics/logs/<user_id>", methods=["GET"])
def admin_read_logs(user_id):
    from admin import data_track  # below this blueprint — no cycle

    data_track.require_admin()  # aborts 401/503

    rows = db.log_read(user_id, _STREAM, limit=_MAX_ROWS)
    entries = []
    for doc in rows:
        entry: dict = {"ts": doc.get("ts"), "meta": doc.get("meta", {})}
        key = doc.get("r2_key")
        if key:
            entry["r2_key"] = key
            entry["download_url"] = storage.presign_get(key, _PRESIGN_TTL)
        elif "content" in doc:
            entry["content"] = doc["content"]
        entries.append(entry)
    return jsonify({"user_id": user_id, "logs": entries}), 200

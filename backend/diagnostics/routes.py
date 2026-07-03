"""Flask blueprint for client diagnostic-log collection.

Two endpoints:
  - ``POST /v1/diagnostics/logs``            (user auth)  — a client uploads its
    ``diagnostics.log`` file (multipart/form-data) + device/env metadata.
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

import json
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

import db

from . import storage

bp = Blueprint("diagnostics", __name__)

_STREAM = "client_diagnostics"
_MAX_BYTES = 512 * 1024  # mirror the iOS DiagnosticLog ring size
# Hard request-body ceiling, checked from Content-Length *before* the multipart
# body is read into memory — so an oversized (even authenticated) upload is
# rejected 413 rather than buffered. Headroom over _MAX_BYTES covers the
# multipart envelope + meta. Scoped to this route (not a global
# MAX_CONTENT_LENGTH) so it never clips the larger frame/photo upload paths.
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 10           # keep the newest N uploads per user
_PRESIGN_TTL = 3600


def _parse_meta() -> dict:
    """Read the optional ``meta`` form field (a JSON object string). Stored
    as-is; never trusted for control flow."""
    raw = request.form.get("meta")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


@bp.route("/v1/diagnostics/logs", methods=["POST"])
def upload_logs():
    from accounts.auth import require_user  # below this blueprint — no cycle

    store = require_user()  # aborts 401 on bad auth
    uid = store.user_id

    # Reject oversized bodies from Content-Length before reading the upload.
    if request.content_length is not None and request.content_length > _MAX_REQUEST_BYTES:
        return jsonify({"error": "payload_too_large"}), 413

    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "missing_file"}), 400
    # Read the file bytes and truncate by *bytes* to the ring-buffer cap.
    raw = upload.read(_MAX_BYTES + 1)[:_MAX_BYTES]
    if not raw:
        return jsonify({"error": "empty_file"}), 400
    meta = _parse_meta()

    ts = time.time()
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")

    doc: dict = {"meta": meta, "ts": ts}
    if storage.enabled():
        try:
            doc["r2_key"] = storage.put_log(uid, ts_iso, raw)
        except Exception as e:  # noqa: BLE001 — fall back to inline rather than lose the log
            storage.log.error("[diagnostics] R2 put failed for %s, storing inline: %s", uid, e)
            doc["content"] = raw.decode("utf-8", "replace")
    else:
        doc["content"] = raw.decode("utf-8", "replace")

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


# --- v1 flow trace (debug panel) — off unless FEEDLING_V1_FLOW_TRACE + per-user opt-in ---

@bp.route("/v1/debug/trace", methods=["GET"])
def debug_trace_read():
    from accounts.auth import require_user  # below this blueprint — no cycle
    import debug_trace

    store = require_user()
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    subsystem = str(request.args.get("subsystem") or "")
    return jsonify({
        "enabled": debug_trace.is_enabled(store),
        "deploy_enabled": debug_trace._deploy_enabled(),
        "verbose": debug_trace.verbose_enabled(store),
        "events": debug_trace.read_trace(store, limit=limit, subsystem=subsystem),
    }), 200


@bp.route("/v1/debug/trace/enable", methods=["POST"])
def debug_trace_enable():
    from accounts.auth import require_user
    import debug_trace

    store = require_user()
    payload = request.get_json(silent=True) or {}
    doc = debug_trace.set_enabled(store, bool(payload.get("enabled")))
    return jsonify({"enabled": doc["enabled"], "deploy_enabled": debug_trace._deploy_enabled()}), 200


@bp.route("/v1/debug/trace", methods=["DELETE"])
def debug_trace_clear():
    from accounts.auth import require_user
    import debug_trace

    store = require_user()
    debug_trace.clear_trace(store)
    return jsonify({"status": "ok"}), 200


@bp.route("/v1/debug/trace/event", methods=["POST"])
def debug_trace_emit():
    """A resident consumer (HTTP-only, no DB) reports one flow event. Auth via
    the same per-user key; recording is gated + best-effort. Field-picking keeps
    a careless caller from injecting arbitrary keys."""
    from accounts.auth import require_user
    import debug_trace

    store = require_user()
    payload = request.get_json(silent=True) or {}
    ev = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    dur = ev.get("dur_ms")
    try:
        dur = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur = None
    debug_trace.trace_event(
        store,
        subsystem=str(ev.get("subsystem") or ""),
        type=str(ev.get("type") or ""),
        summary=str(ev.get("summary") or ""),
        explain=str(ev.get("explain") or ""),
        detail=ev.get("detail") if isinstance(ev.get("detail"), dict) else None,
        content_excerpt=ev.get("content_excerpt") if isinstance(ev.get("content_excerpt"), dict) else None,
        actor=str(ev.get("actor") or "vps_resident"),
        status=str(ev.get("status") or "ok"),
        trace_id=str(ev.get("trace_id") or ""),
        turn_id=str(ev.get("turn_id") or ""),
        dur_ms=dur,
    )
    return jsonify({"status": "ok"}), 200

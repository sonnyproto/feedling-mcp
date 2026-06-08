"""Flask blueprint for Extended Perception — all endpoints under /v1/perception.

Reuses app.require_user for auth (X-API-Key -> user_id). require_user is imported
lazily inside a helper so this module can be imported during app startup without
an import cycle.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import service

bp = Blueprint("perception", __name__, url_prefix="/v1/perception")


def _uid() -> str:
    from app import require_user  # lazy; avoids import cycle at module load
    store = require_user()        # aborts 401 on bad auth
    return store.user_id


def _body() -> dict:
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# Generic sparse report (one endpoint writes all permission-gated state)
# ---------------------------------------------------------------------------

@bp.route("/report", methods=["POST"])
def report():
    """Batch-upload the current context_snapshot — an array of {key, data, message},
    each item equivalent to one signal:
      - `key`     : signal name (aliases ok, e.g. location_signal; composite ok, e.g. device)
      - `data`    : JSON STRING (e.g. "{\\"state\\":\\"walking\\"}") or "null" for no value
      - `message` : human-readable note (stored; useful when data is null)

    Photos do NOT go through here — they have their own two-endpoint flow.
    `client_ts` (optional, top-level) timestamps the whole snapshot for the
    freshness/ordering guard; defaults to server receive time.
    """
    uid = _uid()
    payload = _body()
    snapshot = payload.get("context_snapshot")
    if not isinstance(snapshot, list):
        return jsonify({"error": "context_snapshot (list) required"}), 400
    results = service.ingest_snapshot(uid, snapshot, client_ts=payload.get("client_ts"))
    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# Snapshot (agent: one call for current authorized+fresh state)
# ---------------------------------------------------------------------------

@bp.route("/snapshot", methods=["GET"])
def snapshot():
    return jsonify(service.snapshot(_uid()))


# ---------------------------------------------------------------------------
# Permissions (transparency UI)
# ---------------------------------------------------------------------------

@bp.route("/permissions", methods=["GET"])
def get_permissions():
    return jsonify({"capabilities": service.permissions_view(_uid())})


@bp.route("/permissions", methods=["POST"])
def set_permissions():
    uid = _uid()
    return jsonify({"capabilities": service.set_permissions(uid, _body())})


# ---------------------------------------------------------------------------
# Config (geofences / ssid labels / focus map / sensitive bundles)
# ---------------------------------------------------------------------------

@bp.route("/config", methods=["GET"])
def get_config():
    return jsonify(service.config_view(_uid()))


@bp.route("/config", methods=["POST"])
def set_config():
    return jsonify(service.set_config(_uid(), _body()))


# ---------------------------------------------------------------------------
# user_state
# ---------------------------------------------------------------------------

@bp.route("/user_state", methods=["POST"])
def set_user_state():
    uid = _uid()
    value = _body().get("user_state", "default")
    return jsonify({"user_state": service.set_manual_user_state(uid, value)})


# ---------------------------------------------------------------------------
# Photos (two-step + sensitivity gate)
# ---------------------------------------------------------------------------

@bp.route("/photo/evaluate", methods=["POST"])
def photo_evaluate():
    """Single-step photo ingest: metadata + (if usable) the encrypted image."""
    uid = _uid()
    p = _body()
    out, code = service.photo_evaluate(
        uid, p.get("metadata") or {}, p.get("content_envelope"), p.get("exif_gps"))
    return jsonify(out), code


@bp.route("/photos", methods=["GET"])
def photos_recent():
    uid = _uid()
    limit = int(request.args.get("limit", 20))
    out, code = service.photos_recent(uid, limit=limit)
    return jsonify(out), code


@bp.route("/photo/<photo_id>/content", methods=["GET"])
def photo_content(photo_id):
    out, code = service.photo_content(_uid(), photo_id)
    return jsonify(out), code


# ---------------------------------------------------------------------------
# Tier 2 collections (calendar / health) — generic
# ---------------------------------------------------------------------------

@bp.route("/items", methods=["POST"])
def items_ingest():
    uid = _uid()
    p = _body()
    out, code = service.items_ingest(uid, str(p.get("kind") or ""), p.get("items") or [])
    return jsonify(out), code


@bp.route("/items/<kind>", methods=["GET"])
def items_recent(kind):
    uid = _uid()
    limit = int(request.args.get("limit", 20))
    out, code = service.items_recent(uid, kind, limit=limit)
    return jsonify(out), code


# ---------------------------------------------------------------------------
# App usage — GET endpoint for iOS Shortcuts (everything in the URL, incl. ?key=)
# ---------------------------------------------------------------------------

@bp.route("/app_open", methods=["GET"])
def app_open():
    """iOS Shortcut "open app X -> Get Contents of URL" hits this. ALL params go
    in the query string, including the api key (?key=...), which the standard auth
    (_extract_api_key) already accepts. Example:
        GET /v1/perception/app_open?key=<apikey>&app=Instagram&category=social&ts=1733650000
    """
    uid = _uid()
    app = request.args.get("app") or request.args.get("bundle_id") or ""
    category = request.args.get("category")
    client_ts = request.args.get("ts") or request.args.get("client_ts")
    out, code = service.app_open(uid, app, category=category, client_ts=client_ts)
    return jsonify(out), code


@bp.route("/app_usage", methods=["GET"])
def app_usage():
    uid = _uid()
    limit = int(request.args.get("limit", 100))
    since = float(request.args.get("since", 0) or 0)
    out, code = service.app_usage(uid, limit=limit, since_epoch=since)
    return jsonify(out), code

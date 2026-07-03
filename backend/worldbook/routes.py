"""World book HTTP surface: /v1/worldbook/*."""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request

from accounts import auth
from content.routes import _apply_envelope_fields, _swap_envelope_missing

bp = Blueprint("worldbook", __name__)


def _request_envelope(payload: dict) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    nested = payload.get("envelope")
    if nested is not None:
        if not isinstance(nested, dict):
            return None, "envelope must be an object"
        outer_id = str(payload.get("id") or "").strip()
        inner_id = str(nested.get("id") or "").strip()
        if outer_id and inner_id and outer_id != inner_id:
            return None, "top-level id must match envelope id"
        return nested, None
    return payload, None


def _validate_envelope(env: dict, owner_user_id: str) -> str | None:
    missing = _swap_envelope_missing(env)
    if missing:
        return f"envelope missing {missing}"
    entry_id = str(env.get("id") or "").strip()
    if not entry_id:
        return "id required"
    if str(env.get("visibility") or "") not in {"shared", "local_only"}:
        return "envelope.visibility must be 'shared' or 'local_only'"
    if env.get("visibility") == "shared" and not env.get("K_enclave"):
        return "shared visibility requires K_enclave"
    if env.get("owner_user_id") != owner_user_id:
        return "owner_user_id does not match caller"
    return None


@bp.route("/v1/worldbook/list", methods=["GET"])
def worldbook_list():
    store = auth.require_user()
    with store.world_books_lock:
        envelopes = [dict(item) for item in store.world_books]
    return jsonify({"envelopes": envelopes})


@bp.route("/v1/worldbook/upsert", methods=["POST"])
def worldbook_upsert():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    env, parse_error = _request_envelope(payload)
    if parse_error:
        return jsonify({"error": parse_error}), 400
    validation_error = _validate_envelope(env or {}, store.user_id)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    record = {"id": str(env.get("id") or "").strip(), "updated_at": datetime.now().isoformat()}
    _apply_envelope_fields(record, env)
    saved = store.upsert_world_book(record)
    return jsonify({"id": saved["id"]})


@bp.route("/v1/worldbook/delete", methods=["DELETE"])
def worldbook_delete():
    store = auth.require_user()
    entry_id = str(request.args.get("id") or "").strip()
    if not entry_id:
        return jsonify({"error": "id required"}), 400
    store.delete_world_book(entry_id)
    return jsonify({"ok": True})

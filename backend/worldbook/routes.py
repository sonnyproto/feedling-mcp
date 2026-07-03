"""World book HTTP surface: /v1/worldbook/*."""

from __future__ import annotations

from datetime import datetime
import os

from flask import Blueprint, jsonify, request

from accounts import auth
from content.routes import _apply_envelope_fields, _swap_envelope_missing
import debug_trace
import worldbook_readside_core

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


def _validate_content_cap_with_enclave(record: dict) -> tuple[dict, int] | None:
    """Fail closed on deploys that have the enclave configured.

    The upsert endpoint receives ciphertext, so it cannot inspect plaintext
    length locally. The enclave decrypt path owns that check; this call makes the
    write path reject over-cap world book content before it is persisted.
    """
    if not os.environ.get("FEEDLING_ENCLAVE_URL", "").strip():
        return None
    api_key = auth._extract_api_key()
    runtime_token = request.headers.get("X-Feedling-Runtime-Token", "").strip() or None
    try:
        result = worldbook_readside_core.post_enclave_worldbook_match(
            api_key, [record], [], runtime_token=runtime_token)
    except RuntimeError as e:
        return {"error": "worldbook_validate_unavailable", "detail": str(e)}, 503
    rejected = {str(item) for item in result.get("rejected_over_cap") or []}
    entry_id = str(record.get("id") or "").strip()
    if entry_id in rejected:
        return {
            "error": "content_too_long",
            "id": entry_id,
            "max_chars": worldbook_readside_core.WORLD_BOOK_CONTENT_CAP,
        }, 400
    unavailable = {str(item) for item in result.get("unavailable_ids") or []}
    if entry_id in unavailable:
        return {"error": "worldbook_validate_failed", "id": entry_id}, 400
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
    cap_error = _validate_content_cap_with_enclave(record)
    if cap_error:
        body, status = cap_error
        return jsonify(body), status
    saved = store.upsert_world_book(record)
    return jsonify({"id": saved["id"]})


@bp.route("/v1/worldbook/match", methods=["POST"])
def worldbook_match():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    current = str(payload.get("message") or "").strip()
    if current:
        messages = list(messages) + [{"role": "user", "content": current}]
    with store.world_books_lock:
        world_books = [dict(item) for item in store.world_books]
    if not world_books:
        return jsonify({"block": "", "matched_names": [], "rejected_over_cap": [], "unavailable_ids": []})
    runtime_token = request.headers.get("X-Feedling-Runtime-Token", "").strip() or None
    try:
        result = worldbook_readside_core.post_enclave_worldbook_match(
            api_key,
            world_books,
            messages,
            runtime_token=runtime_token,
        )
    except RuntimeError as e:
        return jsonify({"error": "worldbook_match_unavailable", "detail": str(e)}), 503
    block = str(result.get("block") or "")
    matched_names = result.get("matched_names") if isinstance(result.get("matched_names"), list) else []
    if block:
        debug_trace.trace_event(
            store,
            subsystem="worldbook",
            type="worldbook_injected",
            actor="host_agent_runtime",
            summary=f"worldbook injected {len(matched_names)} entries",
            detail={"names": matched_names},
        )
    return jsonify({
        "block": block,
        "matched_names": matched_names,
        "rejected_over_cap": result.get("rejected_over_cap") if isinstance(result.get("rejected_over_cap"), list) else [],
        "unavailable_ids": result.get("unavailable_ids") if isinstance(result.get("unavailable_ids"), list) else [],
    })


@bp.route("/v1/worldbook/delete", methods=["DELETE"])
def worldbook_delete():
    store = auth.require_user()
    entry_id = str(request.args.get("id") or "").strip()
    if not entry_id:
        return jsonify({"error": "id required"}), 400
    store.delete_world_book(entry_id)
    return jsonify({"ok": True})

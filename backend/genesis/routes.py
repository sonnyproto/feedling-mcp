"""Genesis import HTTP surface."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from flask import Blueprint, jsonify, request

import db
from accounts import auth
from genesis import service

bp = Blueprint("genesis", __name__)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


def _bad(error: str, status: int = 400, **extra):
    return jsonify({"error": error, **extra}), status


def _valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.match(str(job_id or "")))


def _job_response(job: dict | None, *, extra: dict | None = None) -> dict:
    body = {
        "job": job or {},
        "privacy_mode": service.PRIVACY_MODE,
        "privacy_copy": service.PRIVACY_COPY,
    }
    if extra:
        body.update(extra)
    return body


@bp.route("/v1/genesis/imports", methods=["POST"])
def genesis_import_create():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    try:
        job, status = service.create_import_job(store, payload)
    except ValueError as e:
        return _bad(str(e), 400)
    return jsonify(_job_response(job, extra={"status": "created" if status == 201 else "exists"})), status


@bp.route("/v1/genesis/imports", methods=["GET"])
def genesis_import_list():
    store = auth.require_user()
    try:
        limit = int(request.args.get("limit") or 20)
    except Exception:
        limit = 20
    return jsonify({
        "jobs": db.genesis_list_jobs(store.user_id, limit=limit),
        "state": db.get_blob(store.user_id, service.GENESIS_STATE_BLOB),
    })


def _json_chunk_payload(payload: dict) -> tuple[bytes, dict[str, Any]]:
    raw = service.b64decode_required(str(payload.get("ciphertext_b64") or ""))
    return raw, payload


def _binary_chunk_payload() -> tuple[bytes, dict[str, Any]]:
    raw = request.get_data(cache=False) or b""
    meta = {
        "byte_start": request.headers.get("X-Byte-Start") or request.args.get("byte_start"),
        "byte_end": request.headers.get("X-Byte-End") or request.args.get("byte_end"),
        "content_sha256": request.headers.get("X-Content-SHA256") or request.args.get("content_sha256"),
        "ciphertext_sha256": request.headers.get("X-Ciphertext-SHA256") or request.args.get("ciphertext_sha256"),
    }
    return raw, meta


@bp.route("/v1/genesis/imports/<job_id>/chunks/<int:seq>", methods=["PUT"])
def genesis_import_put_chunk(job_id: str, seq: int):
    store = auth.require_user()
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    try:
        if request.is_json:
            raw, meta = _json_chunk_payload(request.get_json(silent=True) or {})
        else:
            raw, meta = _binary_chunk_payload()
        byte_start = int(meta.get("byte_start") or 0)
        byte_end = int(meta.get("byte_end") or 0)
        expected_hash = str(meta.get("ciphertext_sha256") or "").strip().lower()
        if expected_hash and expected_hash != hashlib.sha256(raw).hexdigest():
            return _bad("ciphertext_sha256_mismatch", 400)
        aad = meta.get("aad") if isinstance(meta.get("aad"), dict) else {}
        chunk = service.put_chunk(
            store,
            job_id,
            seq=seq,
            encrypted_body=raw,
            byte_start=byte_start,
            byte_end=byte_end,
            content_sha256=str(meta.get("content_sha256") or ""),
            expected_ciphertext_sha256=expected_hash,
            aad=aad,
        )
    except LookupError as e:
        return _bad(str(e), 404)
    except ValueError as e:
        return _bad(str(e), 409 if str(e) == "chunk_hash_conflict" else 400)
    return jsonify({"status": "uploaded", "chunk": chunk}), 200


@bp.route("/v1/genesis/imports/<job_id>/finalize", methods=["POST"])
def genesis_import_finalize(job_id: str):
    store = auth.require_user()
    api_key = auth._extract_api_key()
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    payload = request.get_json(silent=True) or {}
    try:
        job, missing = service.finalize_upload(store, job_id)
    except LookupError as e:
        return _bad(str(e), 404)
    if missing:
        return jsonify(_job_response(job, extra={
            "status": "missing_chunks",
            "missing_chunks": missing[:200],
            "missing_count": len(missing),
        })), 409

    reducer_output = payload.get("reducer_output")
    if isinstance(reducer_output, dict):
        try:
            applied = service.apply_reducer_output(store, api_key, job_id, reducer_output)
            job = db.genesis_get_job(store.user_id, job_id) or job
            return jsonify(_job_response(job, extra={"status": "done", "applied": applied})), 200
        except Exception as e:  # noqa: BLE001
            failed = service.mark_failed(store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}")
            return jsonify(_job_response(failed or job, extra={"status": "failed", "error": str(e)[:240]})), 500

    return jsonify(_job_response(job, extra={"status": "uploaded"})), 202


@bp.route("/v1/genesis/imports/<job_id>/outputs", methods=["POST"])
def genesis_import_apply_outputs(job_id: str):
    store = auth.require_user()
    api_key = auth._extract_api_key()
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    payload = request.get_json(silent=True) or {}
    reducer_output = payload.get("reducer_output") if isinstance(payload.get("reducer_output"), dict) else payload
    if not isinstance(reducer_output, dict):
        return _bad("reducer_output_required", 400)
    try:
        applied = service.apply_reducer_output(store, api_key, job_id, reducer_output)
    except LookupError as e:
        return _bad(str(e), 404)
    except Exception as e:  # noqa: BLE001
        failed = service.mark_failed(store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}")
        return jsonify(_job_response(failed, extra={"status": "failed", "error": str(e)[:240]})), 500
    job = db.genesis_get_job(store.user_id, job_id)
    return jsonify(_job_response(job, extra={"status": "done", "applied": applied})), 200


@bp.route("/v1/genesis/imports/<job_id>", methods=["GET"])
def genesis_import_status(job_id: str):
    store = auth.require_user()
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        return _bad("genesis_job_not_found", 404)
    include_missing = str(request.args.get("include_missing") or "").lower() in {"1", "true", "yes"}
    extra: dict[str, Any] = {
        "state": db.get_blob(store.user_id, service.GENESIS_STATE_BLOB),
        "persona": db.get_blob(store.user_id, service.GENESIS_PERSONA_BLOB),
    }
    if include_missing:
        extra["missing_chunks"] = db.genesis_missing_chunk_seqs(
            store.user_id,
            job_id,
            int(job.get("total_chunks") or 0),
        )
    return jsonify(_job_response(job, extra=extra))

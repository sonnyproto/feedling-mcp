"""Genesis import service: state blobs, ledger helpers, reducer application."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from typing import Any

import db
from bootstrap import gates as boot_gates
from core import envelope as core_envelope
from core import util as core_util
from core.store import UserStore
from identity import service as identity_service
from memory import actions as memory_actions

GENESIS_STATE_BLOB = "genesis_state"
GENESIS_PERSONA_BLOB = "genesis_persona"
GENESIS_SOURCE = "genesis_import"
GENESIS_PERSONA_REF = f"user_blob:{GENESIS_PERSONA_BLOB}"

PRIVACY_MODE = "backend_storage_no_plaintext_user_provider_authorized"
PRIVACY_COPY = (
    "Feedling backend / persistent storage does not see imported plaintext; "
    "plaintext is processed inside the CVM and sent only to the LLM provider "
    "the user configured with their authorized key."
)

DONE_JOB_STATUS = "done"
FAILED_JOB_STATUS = "failed"

ALLOWED_MEMORY_TYPES = {"fact", "event", "quote", "moment"}


def _text(value: Any, max_chars: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:max_chars].strip()


def _now_iso() -> str:
    return core_util._now_iso()


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def b64decode_required(value: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError("invalid_base64_ciphertext") from e


def new_job_id() -> str:
    return core_util._new_public_id("genesis")


def gate_status_for_job_status(status: str) -> str:
    status = str(status or "").strip().lower()
    if status == DONE_JOB_STATUS:
        return DONE_JOB_STATUS
    if status == FAILED_JOB_STATUS:
        return FAILED_JOB_STATUS
    return "processing"


def write_genesis_state(store: UserStore, job: dict, *, status: str | None = None) -> dict:
    job_status = str(job.get("status") or "")
    state = {
        "v": 1,
        "status": status or gate_status_for_job_status(job_status),
        "job_status": job_status,
        "job_id": str(job.get("job_id") or ""),
        "updated_at": _now_iso(),
        "completed_at": str(job.get("completed_at") or ""),
        "memory_action_count": int(job.get("memory_action_count") or 0),
        "identity_status": str(job.get("identity_status") or ""),
        "persona_ref": str(job.get("persona_ref") or ""),
        "persona_sha256": str(job.get("persona_sha256") or ""),
        "error": str(job.get("error") or ""),
        "privacy_mode": str(job.get("privacy_mode") or PRIVACY_MODE),
    }
    db.set_blob(store.user_id, GENESIS_STATE_BLOB, state)
    return state


def create_import_job(store: UserStore, payload: dict) -> tuple[dict, int]:
    job_id = _text(payload.get("job_id") or new_job_id(), 80)
    source_kind = _text(payload.get("source_kind") or payload.get("source") or "unknown", 80)
    try:
        total_chunks = int(payload.get("total_chunks") or 0)
        total_bytes = int(payload.get("total_bytes") or 0)
    except Exception as e:  # noqa: BLE001
        raise ValueError("total_chunks_total_bytes_must_be_int") from e
    if total_chunks < 0 or total_chunks > 100000:
        raise ValueError("total_chunks_out_of_range")
    if total_bytes < 0:
        raise ValueError("total_bytes_out_of_range")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata = {
        **metadata,
        "privacy_copy": PRIVACY_COPY,
    }
    job = db.genesis_create_job(store.user_id, {
        "job_id": job_id,
        "status": "created",
        "source_kind": source_kind,
        "file_manifest_hash": _text(payload.get("file_manifest_hash"), 128),
        "total_chunks": total_chunks,
        "total_bytes": total_bytes,
        "privacy_mode": PRIVACY_MODE,
        "metadata": metadata,
    })
    if job is None:
        existing = db.genesis_get_job(store.user_id, job_id)
        return existing or {"job_id": job_id, "status": "unknown"}, 200
    write_genesis_state(store, job)
    return job, 201


def put_chunk(
    store: UserStore,
    job_id: str,
    *,
    seq: int,
    encrypted_body: bytes,
    byte_start: int,
    byte_end: int,
    content_sha256: str = "",
    expected_ciphertext_sha256: str = "",
    aad: dict | None = None,
) -> dict:
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        raise LookupError("genesis_job_not_found")
    total_chunks = int(job.get("total_chunks") or 0)
    if seq < 0 or (total_chunks and seq >= total_chunks):
        raise ValueError("chunk_seq_out_of_range")
    if not encrypted_body:
        raise ValueError("empty_chunk")
    cipher_hash = _sha256_hex(encrypted_body)
    if expected_ciphertext_sha256 and expected_ciphertext_sha256 != cipher_hash:
        raise ValueError("ciphertext_sha256_mismatch")
    if byte_end <= 0:
        byte_end = byte_start + len(encrypted_body)
    if byte_start < 0 or byte_end < byte_start:
        raise ValueError("invalid_byte_range")
    clean_content_hash = _text(content_sha256, 128)
    clean_aad = dict(aad or {})
    clean_aad.update({
        "user_id": store.user_id,
        "job_id": job_id,
        "seq": seq,
        "content_hash": clean_content_hash,
        "ciphertext_sha256": cipher_hash,
    })
    chunk = db.genesis_put_chunk(
        store.user_id,
        job_id,
        seq=seq,
        byte_start=byte_start,
        byte_end=byte_end,
        ciphertext_sha256=cipher_hash,
        content_sha256=clean_content_hash,
        aad=clean_aad,
        encrypted_body=encrypted_body,
    )
    updated = db.genesis_get_job(store.user_id, job_id) or job
    write_genesis_state(store, updated)
    return chunk


def finalize_upload(store: UserStore, job_id: str) -> tuple[dict, list[int]]:
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        raise LookupError("genesis_job_not_found")
    total_chunks = int(job.get("total_chunks") or 0)
    missing = db.genesis_missing_chunk_seqs(store.user_id, job_id, total_chunks)
    if missing:
        write_genesis_state(store, {**job, "status": "uploading"})
        return job, missing
    finalized = db.genesis_mark_finalized(store.user_id, job_id) or job
    write_genesis_state(store, finalized, status="uploaded")
    return finalized, []


def mark_failed(store: UserStore, job_id: str, error: str) -> dict | None:
    job = db.genesis_set_job_status(store.user_id, job_id, status=FAILED_JOB_STATUS, error=error)
    if job:
        write_genesis_state(store, job, status=FAILED_JOB_STATUS)
    return job


def _memory_action_from_output(item: dict) -> dict:
    mem_type = _text(item.get("type") or "fact", 40).lower()
    if mem_type not in ALLOWED_MEMORY_TYPES:
        raise ValueError(f"unsupported_genesis_memory_type:{mem_type}")
    memory = {
        "type": mem_type,
        "summary": _text(item.get("summary") or item.get("title") or item.get("description"), 2000),
        "content": str(item.get("content") or "").strip()[:5000],
        "bucket": _text(item.get("bucket"), 80),
        "threads": item.get("threads") if isinstance(item.get("threads"), list) else [],
        "occurred_at": _text(item.get("occurred_at") or _now_iso(), 80),
        "source": GENESIS_SOURCE,
        "importance": item.get("importance", 0.5),
        "pulse": item.get("pulse", 0.3),
    }
    if not memory["summary"]:
        raise ValueError("genesis_memory_summary_required")
    if not memory["content"]:
        memory["content"] = f"Memory: {memory['summary']}"
    return {
        "type": "memory.add",
        "memory": memory,
        "reason": _text(item.get("reason") or "Genesis import fact extraction.", 500),
        "capture_mode": GENESIS_SOURCE,
    }


def apply_memory_outputs(store: UserStore, api_key: str | None, output: dict) -> tuple[int, list[dict]]:
    raw_items = output.get("memories")
    if raw_items is None:
        raw_items = output.get("facts")
    if not isinstance(raw_items, list) or not raw_items:
        return 0, []
    actions = [_memory_action_from_output(item) for item in raw_items if isinstance(item, dict)]
    if not actions:
        return 0, []
    results: list[dict] = []
    for idx in range(0, len(actions), 20):
        batch = actions[idx:idx + 20]
        body, status = memory_actions._execute_memory_actions(store, api_key, batch)
        if status >= 400:
            raise RuntimeError(f"memory_actions_failed:{body.get('error', status)}")
        results.extend(list(body.get("results") or []))
    return len(results), results


def _identity_payload_from_output(output: dict) -> dict | None:
    identity = output.get("identity")
    if not isinstance(identity, dict):
        return None
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    clean_dims: list[dict] = []
    for dim in dims[:7]:
        if not isinstance(dim, dict):
            continue
        name = _text(dim.get("name"), 80)
        desc = _text(dim.get("description") or dim.get("evidence"), 500)
        if not name or not desc:
            continue
        try:
            value = int(dim.get("value", 50))
        except Exception:
            value = 50
        clean_dims.append({
            "name": name,
            "value": max(0, min(100, value)),
            "description": desc,
        })
    agent_name = _text(identity.get("agent_name"), 80).strip(" `\"'“”‘’。，,.;；:：!！?？")
    normalized_name = agent_name.lower()
    if (
        normalized_name in identity_service._IDENTITY_RUNTIME_LABELS
        or normalized_name.startswith(("openai/", "anthropic/", "google/", "deepseek/"))
        or re.search(r"\b(?:api|model|runtime|provider|assistant|agent)\b", normalized_name)
    ):
        agent_name = ""
    payload = {
        "agent_name": agent_name,
        # 7.C-write deliberately leaves self_intro/signature for post-respawn TA.
        "self_introduction": "",
        "dimensions": clean_dims,
    }
    return payload


def init_identity_if_absent(store: UserStore, output: dict) -> str:
    existing = identity_service._load_identity(store)
    if existing:
        return "already_initialized"
    payload = _identity_payload_from_output(output)
    if not payload:
        return "not_provided"
    days = output.get("days_with_user")
    identity = output.get("identity") if isinstance(output.get("identity"), dict) else {}
    if days is None:
        days = identity.get("days_with_user", 0)
    try:
        days_int = max(0, int(days))
    except Exception:
        days_int = 0
    evidence = _text(
        output.get("relationship_anchor_evidence")
        or identity.get("relationship_anchor_evidence")
        or f"{GENESIS_SOURCE}:{output.get('job_id') or 'import'}",
        500,
    )
    if len(evidence) < 8:
        evidence = f"{GENESIS_SOURCE}:derived from uploaded import"
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    if envelope is None:
        raise RuntimeError(f"identity_envelope_failed:{err}")
    now = datetime.now().isoformat()
    identity_doc = {
        "v": 1,
        "id": envelope.get("id") or core_util._new_public_id("identity"),
        "body_ct": envelope["body_ct"],
        "nonce": envelope["nonce"],
        "K_user": envelope["K_user"],
        "enclave_pk_fpr": envelope.get("enclave_pk_fpr", ""),
        "visibility": envelope["visibility"],
        "owner_user_id": envelope["owner_user_id"],
        "created_at": now,
        "updated_at": now,
        "relationship_started_at": identity_service._anchor_from_days(days_int, store=store, prefer_memory=True),
        "relationship_anchor_source": GENESIS_SOURCE,
        "relationship_anchor_evidence": evidence,
    }
    if envelope.get("K_enclave"):
        identity_doc["K_enclave"] = envelope["K_enclave"]
    identity_service._save_identity(store, identity_doc)
    boot_gates._log_bootstrap_event(store, "genesis_identity_written_v1", success=True)
    identity_service._append_identity_change(store, {
        "action": "init",
        "reason": "Identity initialized from Genesis import.",
    })
    return "initialized"


def write_persona_artifact(store: UserStore, job_id: str, output: dict) -> tuple[str, str]:
    persona = output.get("persona")
    if isinstance(persona, dict):
        content = str(persona.get("content") or persona.get("text") or "")
        prompt_version = _text(persona.get("prompt_version") or "7.B", 40)
    else:
        content = str(persona or "")
        prompt_version = "7.B"
    content = content.strip()
    if not content:
        return "", ""
    digest = _sha256_hex(content.encode("utf-8"))
    now = _now_iso()
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        content.encode("utf-8"),
        item_id=f"genesis_persona_{job_id}",
    )
    if envelope is None:
        raise RuntimeError(f"persona_envelope_failed:{err}")
    db.set_blob(store.user_id, GENESIS_PERSONA_BLOB, {
        "v": 1,
        "job_id": job_id,
        "source": GENESIS_SOURCE,
        "encrypted": True,
        "content_envelope": envelope,
        "sha256": digest,
        "prompt_version": prompt_version,
        "created_at": now,
        "updated_at": now,
    })
    return GENESIS_PERSONA_REF, digest


def apply_reducer_output(store: UserStore, api_key: str | None, job_id: str, output: dict) -> dict:
    job = db.genesis_get_job(store.user_id, job_id)
    if not job:
        raise LookupError("genesis_job_not_found")
    output = dict(output)
    output["job_id"] = job_id
    db.genesis_set_job_status(store.user_id, job_id, status="processing", output={"stage": "apply_outputs"})
    write_genesis_state(store, {**job, "status": "processing"})
    memory_count, memory_results = apply_memory_outputs(store, api_key, output)
    identity_status = init_identity_if_absent(store, output)
    persona_ref, persona_sha = write_persona_artifact(store, job_id, output)
    result_doc = {
        "memory_action_count": memory_count,
        "memory_results": memory_results,
        "identity_status": identity_status,
        "persona_ref": persona_ref,
        "persona_sha256": persona_sha,
    }
    db.genesis_upsert_output(store.user_id, job_id, "reducer", doc=output, status="applied", ref="inline")
    db.genesis_upsert_output(store.user_id, job_id, "apply", doc=result_doc, status="done", ref="inline")
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output=result_doc,
        memory_action_count=memory_count,
        identity_status=identity_status,
        persona_ref=persona_ref,
        persona_sha256=persona_sha,
    )
    if completed:
        write_genesis_state(store, completed, status=DONE_JOB_STATUS)
    return result_doc

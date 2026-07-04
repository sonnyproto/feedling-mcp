"""Framework-neutral /v1/history_import/* cores (ASGI-migration plan §5.3 / §9).

The upload/status route bodies lifted out of the Flask ``history_import`` blueprint
so the native ASGI routes reuse the exact same logic and return byte-identical
bodies. No ``flask.request`` here — the caller resolves the store + credentials and
passes them in.

The heavy import/distill worker stays in ``hosted.history_import`` and is reached
ONLY through the injected ``start_job`` seam (``_start_history_import_job`` — a
daemon-thread enqueue). ``upload`` therefore ENQUEUES and never runs the distill
inline on the event loop (plan §5.7). The routes-resident helpers are injected so
both frameworks hit the SAME monkeypatchable seams (mirrors genesis_core), which
also avoids a core↔routes import cycle.
"""

from __future__ import annotations

import uuid

import db
from core import util as core_util
from core.store import UserStore


def upload(
    store: UserStore,
    payload: dict,
    *,
    api_key,
    payload_hash,
    client_job_id_fn,
    find_reusable,
    save_job,
    start_job,
    phase_fields,
):
    """POST /v1/history_import/upload — dedupe/reuse or enqueue a new job.

    Returns ``(body, status)``: 202 when a queued/processing job is (re)started,
    200 when a terminal job is reused verbatim, 202 for a freshly enqueued job.
    """
    input_hash = payload_hash(payload)
    client_job_id = client_job_id_fn(payload)
    existing = find_reusable(
        store,
        client_job_id=client_job_id,
        input_hash=input_hash,
    )
    if existing:
        if str(existing.get("status") or "") in {"queued", "processing"}:
            start_job(store, api_key, existing, payload)
            return {"job": existing}, 202
        return {"job": existing}, 200

    job_id = f"hi_{uuid.uuid4().hex[:16]}"
    job = {
        "job_id": job_id,
        "status": "queued",
        "client_job_id": client_job_id,
        "input_hash": input_hash,
        "created_at": core_util._now_iso(),
        "content_chars": len(str(payload.get("content") or "")),
        "ai_persona_chars": len(str(
            payload.get("ai_persona_content")
            or payload.get("ai_persona")
            or ""
        )),
        "character_chars": len(str(
            payload.get("character_content")
            or payload.get("character_card")
            or ""
        )),
        "agent_prompt_chars": len(str(
            payload.get("agent_prompt_content")
            or payload.get("original_system_prompt_content")
            or payload.get("system_prompt_content")
            or payload.get("agent_prompt")
            or payload.get("system_prompt")
            or payload.get("original_system_prompt")
            or ""
        )),
        "persona_chars": len(str(
            payload.get("personal_profile_content")
            or payload.get("persona_content")
            or payload.get("persona")
            or payload.get("profile_content")
            or ""
        )),
        "memory_summary_chars": len(str(
            payload.get("memory_summary_content")
            or payload.get("memory_summary")
            or payload.get("memory_sample_content")
            or payload.get("memory_sample")
            or ""
        )),
        "ai_persona_filename": str(payload.get("ai_persona_filename") or "")[:240],
        "character_filename": str(
            payload.get("character_filename")
            or payload.get("character_card_filename")
            or ""
        )[:240],
        "agent_prompt_filename": str(
            payload.get("agent_prompt_filename")
            or payload.get("original_system_prompt_filename")
            or payload.get("system_prompt_filename")
            or ""
        )[:240],
        "persona_filename": str(
            payload.get("personal_profile_filename")
            or payload.get("persona_filename")
            or ""
        )[:240],
        "memory_summary_filename": str(
            payload.get("memory_summary_filename")
            or payload.get("memory_sample_filename")
            or ""
        )[:240],
        "chat_ready": False,
        "background_status": "not_started",
        **phase_fields("upload_received"),
    }
    save_job(store, job)
    start_job(store, api_key, job, payload)
    print(f"[history_import:{store.user_id}] job={job_id} queued async=1 client_job_id={client_job_id[:24]}")
    return {"job": job}, 202


def status(store: UserStore, job_id, *, job_kind):
    """GET /v1/history_import/status/<job_id> — the single job blob, or 404."""
    data = db.get_blob(store.user_id, job_kind(job_id))
    if not data:
        return {"error": "job_not_found"}, 404
    return {"job": data}, 200

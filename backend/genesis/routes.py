"""Genesis import HTTP surface."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import date
from typing import Any

from flask import Blueprint, jsonify, request

import db
from accounts import auth
from accounts import runtime_auth
from genesis import dedup, foreground, foreground_identity, service, worker
from hosted import config_store as hosted_config_store
from hosted import history_import
from identity import service as identity_service

bp = Blueprint("genesis", __name__)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_SECONDS_PER_DAY = 24 * 60 * 60
_PLAINTEXT_ACTIVE_LOCK = threading.Lock()
_PLAINTEXT_ACTIVE_JOBS: set[tuple[str, str]] = set()
_PLAINTEXT_SOURCE_ORDER = (
    history_import._AI_PERSONA_SOURCE,
    history_import._HISTORY_SOURCE,
    history_import._MEMORY_SUMMARY_SOURCE,
    history_import._USER_PROFILE_SOURCE,
)
_PLAINTEXT_SUPPORT_SOURCE_FAMILIES = {
    history_import._AI_PERSONA_SOURCE,
    history_import._USER_PROFILE_SOURCE,
    history_import._MEMORY_SUMMARY_SOURCE,
}
_PLAINTEXT_MODES = {"onboarding", "add_memory", "update_identity"}


def _bad(error: str, status: int = 400, **extra):
    return jsonify({"error": error, **extra}), status


def _valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.match(str(job_id or "")))


def _job_response(job: dict | None, *, extra: dict | None = None) -> dict:
    job = job or {}
    # Report the client-facing stage name (v2-internal -> legacy phase the old iOS maps),
    # so shipped apps show correct copy without an update. Stored stage is unchanged.
    out = job.get("output")
    if isinstance(out, dict) and out.get("stage"):
        job = {**job, "output": {**out, "stage": service.public_stage(out["stage"])}}
    body = {
        "job": job,
        "privacy_mode": service.PRIVACY_MODE,
        "privacy_copy": service.PRIVACY_COPY,
    }
    if extra:
        body.update(extra)
    return body


def _plaintext_fresh_start_message() -> dict:
    return {
        "role": "user",
        "content": "Fresh start. No persona profile or previous chat history was provided.",
        "ts": None,
        "source": history_import._FRESH_START_SOURCE,
        "source_family": history_import._FRESH_START_SOURCE,
    }


def _plaintext_source_kind(history_messages: list[dict], support_messages: list[dict]) -> str:
    if history_messages:
        return history_import._HISTORY_SOURCE
    families = {
        history_import._import_source_family(str(m.get("source") or m.get("source_family") or ""))
        for m in support_messages
    }
    if len(families) == 1:
        family = next(iter(families))
        if family == history_import._AI_PERSONA_SOURCE:
            return "ai_persona"
        if family == history_import._USER_PROFILE_SOURCE:
            return "user_profile"
        if family == history_import._MEMORY_SUMMARY_SOURCE:
            return "memory_summary"
    return history_import._HISTORY_SOURCE


def _plaintext_mode_from_client_job_id(client_job_id: str) -> str:
    lowered = str(client_job_id or "").strip().lower()
    if lowered.startswith("garden-"):
        return "add_memory"
    if lowered.startswith("identity-"):
        return "update_identity"
    return "onboarding"


def _plaintext_mode(payload: dict, *, client_job_id: str) -> str:
    explicit = str(payload.get("mode") or "").strip().lower()
    if explicit in _PLAINTEXT_MODES:
        return explicit
    return _plaintext_mode_from_client_job_id(client_job_id)


def _plaintext_route_family(msg: dict) -> str:
    family = history_import._import_source_family(str(msg.get("source") or msg.get("source_family") or ""))
    if family in _PLAINTEXT_SUPPORT_SOURCE_FAMILIES:
        return family
    return history_import._HISTORY_SOURCE


def _plaintext_chunk_texts_for_messages(messages: list[dict], *, window_limit: int) -> list[str]:
    windows = history_import._build_transcript_windows(
        messages,
        max_chars=18000,
        max_windows=window_limit,
    )
    if len(windows) > window_limit:
        windows = history_import._select_evenly(windows, window_limit)
    return [
        str(window.get("text") or "").strip()
        for window in windows
        if str(window.get("text") or "").strip()
    ]


def _plaintext_source_groups(analysis_messages: list[dict], *, window_limit: int) -> list[dict]:
    buckets: dict[str, list[dict]] = {family: [] for family in _PLAINTEXT_SOURCE_ORDER}
    for msg in analysis_messages:
        if not isinstance(msg, dict):
            continue
        buckets.setdefault(_plaintext_route_family(msg), []).append(msg)

    groups: list[dict] = []
    for source_kind in _PLAINTEXT_SOURCE_ORDER:
        messages = buckets.get(source_kind) or []
        if not messages:
            continue
        chunk_texts = _plaintext_chunk_texts_for_messages(messages, window_limit=window_limit)
        if not chunk_texts:
            continue
        groups.append({
            "source_kind": source_kind,
            "source_family": worker._source_family(source_kind),
            "chunk_texts": chunk_texts,
            "message_count": len(messages),
        })
    return groups


def _plaintext_timeline_span_days(messages: list[dict]) -> int:
    timestamps: list[float] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        raw = msg.get("ts")
        if raw in (None, ""):
            continue
        try:
            ts = float(raw)
        except Exception:
            continue
        if math.isfinite(ts):
            timestamps.append(ts)
    if len(timestamps) < 2:
        return 0
    return int(max(0.0, max(timestamps) - min(timestamps)) // _SECONDS_PER_DAY)


def _plaintext_relationship_anchor(payload: dict, *, messages: list[dict]) -> dict:
    # Reuse the ORIGINAL relationship-start logic (history_import._relationship_start_from_import):
    # typed date -> use it; else the EARLIEST message timestamp; else today (fresh_start).
    # The genesis path previously reinvented this and left relationship_started_at BLANK
    # for the no-typed-date case, which fell through to prefer_memory (genesis' today-dated
    # core memories) and collapsed 相处天数 to 0.
    start, _evidence = history_import._relationship_start_from_import(payload, messages)
    if not start:
        return {"relationship_started_at": "", "days_with_user": 0, "relationship_anchor_evidence": ""}
    iso = start.isoformat()
    return {
        "relationship_started_at": iso,
        "days_with_user": max(0, (date.today() - start).days),
        "relationship_anchor_evidence": f"plaintext_import:relationship_started_at={iso}",
    }


def _prepare_plaintext_import(payload: dict) -> dict:
    content = str(payload.get("content") or "")
    fmt = str(payload.get("format") or "auto").strip().lower()
    warnings: list[str] = []
    history_messages = history_import._parse_import_history_content(content, fmt, warnings)
    support_messages = history_import._persona_support_messages(payload)
    if not history_messages and not support_messages:
        if not bool(payload.get("fresh_start")):
            raise ValueError(
                "content, ai_persona_content, character_content, personal_profile_content, "
                "memory_summary_content, persona_content, or fresh_start=true required"
            )
        support_messages = [_plaintext_fresh_start_message()]
        warnings.append("fresh_start_without_support_material")

    analysis_messages = support_messages + history_messages
    profile = history_import._history_import_profile(
        history_messages,
        support_messages,
        content_chars=len(content),
    )
    window_limit = int(profile.get("total_windows") or 8)
    source_groups = _plaintext_source_groups(analysis_messages, window_limit=window_limit)
    chunk_texts = [
        text
        for group in source_groups
        for text in (group.get("chunk_texts") or [])
        if str(text or "").strip()
    ]
    if not chunk_texts:
        raise ValueError("plaintext_import_empty")
    timeline_span_days = _plaintext_timeline_span_days(history_messages)
    relationship_anchor = _plaintext_relationship_anchor(payload, messages=history_messages)
    return {
        "analysis_messages": analysis_messages,
        "chunk_texts": chunk_texts,
        "content_bytes": len(content.encode("utf-8")),
        "history_messages": history_messages,
        "profile": profile,
        "relationship_anchor": relationship_anchor,
        "source_kind": _plaintext_source_kind(history_messages, support_messages),
        "source_groups": source_groups,
        "source_stats": history_import._import_source_stats(analysis_messages),
        "support_messages": support_messages,
        "timeline_span_days": timeline_span_days,
        "warnings": warnings,
    }


def _plaintext_job_metadata(
    payload: dict,
    prepared: dict,
    *,
    client_job_id: str,
    input_hash: str,
    mode: str,
) -> dict:
    profile = prepared.get("profile") if isinstance(prepared.get("profile"), dict) else {}
    source_stats = prepared.get("source_stats") if isinstance(prepared.get("source_stats"), dict) else {}
    metadata: dict[str, Any] = {
        "ingest": "plaintext",
        "input_hash": input_hash,
        "client_job_id": client_job_id,
        "mode": mode if mode in _PLAINTEXT_MODES else "onboarding",
        "history_tier": str(profile.get("tier") or "small"),
        "window_count": len(prepared.get("chunk_texts") or []),
        "history_count": int(profile.get("message_count") or 0),
        "timeline_span_days": int(prepared.get("timeline_span_days") or 0),
        "support_count": int(profile.get("support_count") or 0),
        "warning_count": len(prepared.get("warnings") or []),
        "content_bytes": int(prepared.get("content_bytes") or 0),
    }
    filename_fields = [
        payload.get("history_filename"),
        payload.get("ai_persona_filename"),
        payload.get("character_filename") or payload.get("character_card_filename"),
        payload.get("personal_profile_filename") or payload.get("persona_filename"),
        payload.get("memory_summary_filename") or payload.get("memory_sample_filename"),
    ]
    metadata["file_count"] = len([x for x in filename_fields if str(x or "").strip()])
    for prefix, family in (
        ("ai_persona", history_import._AI_PERSONA_SOURCE),
        ("user_profile", history_import._USER_PROFILE_SOURCE),
        ("memory_summary", history_import._MEMORY_SUMMARY_SOURCE),
        ("fresh_start", history_import._FRESH_START_SOURCE),
    ):
        stats = source_stats.get(family) if isinstance(source_stats.get(family), dict) else {}
        metadata[f"{prefix}_count"] = int(stats.get("count") or 0)
    return metadata


def _metadata_for_job(job: dict | None) -> dict:
    metadata = (job or {}).get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _metadata_plaintext_mode(metadata: dict) -> str:
    mode = str(metadata.get("mode") or "").strip().lower()
    if mode in _PLAINTEXT_MODES:
        return mode
    return _plaintext_mode_from_client_job_id(str(metadata.get("client_job_id") or ""))


def _find_reusable_plaintext_job(
    store,
    *,
    client_job_id: str,
    input_hash: str,
    mode: str,
) -> dict | None:
    try:
        jobs = db.genesis_list_jobs(store.user_id, limit=100)
    except Exception:
        return None
    for job in jobs:
        if str(job.get("status") or "") == service.FAILED_JOB_STATUS:
            continue
        metadata = _metadata_for_job(job)
        if metadata.get("ingest") != "plaintext":
            continue
        if _metadata_plaintext_mode(metadata) != mode:
            continue
        if client_job_id and str(metadata.get("client_job_id") or "") == client_job_id:
            return job
        if input_hash and str(metadata.get("input_hash") or "") == input_hash:
            return job
    return None


def _plaintext_identity_name(identity: dict | None) -> str:
    if not isinstance(identity, dict):
        return ""
    name = str(identity.get("agent_name") or "").strip()
    return name[:80]


def _plaintext_identity_dimensions(identity: dict | None) -> list[dict]:
    if not isinstance(identity, dict):
        return []
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    return [dim for dim in dims if isinstance(dim, dict)][:7]


def _plaintext_positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _plaintext_memory_key(item: dict) -> str:
    return "|".join([
        re.sub(r"\s+", " ", str(item.get("type") or "")).strip().lower(),
        re.sub(r"\s+", " ", str(item.get("summary") or item.get("title") or "")).strip().lower()[:500],
        re.sub(r"\s+", " ", str(item.get("content") or item.get("description") or "")).strip().lower()[:1000],
    ])


def _plaintext_merge_memories(outputs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for output in outputs:
        raw_items = output.get("memories")
        if raw_items is None:
            raw_items = output.get("facts")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            key = _plaintext_memory_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _plaintext_merge_voice_workset(outputs: list[dict]) -> dict:
    notes: list[str] = []
    exemplars: list[dict] = []
    seen_notes: set[str] = set()
    seen_exemplars: set[str] = set()
    for output in outputs:
        if str(output.get("source_family") or "") == "user_profile":
            continue
        workset = output.get("voice_workset") if isinstance(output.get("voice_workset"), dict) else {}
        for note in workset.get("behavior_notes") if isinstance(workset.get("behavior_notes"), list) else []:
            clean = re.sub(r"\s+", " ", str(note or "").strip())
            if not clean or clean in seen_notes:
                continue
            seen_notes.add(clean)
            notes.append(clean)
        for exemplar in workset.get("exemplars") if isinstance(workset.get("exemplars"), list) else []:
            if not isinstance(exemplar, dict):
                continue
            key = json.dumps(exemplar, ensure_ascii=False, sort_keys=True, default=str)[:2000]
            if key in seen_exemplars:
                continue
            seen_exemplars.add(key)
            exemplars.append(exemplar)
    if not notes and not exemplars:
        return {}
    return {
        "behavior_notes": notes[:16],
        "exemplars": exemplars[:80],
    }


def _plaintext_merge_reducer_outputs(outputs: list[dict], *, relationship_anchor: dict | None = None) -> dict:
    relationship_anchor = relationship_anchor if isinstance(relationship_anchor, dict) else {}
    usable_identity_outputs = [
        output for output in outputs
        if str(output.get("source_family") or "") != "user_profile"
        and isinstance(output.get("identity"), dict)
    ]

    def first_identity_name(*families: str) -> str:
        for family in families:
            for output in usable_identity_outputs:
                if str(output.get("source_family") or "") != family:
                    continue
                name = _plaintext_identity_name(output.get("identity"))
                if name:
                    return name
        return ""

    def first_identity_dims(*families: str) -> list[dict]:
        for family in families:
            for output in usable_identity_outputs:
                if str(output.get("source_family") or "") != family:
                    continue
                dims = _plaintext_identity_dimensions(output.get("identity"))
                if dims:
                    return dims
        return []

    agent_name = first_identity_name("ai_persona", "history", "memory_summary")
    dimensions = first_identity_dims("ai_persona", "history")
    identity = {"agent_name": agent_name, "dimensions": dimensions} if (agent_name or dimensions) else {}

    persona: dict = {}
    for output in outputs:
        if str(output.get("source_family") or "") == "user_profile":
            continue
        candidate = output.get("persona") if isinstance(output.get("persona"), dict) else {}
        if str(candidate.get("content") or "").strip():
            persona = candidate

    voice_workset = _plaintext_merge_voice_workset(outputs)
    voice = {
        "behavior_notes_count": len(voice_workset.get("behavior_notes") or []),
        "exemplar_count": len(voice_workset.get("exemplars") or []),
        "founding_exemplar_count": len([
            item for item in (voice_workset.get("exemplars") or [])
            if isinstance(item, dict) and item.get("founding")
        ]),
    }
    if not voice_workset:
        for output in reversed(outputs):
            candidate = output.get("voice") if isinstance(output.get("voice"), dict) else {}
            if candidate:
                voice = candidate
                break

    output_days = max(_plaintext_positive_int(output.get("days_with_user")) for output in outputs) if outputs else 0
    days = _plaintext_positive_int(relationship_anchor.get("days_with_user")) or output_days
    evidence = str(relationship_anchor.get("relationship_anchor_evidence") or "").strip()
    if not evidence:
        evidence = " | ".join(
            str(output.get("relationship_anchor_evidence") or "").strip()
            for output in outputs
            if str(output.get("relationship_anchor_evidence") or "").strip()
        )[:500]

    source_families = [str(output.get("source_family") or "") for output in outputs if str(output.get("source_family") or "")]
    merged: dict[str, Any] = {
        "memories": _plaintext_merge_memories(outputs),
        "source_kind": "plaintext_multi_source" if len(source_families) > 1 else str((outputs[0] if outputs else {}).get("source_kind") or "history_import"),
        "source_family": "merged" if len(set(source_families)) > 1 else (source_families[0] if source_families else "history"),
        "voice": voice,
        "days_with_user": days,
    }
    if identity:
        merged["identity"] = identity
    if evidence:
        merged["relationship_anchor_evidence"] = evidence
    if str(relationship_anchor.get("relationship_started_at") or "").strip():
        merged["relationship_started_at"] = str(relationship_anchor.get("relationship_started_at") or "").strip()
    if persona:
        merged["persona"] = persona
    if voice_workset:
        merged["voice_workset"] = voice_workset
    return merged


def _plaintext_existing_persona_from_output(output: dict) -> dict:
    persona = output.get("persona") if isinstance(output.get("persona"), dict) else {}
    content = str(persona.get("content") or "").strip()
    if not content:
        return {}
    return {
        "content": content,
        "source_family": str(persona.get("source_family") or output.get("source_family") or ""),
    }


def _plaintext_existing_voice_from_output(output: dict) -> dict:
    workset = output.get("voice_workset") if isinstance(output.get("voice_workset"), dict) else {}
    if not workset:
        return {}
    return {
        "behavior_notes": workset.get("behavior_notes") if isinstance(workset.get("behavior_notes"), list) else [],
        "exemplars": workset.get("exemplars") if isinstance(workset.get("exemplars"), list) else [],
    }


def _merged_has_identity(merged: dict) -> bool:
    """True when the reduce output carries a usable Identity Card (a name or any
    dimension). Mirrors service._identity_payload_from_output's emptiness rule."""
    ident = merged.get("identity") if isinstance(merged.get("identity"), dict) else {}
    dims = ident.get("dimensions") if isinstance(ident.get("dimensions"), list) else []
    return bool(str(ident.get("agent_name") or "").strip()) or len(dims) > 0


def _provider_identity_failure(warnings: list[str] | tuple[str, ...] | None) -> str:
    for warning in warnings or []:
        text = str(warning or "")
        if text.startswith("provider_identity_failed:"):
            return text
    return ""


def _run_plaintext_genesis_v2(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
    relationship_anchor: dict | None = None,
    analysis_messages: list[dict] | None = None,
) -> bool:
    """Genesis v2 foreground-fast orchestration (behind FEEDLING_GENESIS_V2_ENABLED).

    Foreground restores the legacy chat_ready contract: pick 3-5 core memories, derive a
    REAL Identity Card via the existing hosted deriver (foreground_identity, no new
    prompt), write identity + relationship anchor (_store_identity_payload) + a greeting,
    and only THEN complete the job — so the app enters with a named/anchored TA, never a
    blank home. Background then does the heavy full reduce (rest of memories + voice +
    persona), skipping the core and NOT re-writing identity; a background failure never
    fails the already-greetable onboarding.

    Edge: if the deriver can't produce an identity, fall back to the v1-style apply
    (complete on core + background fills identity) — never a fake-complete.

    Returns True when it handled the job. Returns False only when there's nothing to work
    with (no core), so the caller runs the v1 full path instead.
    """
    # primary group: prefer the real chat history (best greeting signal), else the first
    fg_group = next(
        (g for g in source_groups if str(g.get("source_family") or "") == "history"),
        source_groups[0],
    )
    fg_idx = source_groups.index(fg_group) + 1
    fg_kind = str(fg_group.get("source_kind") or history_import._HISTORY_SOURCE)
    fg_family = str(fg_group.get("source_family") or worker._source_family(fg_kind))

    db.genesis_set_job_status(
        store.user_id, job_id, status="processing",
        output={"stage": "genesis_v2_foreground", "source_family": fg_family}, processed_chunks=0,
    )
    foreground_reduces: list[dict] = []
    primary_reduce: dict | None = None
    for idx, group in enumerate(source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(t) for t in (group.get("chunk_texts") or []) if str(t or "").strip()]
        if not group_chunks:
            continue
        reduce = worker.build_foreground_output_from_texts(
            user_id=store.user_id, job_id=job_id,
            key_prefix=f"{job_id}:source_pass:{idx}:{group_family}",
            runtime=runtime, chunk_texts=group_chunks, source_kind=group_kind,
            write_core=False,
        )
        foreground_reduces.append(reduce)
        if idx == fg_idx:
            primary_reduce = reduce

    if not foreground_reduces:
        return False
    primary_reduce = primary_reduce or foreground_reduces[0]
    all_fact_candidates: list[dict] = []
    for reduce in foreground_reduces:
        candidates = reduce.get("all_fact_candidates") or reduce.get("core_fact_candidates") or []
        all_fact_candidates.extend([c for c in candidates if isinstance(c, dict)])

    core = primary_reduce.get("core_fact_candidates") or foreground.select_core_for_foreground(all_fact_candidates)
    if not core:
        return False  # nothing to work with -> let the v1 full path handle it

    full_fact_write = worker.build_memory_output_from_fact_candidates(
        user_id=store.user_id,
        job_id=job_id,
        key_prefix=f"{job_id}:foreground_full",
        runtime=runtime,
        fact_candidates=all_fact_candidates,
    )
    fg_merged = _plaintext_merge_reducer_outputs(
        [{**primary_reduce, **full_fact_write}],
        relationship_anchor=relationship_anchor,
    )
    full_memories = fg_merged.get("memories") or []
    days = int((relationship_anchor or {}).get("days_with_user") or 0)
    # explicit relationship_started_at (user typed a date) -> honored verbatim below,
    # per the documented priority; blank -> _store_identity_payload falls back to memory.
    explicit_started_at = str((relationship_anchor or {}).get("relationship_started_at") or "").strip()
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)

    # Foreground-ready contract, restored from the legacy chat_ready: the user only
    # enters once the Identity Card is REAL. Derive it with the EXISTING hosted deriver
    # (orchestration only — no new prompt/logic), then write identity + relationship
    # anchor + a greeting BEFORE completing. So the home is never blank and validate's
    # identity_card passes at entry. Heavy voice/persona/full-memory stay in background.
    identity_payload, id_warnings = foreground_identity.derive_foreground_identity(
        runtime=runtime, analysis_messages=msgs, core_memories=full_memories,
        days_with_user=days, language=language,
    )
    provider_failure = _provider_identity_failure(id_warnings)
    if provider_failure:
        service.mark_failed(store, job_id, f"foreground_identity_failed:{provider_failure}")
        return True
    identity_first = bool(msgs) and foreground_identity.has_identity_signal(identity_payload)

    if identity_first:
        # core memories now; identity via the legacy _store_identity_payload (exact old
        # path — writes the card + relationship anchor); greeting via the legacy pair.
        mem_count, _mr = service.apply_memory_outputs(store, api_key, {"memories": full_memories})
        history_import._store_identity_payload(
            store, identity_payload, days_with_user=days,
            evidence=f"genesis_foreground:{job_id}", language=language,
            relationship_started_at=explicit_started_at,
        )
        _append_plaintext_onboarding_greeting(
            store,
            runtime=runtime,
            analysis_messages=msgs,
            memories=full_memories,
            identity_payload=identity_payload,
            days=days,
            language=language,
        )
        completed = db.genesis_complete_job(
            store.user_id, job_id, output={"stage": "genesis_v2_foreground_ready"},
            memory_action_count=mem_count, identity_status="initialized",
            persona_ref="", persona_sha256="",
        )
        if completed:
            service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)
    else:
        # edge: foreground couldn't derive an identity -> current backstop (complete on
        # core + let the background fill identity via init_identity/persona baseline).
        # Rare, never worse than before; the iOS minimal-seed page is the real fix.
        _append_plaintext_onboarding_greeting(
            store,
            runtime=runtime,
            analysis_messages=msgs,
            memories=full_memories,
            identity_payload=identity_payload,
            days=days,
            language=language,
        )
        service.apply_reducer_output(store, api_key, job_id, fg_merged)

    # foreground core memory texts -> background as "already saved, don't repeat"
    # (semantic dedup of reworded twins lives in the model).
    core_memory_texts = [
        t for t in (str((m or {}).get("summary") or (m or {}).get("content") or "").strip()
                    for m in full_memories) if t
    ]

    # background continuation — never fails the (already greetable) job. When the
    # foreground already wrote identity, the background must NOT re-write it.
    try:
        _run_plaintext_background_enrichment(
            store, api_key, job_id, runtime=runtime, source_groups=source_groups,
            relationship_anchor=relationship_anchor,
            skip_family=fg_family, skip_texts=foreground.core_skip_texts(core),
            known_memories=core_memory_texts, write_identity=not identity_first,
            include_memory=False,
        )
    except Exception as e:  # noqa: BLE001
        db.genesis_set_job_status(
            store.user_id, job_id, status=service.DONE_JOB_STATUS,
            output={"stage": "genesis_v2_background_deferred",
                    "error": f"{type(e).__name__}:{str(e)[:180]}"},
        )
    return True


def _run_plaintext_background_enrichment(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
    relationship_anchor: dict | None,
    skip_family: str,
    skip_texts: set[str],
    known_memories: list[str] | None = None,
    write_identity: bool = True,
    include_memory: bool = True,
) -> None:
    """Background continuation: the full reduce over every group (skipping the core the
    foreground already wrote for skip_family), then apply the REST incrementally —
    memories + persona + voice. Does NOT re-complete the job (foreground already did).

    Dedup is two-layered against the foreground core (`known_memories`): the model
    dedups reworded twins semantically inside fact_write (known_memories = "already
    saved, don't repeat"), and a CONSERVATIVE lexical backstop drops any near-identical
    survivor before apply. The lexical threshold is high on purpose — it must never
    merge two distinct same-template facts (美式/拿铁, 蛋子/金毛)."""
    known = [t for t in (str(x or "").strip() for x in (known_memories or [])) if t]
    reducer_outputs: list[dict] = []
    existing_persona: dict = {}
    existing_voice: dict = {}
    for idx, group in enumerate(source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(t) for t in (group.get("chunk_texts") or []) if str(t or "").strip()]
        if not group_chunks:
            continue
        db.genesis_set_job_status(
            store.user_id, job_id, status=service.DONE_JOB_STATUS,
            output={"stage": "genesis_v2_background", "source_family": group_family,
                    "source_pass": idx, "source_pass_total": len(source_groups)},
        )
        output = worker.build_reducer_output_from_texts(
            user_id=store.user_id, job_id=job_id,
            key_prefix=f"{job_id}:source_pass:{idx}:{group_family}",
            runtime=runtime, chunk_texts=group_chunks, source_kind=group_kind,
            existing_persona=existing_persona, existing_voice=existing_voice,
            # only the foreground group needs its already-written core skipped/deduped
            skip_fact_texts=skip_texts if group_family == skip_family else None,
            known_memories=known if group_family == skip_family else None,
            include_memory=include_memory,
        )
        reducer_outputs.append(output)
        next_persona = _plaintext_existing_persona_from_output(output)
        if next_persona:
            existing_persona = next_persona
        next_voice = _plaintext_existing_voice_from_output(output)
        if next_voice:
            existing_voice = next_voice

    merged = _plaintext_merge_reducer_outputs(reducer_outputs, relationship_anchor=relationship_anchor)
    # conservative lexical backstop: drop any near-identical survivor the model missed
    if include_memory and known and isinstance(merged.get("memories"), list):
        kept, dropped = dedup.filter_semantic_dups(merged["memories"], known)
        if dropped:
            merged["memories"] = kept
    # apply the REST without re-completing: memories (core already excluded), persona, voice
    if include_memory:
        service.apply_memory_outputs(store, api_key, merged)
    # Identity is normally written by the FOREGROUND now (identity-first contract), so the
    # background skips it (write_identity=False). Only the edge fallback (foreground could
    # not derive an identity) asks the background to fill it — from the full reduce, or a
    # persona-derived baseline so onboarding never wedges on an empty identity_card.
    if write_identity:
        if not _merged_has_identity(merged) and isinstance(merged.get("persona"), dict):
            persona_content = str(merged["persona"].get("content") or "").strip()
            if persona_content:
                baseline = worker.derive_identity_from_persona(
                    user_id=store.user_id, job_id=job_id, runtime=runtime, persona_content=persona_content,
                )
                if baseline.get("agent_name") or baseline.get("dimensions"):
                    merged["identity"] = baseline
        service.init_identity_if_absent(store, merged, api_key)
    service.write_persona_artifact(store, job_id, merged)
    service.write_voice_artifact(store, job_id, merged)
    db.genesis_set_job_status(
        store.user_id, job_id, status=service.DONE_JOB_STATUS, output={"stage": "genesis_v2_done"},
    )


def _append_plaintext_onboarding_greeting(
    store,
    *,
    runtime,
    analysis_messages: list[dict],
    memories: list[dict],
    identity_payload: dict,
    days: int,
    language: str,
) -> str:
    try:
        greeting_text, _warnings = history_import._generate_model_api_onboarding_greeting(
            runtime,
            analysis_messages,
            memories,
            identity_payload,
            days,
            language,
        )
    except Exception:
        greeting_text = ""
    if not str(greeting_text or "").strip():
        greeting_text = (
            "好久不见，很高兴又能和你聊天。"
            if str(language).startswith("zh")
            else "Good to see you again — I'm glad we can talk."
        )
    try:
        history_import._append_model_api_onboarding_greeting(store, greeting_text)
    except Exception:
        return ""
    return str(greeting_text or "")


def _run_plaintext_add_memory_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    runtime,
    source_groups: list[dict],
) -> None:
    fact_candidates: list[dict] = []
    first_output: dict = {}
    for idx, group in enumerate(source_groups, start=1):
        group_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
        group_family = str(group.get("source_family") or worker._source_family(group_kind))
        group_chunks = [str(text) for text in (group.get("chunk_texts") or []) if str(text or "").strip()]
        if not group_chunks:
            continue
        db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={
                "stage": "plaintext_add_memory",
                "source_family": group_family,
                "source_pass": idx,
                "source_pass_total": len(source_groups),
            },
        )
        output = worker.build_foreground_output_from_texts(
            user_id=store.user_id,
            job_id=job_id,
            key_prefix=f"{job_id}:add_memory:{idx}:{group_family}",
            runtime=runtime,
            chunk_texts=group_chunks,
            source_kind=group_kind,
            write_core=False,
        )
        if not first_output:
            first_output = output
        candidates = output.get("all_fact_candidates") or output.get("core_fact_candidates") or []
        fact_candidates.extend([item for item in candidates if isinstance(item, dict)])

    memory_output = worker.build_memory_output_from_fact_candidates(
        user_id=store.user_id,
        job_id=job_id,
        key_prefix=f"{job_id}:add_memory:fact_write",
        runtime=runtime,
        fact_candidates=fact_candidates,
    )
    merged = _plaintext_merge_reducer_outputs([{**first_output, **memory_output}], relationship_anchor={})
    mem_count, _results = service.apply_memory_outputs(store, api_key, merged)
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output={"stage": "plaintext_add_memory_done"},
        memory_action_count=mem_count,
        identity_status="skipped",
        persona_ref="",
        persona_sha256="",
    )
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)


def _run_plaintext_update_identity_job(
    store,
    job_id: str,
    *,
    runtime,
    analysis_messages: list[dict] | None,
) -> None:
    if not identity_service._load_identity(store):
        service.mark_failed(store, job_id, "identity_not_initialized")
        return
    msgs = analysis_messages if isinstance(analysis_messages, list) else []
    language = history_import._import_language_for_store(store, msgs)
    identity_payload, warnings = history_import._derive_identity_with_provider(
        runtime,
        msgs,
        [],
        0,
        language,
    )
    provider_failure = _provider_identity_failure(warnings)
    if provider_failure:
        service.mark_failed(store, job_id, f"update_identity_failed:{provider_failure}")
        return
    status = service.replace_identity_preserving_anchor(store, {"identity": identity_payload})
    if status != "updated":
        service.mark_failed(store, job_id, status)
        return
    completed = db.genesis_complete_job(
        store.user_id,
        job_id,
        output={"stage": "plaintext_update_identity_done"},
        memory_action_count=0,
        identity_status="updated",
        persona_ref="",
        persona_sha256="",
    )
    if completed:
        service.write_genesis_state(store, completed, status=service.DONE_JOB_STATUS)


def _run_plaintext_genesis_job(
    store,
    api_key: str | None,
    job_id: str,
    *,
    mode: str = "onboarding",
    chunk_texts: list[str] | None = None,
    source_kind: str = history_import._HISTORY_SOURCE,
    source_groups: list[dict] | None = None,
    relationship_anchor: dict | None = None,
    analysis_messages: list[dict] | None = None,
) -> None:
    active_key = (store.user_id, job_id)
    try:
        if source_groups is None:
            source_groups = [{
                "source_kind": source_kind,
                "source_family": worker._source_family(source_kind),
                "chunk_texts": list(chunk_texts or []),
                "message_count": 0,
            }]
        source_groups = [
            group for group in source_groups
            if isinstance(group, dict) and group.get("chunk_texts")
        ]
        if not source_groups:
            raise ValueError("plaintext_import_empty")

        job = db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={"stage": "plaintext_reducer"},
            processed_chunks=0,
        )
        if job:
            service.write_genesis_state(store, job, status="processing")
        runtime = hosted_config_store._load_runtime_provider_config(store, api_key)
        if isinstance(runtime, tuple):
            _, err = runtime
            raise RuntimeError(json.dumps(err, ensure_ascii=False))

        if mode == "add_memory":
            _run_plaintext_add_memory_job(
                store,
                api_key,
                job_id,
                runtime=runtime,
                source_groups=source_groups,
            )
            return
        if mode == "update_identity":
            _run_plaintext_update_identity_job(
                store,
                job_id,
                runtime=runtime,
                analysis_messages=analysis_messages,
            )
            return

        # Genesis v2 (FEEDLING_GENESIS_V2_ENABLED): foreground-fast — greet on 3-5 core
        # + identity baseline, push the heavy reduce to background. Returns False when
        # the foreground yields nothing greetable, so we fall through to the v1 path.
        if worker.genesis_v2_enabled() and _run_plaintext_genesis_v2(
            store, api_key, job_id,
            runtime=runtime, source_groups=source_groups, relationship_anchor=relationship_anchor,
            analysis_messages=analysis_messages,
        ):
            return

        reducer_outputs: list[dict] = []
        existing_persona: dict = {}
        existing_voice: dict = {}
        processed_chunks = 0
        for idx, group in enumerate(source_groups, start=1):
            group_source_kind = str(group.get("source_kind") or history_import._HISTORY_SOURCE)
            group_source_family = str(group.get("source_family") or worker._source_family(group_source_kind))
            group_chunk_texts = [str(text) for text in (group.get("chunk_texts") or []) if str(text or "").strip()]
            if not group_chunk_texts:
                continue
            db.genesis_set_job_status(
                store.user_id,
                job_id,
                status="processing",
                output={
                    "stage": "plaintext_reducer",
                    "source_family": group_source_family,
                    "source_pass": idx,
                    "source_pass_total": len(source_groups),
                },
                processed_chunks=processed_chunks,
            )
            output = worker.build_reducer_output_from_texts(
                user_id=store.user_id,
                job_id=job_id,
                key_prefix=f"{job_id}:source_pass:{idx}:{group_source_family}",
                runtime=runtime,
                chunk_texts=group_chunk_texts,
                source_kind=group_source_kind,
                existing_persona=existing_persona,
                existing_voice=existing_voice,
            )
            reducer_outputs.append(output)
            processed_chunks += len(group_chunk_texts)
            next_persona = _plaintext_existing_persona_from_output(output)
            if next_persona:
                existing_persona = next_persona
            next_voice = _plaintext_existing_voice_from_output(output)
            if next_voice:
                existing_voice = next_voice

        reducer_output = _plaintext_merge_reducer_outputs(
            reducer_outputs,
            relationship_anchor=relationship_anchor,
        )
        db.genesis_set_job_status(
            store.user_id,
            job_id,
            status="processing",
            output={"stage": "plaintext_reducer_done"},
            processed_chunks=sum(len(group.get("chunk_texts") or []) for group in source_groups),
        )
        service.apply_reducer_output(store, api_key, job_id, reducer_output)
    except Exception as e:  # noqa: BLE001
        service.mark_failed(store, job_id, f"plaintext_import_failed:{type(e).__name__}:{str(e)[:220]}")
    finally:
        with _PLAINTEXT_ACTIVE_LOCK:
            _PLAINTEXT_ACTIVE_JOBS.discard(active_key)


def _start_plaintext_genesis_job(
    store,
    api_key: str | None,
    job: dict,
    *,
    mode: str = "onboarding",
    chunk_texts: list[str],
    source_kind: str,
    source_groups: list[dict] | None = None,
    relationship_anchor: dict | None = None,
    analysis_messages: list[dict] | None = None,
) -> bool:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return False
    active_key = (store.user_id, job_id)
    with _PLAINTEXT_ACTIVE_LOCK:
        if active_key in _PLAINTEXT_ACTIVE_JOBS:
            return False
        _PLAINTEXT_ACTIVE_JOBS.add(active_key)
    thread = threading.Thread(
        target=_run_plaintext_genesis_job,
        args=(store, api_key, job_id),
        kwargs={
            "mode": mode,
            "chunk_texts": chunk_texts,
            "source_kind": source_kind,
            "source_groups": source_groups,
            "relationship_anchor": relationship_anchor,
            "analysis_messages": analysis_messages,
        },
        name=f"genesis-plaintext-{job_id[:24]}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with _PLAINTEXT_ACTIVE_LOCK:
            _PLAINTEXT_ACTIVE_JOBS.discard(active_key)
        raise
    return True


@bp.route("/v1/genesis/imports", methods=["POST"])
def genesis_import_create():
    store = auth.require_user()
    payload = request.get_json(silent=True) or {}
    try:
        job, status = service.create_import_job(store, payload)
    except ValueError as e:
        return _bad(str(e), 400)
    return jsonify(_job_response(job, extra={"status": "created" if status == 201 else "exists"})), status


@bp.route("/v1/genesis/imports/plaintext", methods=["POST"])
def genesis_import_plaintext():
    store = auth.require_user()
    api_key = auth._extract_api_key()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _bad("json_object_required", 400)

    input_hash = history_import._history_import_payload_hash(payload)
    client_job_id = history_import._history_import_client_job_id(payload)
    mode = _plaintext_mode(payload, client_job_id=client_job_id)
    if mode == "update_identity" and not identity_service._load_identity(store):
        return _bad("identity_not_initialized", 409)
    existing = _find_reusable_plaintext_job(
        store,
        client_job_id=client_job_id,
        input_hash=input_hash,
        mode=mode,
    )
    if existing and str(existing.get("status") or "") == service.DONE_JOB_STATUS:
        return jsonify(_job_response(existing, extra={"status": "done"})), 200

    try:
        prepared = _prepare_plaintext_import(payload)
    except ValueError as e:
        return _bad(str(e), 400)

    if existing:
        existing = db.genesis_set_job_status(
            store.user_id,
            str(existing.get("job_id") or ""),
            status="processing",
            output={"stage": "plaintext_queued"},
            processed_chunks=0,
        ) or existing
        service.write_genesis_state(store, existing, status="processing")
        _start_plaintext_genesis_job(
            store,
            api_key,
            existing,
            mode=mode,
            chunk_texts=prepared["chunk_texts"],
            source_kind=prepared["source_kind"],
            source_groups=prepared["source_groups"],
            relationship_anchor=prepared["relationship_anchor"],
            analysis_messages=prepared["analysis_messages"],
        )
        return jsonify(_job_response(existing, extra={"status": "processing"})), 202

    metadata = _plaintext_job_metadata(
        payload,
        prepared,
        client_job_id=client_job_id,
        input_hash=input_hash,
        mode=mode,
    )
    total_bytes = sum(len(text.encode("utf-8")) for text in prepared["chunk_texts"])
    try:
        job, _status = service.create_import_job(store, {
            "source_kind": prepared["source_kind"],
            "file_manifest_hash": input_hash,
            "total_chunks": len(prepared["chunk_texts"]),
            "total_bytes": total_bytes,
            "metadata": metadata,
        })
    except ValueError as e:
        return _bad(str(e), 400)

    job = db.genesis_set_job_status(
        store.user_id,
        str(job.get("job_id") or ""),
        status="processing",
        output={"stage": "plaintext_queued"},
        processed_chunks=0,
    ) or job
    service.write_genesis_state(store, job, status="processing")
    _start_plaintext_genesis_job(
        store,
        api_key,
        job,
        mode=mode,
        chunk_texts=prepared["chunk_texts"],
        source_kind=prepared["source_kind"],
        source_groups=prepared["source_groups"],
        relationship_anchor=prepared["relationship_anchor"],
        analysis_messages=prepared["analysis_messages"],
    )
    return jsonify(_job_response(job, extra={"status": "processing"})), 202


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
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    envelope_meta = payload.get("envelope_meta") if isinstance(payload.get("envelope_meta"), dict) else envelope
    body_ct = str(payload.get("ciphertext_b64") or envelope.get("body_ct") or "")
    raw = service.b64decode_required(body_ct)
    payload = {**payload, "envelope_meta": envelope_meta}
    return raw, payload


def _binary_chunk_payload() -> tuple[bytes, dict[str, Any]]:
    raw = request.get_data(cache=False) or b""
    envelope_meta_raw = request.headers.get("X-Envelope-Meta") or request.args.get("envelope_meta") or ""
    envelope_meta = {}
    if envelope_meta_raw:
        try:
            envelope_meta = json.loads(envelope_meta_raw)
        except Exception as e:  # noqa: BLE001
            raise ValueError("invalid_envelope_meta_json") from e
        if not isinstance(envelope_meta, dict):
            raise ValueError("invalid_envelope_meta_json")
    meta = {
        "byte_start": request.headers.get("X-Byte-Start") or request.args.get("byte_start"),
        "byte_end": request.headers.get("X-Byte-End") or request.args.get("byte_end"),
        "content_sha256": request.headers.get("X-Content-SHA256") or request.args.get("content_sha256"),
        "ciphertext_sha256": request.headers.get("X-Ciphertext-SHA256") or request.args.get("ciphertext_sha256"),
        "envelope_meta": envelope_meta,
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
            envelope_meta=meta.get("envelope_meta") if isinstance(meta.get("envelope_meta"), dict) else None,
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
        except ValueError as e:
            return _bad(str(e), 400)
        except Exception as e:  # noqa: BLE001
            failed = service.mark_failed(store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}")
            return jsonify(_job_response(failed or job, extra={"status": "failed", "error": str(e)[:240]})), 500

    return jsonify(_job_response(job, extra={"status": "uploaded"})), 202


@bp.route("/v1/genesis/imports/<job_id>/outputs", methods=["POST"])
def genesis_import_apply_outputs(job_id: str):
    store = auth.require_user()
    runtime_auth.authorize_scope("genesis")
    api_key = auth._extract_api_key()
    runtime_token = runtime_auth.extract_runtime_token() or ""
    if not _valid_job_id(job_id):
        return _bad("invalid_job_id", 400)
    payload = request.get_json(silent=True) or {}
    reducer_output = payload.get("reducer_output") if isinstance(payload.get("reducer_output"), dict) else payload
    if not isinstance(reducer_output, dict):
        return _bad("reducer_output_required", 400)
    try:
        applied = service.apply_reducer_output(
            store,
            api_key,
            job_id,
            reducer_output,
            runtime_token=runtime_token,
        )
    except LookupError as e:
        return _bad(str(e), 404)
    except ValueError as e:
        return _bad(str(e), 400)
    except Exception as e:  # noqa: BLE001
        import debug_trace
        debug_trace.trace_event(
            store, subsystem="genesis", type="genesis.outputs.applied", actor="backend",
            job_id=job_id, status="failed", summary="apply failed",
            detail={"reason": f"{type(e).__name__}:{str(e)[:80]}"})
        failed = service.mark_failed(store, job_id, f"apply_outputs_failed:{type(e).__name__}:{str(e)[:180]}")
        return jsonify(_job_response(failed, extra={"status": "failed", "error": str(e)[:240]})), 500
    job = db.genesis_get_job(store.user_id, job_id)
    import debug_trace
    _a = applied if isinstance(applied, dict) else {}
    debug_trace.trace_event(
        store, subsystem="genesis", type="genesis.outputs.applied", actor="backend",
        job_id=job_id, summary="genesis outputs applied",
        detail={
            "source_kind": str((job or {}).get("source_kind") or ""),
            "memory_action_count": _a.get("memory_action_count"),
            "identity_status": str(_a.get("identity_status") or ""),
            "persona_ref": str(_a.get("persona_ref") or ""),
        },
    )
    return jsonify(_job_response(job, extra={"status": "done", "applied": applied})), 200


@bp.route("/v1/genesis/persona_backfill", methods=["POST"])
def genesis_persona_backfill():
    """Cutover gate 4 B: backfill the persona/voice blob for a pre-genesis host user
    from their existing identity record (NOT a transcript). Decrypts identity (auth =
    api_key or runtime token), then run_persona_backfill assembles the material and
    submits ONE genesis import job (source_kind=companion_persona_backfill → worker →
    persona_build → genesis_persona blob). Idempotent + signal-gated inside
    run_persona_backfill (no signal → no_signal; already in-flight → the existing job).
    Triggered by the cutover batch and the supervisor lazy path."""
    store = auth.require_user()
    runtime_auth.authorize_scope("genesis")
    api_key = auth._extract_api_key()
    runtime_token = request.headers.get("X-Feedling-Runtime-Token", "")
    from identity import actions as identity_actions
    from genesis import persona_backfill
    identity_plain, err = identity_actions._identity_plain_for_action(
        store, api_key, runtime_token=runtime_token)
    if identity_plain is None:
        return _bad(err or "identity_unavailable", 409)
    try:
        job = persona_backfill.run_persona_backfill(store, identity_plain)
    except Exception as e:  # noqa: BLE001
        return _bad(f"persona_backfill_failed:{type(e).__name__}:{str(e)[:160]}", 500)
    if job is None:
        return jsonify({"status": "no_signal"}), 200  # nothing to backfill; Dream grows it
    return jsonify({
        "status": "enqueued",
        "job_id": job.get("job_id"),
        "job_status": job.get("status"),
    }), 202


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

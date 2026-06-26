"""Memory-capture lane job substrate.

Capture jobs reuse the existing proactive job log/wake/claim primitives, but
they are not proactive reach-out wakes. They must never be gated by ambient /
scheduled / delivery controls.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

from core import util
from core.store import UserStore

CAPTURE_JOB_KIND_MEMORY = "memory_capture"
CAPTURE_JOB_SOURCE = "memory_capture"
CAPTURE_JOB_ID_PREFIX = "cap"
CAPTURE_JOB_KIND_DREAM = "memory_dream"
DREAM_JOB_SOURCE = "memory_dream"
DREAM_JOB_ID_PREFIX = "dream"
CAPTURE_ACTIVE_STATUSES = frozenset({"pending", "claimed", "realizing"})


def is_memory_capture_job(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    return (
        str(job.get("job_kind") or "").strip() == CAPTURE_JOB_KIND_MEMORY
        or str(job.get("source") or "").strip() == CAPTURE_JOB_SOURCE
    )


def is_memory_dream_job(job: Mapping[str, Any] | None) -> bool:
    if not isinstance(job, Mapping):
        return False
    return (
        str(job.get("job_kind") or "").strip() == CAPTURE_JOB_KIND_DREAM
        or str(job.get("source") or "").strip() == DREAM_JOB_SOURCE
    )


def is_memory_maintenance_job(job: Mapping[str, Any] | None) -> bool:
    return is_memory_capture_job(job) or is_memory_dream_job(job)


def _safe_window(window: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = window if isinstance(window, Mapping) else {}
    try:
        until_ts = float(raw.get("until_ts") or 0)
    except (TypeError, ValueError):
        until_ts = 0.0
    try:
        message_count = int(raw.get("message_count") or 0)
    except (TypeError, ValueError):
        message_count = 0
    return {
        "after_message_id": str(raw.get("after_message_id") or "")[:160],
        "until_message_id": str(raw.get("until_message_id") or "")[:160],
        "until_ts": until_ts,
        "message_count": max(0, message_count),
    }


def _active_capture_job(job: Mapping[str, Any]) -> bool:
    return is_memory_capture_job(job) and str(job.get("status") or "pending").strip().lower() in CAPTURE_ACTIVE_STATUSES


def _active_dream_job(job: Mapping[str, Any]) -> bool:
    return is_memory_dream_job(job) and str(job.get("status") or "pending").strip().lower() in CAPTURE_ACTIVE_STATUSES


def _find_capture_by_key(store: UserStore, capture_key: str) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if is_memory_capture_job(job) and str(job.get("capture_key") or "") == capture_key:
            return dict(job)
    return None


def _find_dream_by_key(store: UserStore, dream_key: str) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if is_memory_dream_job(job) and str(job.get("dream_key") or "") == dream_key:
            return dict(job)
    return None


def _find_active_capture(store: UserStore) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if _active_capture_job(job):
            return dict(job)
    return None


def _find_active_dream(store: UserStore) -> dict | None:
    for job in store.list_proactive_jobs(since_epoch=0, limit=0):
        if _active_dream_job(job):
            return dict(job)
    return None


def make_memory_capture_job(
    *,
    trigger: str,
    capture_key: str,
    window: Mapping[str, Any] | None,
    not_before: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    not_before_ts = now_ts if not_before is None else float(not_before)
    return {
        "job_id": util._new_public_id(CAPTURE_JOB_ID_PREFIX),
        "job_kind": CAPTURE_JOB_KIND_MEMORY,
        "source": CAPTURE_JOB_SOURCE,
        "status": "pending",
        "trigger": str(trigger or "session_break")[:120],
        "capture_key": str(capture_key or "")[:240],
        "window": _safe_window(window),
        "not_before": not_before_ts,
        "ts": now_ts,
        "created_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def make_memory_dream_job(
    *,
    trigger: str,
    dream_key: str,
    dream_until: Mapping[str, Any] | None = None,
    dream_stats: Mapping[str, Any] | None = None,
    not_before: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time() if now is None else float(now)
    not_before_ts = now_ts if not_before is None else float(not_before)
    return {
        "job_id": util._new_public_id(DREAM_JOB_ID_PREFIX),
        "job_kind": CAPTURE_JOB_KIND_DREAM,
        "source": DREAM_JOB_SOURCE,
        "status": "pending",
        "trigger": str(trigger or "nightly_dream")[:120],
        "dream_key": str(dream_key or "")[:240],
        "dream_until": dict(dream_until or {}),
        "dream_stats": dict(dream_stats or {}),
        "not_before": not_before_ts,
        "ts": now_ts,
        "created_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def enqueue_memory_capture_job(
    store: UserStore,
    *,
    trigger: str,
    capture_key: str,
    window: Mapping[str, Any] | None,
    not_before: float | None = None,
    now: float | None = None,
) -> tuple[dict | None, bool, str]:
    """Enqueue one memory-capture job if no equivalent/active job exists.

    Returns (job, enqueued, reason). Existing jobs are returned for idempotency
    or single-flight visibility, but are not appended again.
    """
    key = str(capture_key or "").strip()
    if not key:
        return None, False, "capture_key_required"
    existing_same_key = _find_capture_by_key(store, key)
    if existing_same_key is not None:
        return existing_same_key, False, "duplicate_capture_key"
    active = _find_active_capture(store)
    if active is not None:
        return active, False, "capture_already_pending"
    job = make_memory_capture_job(
        trigger=trigger,
        capture_key=key,
        window=window,
        not_before=not_before,
        now=now,
    )
    return store.append_proactive_job(job), True, "enqueued"


def enqueue_memory_dream_job(
    store: UserStore,
    *,
    trigger: str,
    dream_key: str,
    dream_until: Mapping[str, Any] | None = None,
    dream_stats: Mapping[str, Any] | None = None,
    not_before: float | None = None,
    now: float | None = None,
) -> tuple[dict | None, bool, str]:
    """Enqueue one memory-dream job if no equivalent/active dream job exists."""
    key = str(dream_key or "").strip()
    if not key:
        return None, False, "dream_key_required"
    existing_same_key = _find_dream_by_key(store, key)
    if existing_same_key is not None:
        return existing_same_key, False, "duplicate_dream_key"
    active = _find_active_dream(store)
    if active is not None:
        return active, False, "dream_already_pending"
    job = make_memory_dream_job(
        trigger=trigger,
        dream_key=key,
        dream_until=dream_until,
        dream_stats=dream_stats,
        not_before=not_before,
        now=now,
    )
    return store.append_proactive_job(job), True, "enqueued"

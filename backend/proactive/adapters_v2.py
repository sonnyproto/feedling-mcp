"""Adapters from the legacy proactive job shape into V2 wake events."""
from __future__ import annotations

import time
from typing import Any, Mapping

from proactive.runtime_v2 import WakeEventV2


def _source_for_legacy_trigger(trigger: str, *, manual: bool) -> str:
    normalized = str(trigger or "").strip().lower()
    if manual:
        return "user_message"
    if normalized.startswith("heartbeat"):
        return "heartbeat"
    if normalized.startswith("perception_"):
        return "perception_event"
    if normalized in {"scene_change", "screen_tick", "broadcast_opened", "heartbeat_broadcast_on"}:
        return "scene_change"
    if normalized.startswith("scheduled"):
        return "scheduled_wake"
    if normalized == "background_result":
        return "background_result"
    return "perception_event"


def wake_event_v2_from_legacy_job(
    user_id: str,
    job: Mapping[str, Any],
    *,
    now: float | None = None,
) -> WakeEventV2:
    """Convert one current proactive job into the new wake envelope.

    This deliberately preserves the legacy job as payload. The adapter is a
    migration boundary; new runtime code should not reach back into
    proactive_jobs for strategy.
    """
    now = time.time() if now is None else float(now)
    trigger = str(job.get("trigger") or job.get("wake_kind") or "legacy_job")
    manual = bool(job.get("manual"))
    source = _source_for_legacy_trigger(trigger, manual=manual)
    return WakeEventV2(
        user_id=user_id,
        wake_id=str(job.get("wake_id") or job.get("job_id") or ""),
        source=source,
        trigger=trigger,
        created_at=float(job.get("ts") or now),
        latency_sensitive=manual,
        manual=manual,
        change_digest=str(job.get("change_digest") or job.get("context_hint") or ""),
        presence_hints=dict(job.get("presence_hints") or {}),
        timezone=str(job.get("timezone") or ""),
        scheduled_note=str(job.get("scheduled_note") or ""),
        origin_refs=tuple(str(x) for x in (job.get("origin_refs") or []) if str(x)),
        background_payload=job.get("background_payload") if isinstance(job.get("background_payload"), dict) else {},
        payload={"legacy_proactive_job": dict(job)},
    )

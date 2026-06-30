"""Genesis v2 Step 3 — foreground reducer DECISION logic (pure, unit-testable).

The foreground's only job is the "open the door" set, NOT a small full genesis
(Codex). This module holds the *decisions* — which candidates become core, how the
greeting material is assembled, how the checkpoint is marked FOREGROUND_READY — as
pure functions, separate from the LLM/decrypt/API-write plumbing the worker owns.

Holds Codex's Step-3 constraints in code:
  - core memory is CAPPED at 3-5, by priority, never padded;
  - foreground does NOT touch full voice (that's background);
  - core writes go through the stable candidate_id and are marked foreground_written
    so background skips them (dedup anchor lives in checkpoint.py).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from genesis import checkpoint

FOREGROUND_CORE_MAX = 5  # cap, not a target — fewer is fine when signal is thin

# Codex priority order (lower rank = picked first). Buckets are the A9 canonical set.
_BUCKET_PRIORITY = {
    "我们的关系": 0, "Our relationship": 0,
    "宠物": 1, "Pets": 1, "家庭": 1, "Family": 1, "朋友": 1, "Friends": 1,
    "偏好与边界": 2, "Preferences & boundaries": 2,
    "健康": 3, "Health": 3, "个性与价值观": 3, "Personality & values": 3,
    "目标与成长": 4, "Goals & growth": 4,
}
_DEFAULT_PRIORITY = 5


def candidate_priority(bucket: Any) -> int:
    return _BUCKET_PRIORITY.get(str(bucket or "").strip(), _DEFAULT_PRIORITY)


def _text(c: Mapping[str, Any]) -> str:
    return str(c.get("summary") or c.get("content") or "").strip()


def is_low_signal(c: Mapping[str, Any], *, min_len: int = 4) -> bool:
    """Exclude one-off / low-confidence / too-short / emotion-only-no-long-term.
    min_len is in code points and low (Chinese facts are dense: 怕香菜 is real) — it
    only catches degenerate 1-3 char junk; the importance/priority rule does the rest."""
    if not isinstance(c, Mapping):
        return True
    if len(_text(c)) < min_len:                       # too short / no context
        return True
    importance = float(c.get("importance") or 0.0)
    # low importance AND no priority bucket = not durable enough for the door set
    if importance < 0.2 and candidate_priority(c.get("bucket")) >= _DEFAULT_PRIORITY:
        return True
    return False


def select_core_for_foreground(
    candidates: Sequence[Mapping[str, Any]], *, max_n: int = FOREGROUND_CORE_MAX,
) -> list[dict]:
    """Pick up to `max_n` high-signal core memories for the open-the-door set.

    Priority: relationship > pet/family/friend > preference/boundary >
    health/values > goals; then importance desc. Excludes low-signal. NEVER pads —
    `max_n` is a cap; returns fewer (even 0-1) when signal is thin (Codex)."""
    eligible = [dict(c) for c in (candidates or []) if isinstance(c, Mapping) and not is_low_signal(c)]
    eligible.sort(key=lambda c: (candidate_priority(c.get("bucket")), -float(c.get("importance") or 0.0)))
    return eligible[: max(1, int(max_n))] if eligible else []


def build_greeting_material(
    *, identity_baseline: Mapping[str, Any] | None, core_memories: Sequence[Mapping[str, Any]] | None,
) -> dict:
    """Assemble greeting material from the baseline + core — NO extra heavy LLM chain
    (Codex: greeting must not run its own reducer). Greeting fires at FOREGROUND_READY."""
    ident = identity_baseline or {}
    facts: list[str] = []
    for c in list(core_memories or [])[:3]:           # 1-3 high-signal facts
        s = _text(c)
        if s:
            facts.append(s[:120])
    return {
        "agent_name": str(ident.get("agent_name") or "").strip(),
        "relationship_anchor": str(ident.get("relationship_anchor_evidence") or "").strip(),
        "signal_facts": facts,
        "persona_baseline": str(ident.get("persona_baseline") or ident.get("category") or "").strip(),
    }


def mark_foreground_core_written(
    cp: Mapping[str, Any] | None, written: Sequence[Mapping[str, Any]], *, now: float | None = None,
) -> dict:
    """Record the foreground core writes (DONE + foreground_written) and flip to
    FOREGROUND_READY so the greeting may fire. `written` items = {candidate_id,
    source_ref, memory_id}. Background then skips these via the dedup anchor."""
    out = dict(cp) if isinstance(cp, Mapping) else checkpoint.new_checkpoint(now=now)
    for w in (written or []):
        if not isinstance(w, Mapping) or not str(w.get("candidate_id") or "").strip():
            continue
        out = checkpoint.upsert_task(
            out, task_id="fact_write", chunk_id=str(w["candidate_id"]),
            status=checkpoint.TASK_DONE, source_ref=str(w.get("source_ref") or ""),
            candidate_id=str(w["candidate_id"]),
            written_memory_ids=[str(w["memory_id"])] if w.get("memory_id") else [],
            foreground_written=True, now=now,
        )
    return checkpoint.set_phase(out, checkpoint.PHASE_FOREGROUND_READY, now=now)

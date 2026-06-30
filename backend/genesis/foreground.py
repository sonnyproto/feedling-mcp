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


def candidate_priority(c: Mapping[str, Any]) -> int:
    """Priority rank (lower = picked first). Works on BOTH shapes the pipeline has:
    the pre-fact-write fact_candidate `{about, summary, evidence}` (use `about`) and
    the post-fact-write memory `{bucket, importance, ...}` (use `bucket`)."""
    if not isinstance(c, Mapping):
        return _DEFAULT_PRIORITY
    about = str(c.get("about") or "").strip().lower()
    bucket = str(c.get("bucket") or "").strip()
    if about == "relationship" or bucket in ("我们的关系", "Our relationship"):
        return 0  # relationship anchor = greeting gold
    return _BUCKET_PRIORITY.get(bucket, _DEFAULT_PRIORITY)


def _text(c: Mapping[str, Any]) -> str:
    return str(c.get("summary") or c.get("content") or "").strip()


def is_low_signal(c: Mapping[str, Any], *, min_len: int = 4) -> bool:
    """Exclude only degenerate candidates (too short / no real text). Lenient by
    design (Codex: foreground should not hard-gate) — ranking, not exclusion, does
    the prioritising. min_len is low in code points (Chinese facts are dense)."""
    return not isinstance(c, Mapping) or len(_text(c)) < min_len


def select_core_for_foreground(
    candidates: Sequence[Mapping[str, Any]], *, max_n: int = FOREGROUND_CORE_MAX,
) -> list[dict]:
    """Pick up to `max_n` high-signal core for the open-the-door set, from the RAW
    fact_candidates (`{about, summary, evidence}`) — i.e. select BEFORE the heavy
    full fact_write, then fact_write only these (Codex flow).

    Rank: relationship > pet/family/friend > preference/boundary > health/values >
    goals; then grounded (has evidence) > importance (if present) > longer summary.
    Excludes only degenerate. NEVER pads — `max_n` is a cap, returns fewer when thin."""
    def _key(c: Mapping[str, Any]):
        has_evidence = 1 if str(c.get("evidence") or "").strip() else 0
        importance = float(c.get("importance") or 0.0)
        return (candidate_priority(c), -has_evidence, -importance, -min(len(_text(c)), 200))

    eligible = [dict(c) for c in (candidates or []) if isinstance(c, Mapping) and not is_low_signal(c)]
    eligible.sort(key=_key)
    return eligible[: max(1, int(max_n))] if eligible else []


def core_skip_texts(core_candidates: Sequence[Mapping[str, Any]] | None) -> set[str]:
    """Normalized fact-text set for the foreground core, for the background reduce to
    skip (worker.build_reducer_output_from_texts(skip_fact_texts=...)). Same
    normalization both sides use, so the background's SAME cached candidates match
    exactly and never get re-written (structural dedup, Codex #1)."""
    out: set[str] = set()
    for c in (core_candidates or []):
        if isinstance(c, Mapping):
            t = checkpoint.normalize_fact_text(_text(c))
            if t:
                out.add(t)
    return out


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

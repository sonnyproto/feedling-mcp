"""Single source of truth for what a valid IO identity card must satisfy.

Imported by BOTH the backend write paths (init / replace / profile_patch /
dimension_nudge) AND io_cli's local pre-validation, so the two never drift.
Therefore this module MUST stay pure stdlib — io_cli runs standalone on a VPS
and cannot pull backend DB deps.

Contract = B (evidence-first, sparse-allowed): we validate STRUCTURE only.
We do NOT require exactly 7 dimensions and we do NOT reject clustered /
low-spread / sparse cards — those are quality nudges owned by the prompt, not
gates. Blocking on them would hurt onboarding success rate.
"""
from __future__ import annotations

# Single source of truth. backend/identity/service.py imports this.
RUNTIME_LABELS: frozenset[str] = frozenset({
    "io", "feedling", "p0", "p-zero",
    "hermes", "claude", "claude code", "claude desktop", "claude-code", "claude-desktop",
    "claude.ai", "anthropic", "openclaw", "open-claw", "open claw", "cursor",
    "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o", "gpt-5", "openai", "openrouter",
    "gemini", "google ai", "google", "bard", "deepseek", "minimax", "copilot", "github copilot",
    "agent", "assistant", "ai", "bot",
})

MAX_DIMENSIONS = 12  # sanity cap, NOT a floor
_VALUE_MIN, _VALUE_MAX = 0, 100
_OK: tuple[bool, str] = (True, "")


def is_runtime_label(name: str) -> bool:
    return str(name or "").strip().lower() in RUNTIME_LABELS


def validate_dimensions_structure(dims) -> tuple[bool, str]:
    if not isinstance(dims, list):
        return (False, "dimensions_must_be_list")
    if len(dims) > MAX_DIMENSIONS:
        return (False, "too_many_dimensions")
    seen: set[str] = set()
    for d in dims:
        if not isinstance(d, dict):
            return (False, "dimension_must_be_object")
        name = str(d.get("name") or "").strip()
        if not name:
            return (False, "dimension_name_empty")
        key = name.lower()
        if key in seen:
            return (False, "dimension_name_duplicate")
        seen.add(key)
        value = d.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (False, "dimension_value_not_number")
        if value < _VALUE_MIN or value > _VALUE_MAX:
            return (False, "dimension_value_out_of_range")
    return _OK


def validate_full_identity_card(card: dict) -> tuple[bool, str]:
    """init / full replace. Structure only (contract B — no count/spread floor)."""
    if not isinstance(card, dict):
        return (False, "identity_must_be_object")
    # agent_name MAY be empty here: contract B / 优先 onboarding 成功率 — do NOT
    # block onboarding on a missing name. The agent should supply a default name
    # (Batch 1 guardrail), but that is guidance, not a gate. A NON-empty name
    # still cannot be a runtime label (e.g. "Claude").
    name = str(card.get("agent_name") or "").strip()
    if name and is_runtime_label(name):
        return (False, "agent_name_is_runtime_label")
    return validate_dimensions_structure(card.get("dimensions", []))


def validate_profile_patch(patch: dict) -> tuple[bool, str]:
    """Only validate fields PRESENT in the patch — never judge the whole card,
    so a name change is not rejected because the old card is sparse."""
    if not isinstance(patch, dict):
        return (False, "patch_must_be_object")
    if "agent_name" in patch:
        name = str(patch.get("agent_name") or "").strip()
        if not name:
            return (False, "agent_name_empty")
        if is_runtime_label(name):
            return (False, "agent_name_is_runtime_label")
    if "dimensions" in patch:
        return validate_dimensions_structure(patch.get("dimensions"))
    return _OK


def validate_dimension_nudge(target_name: str, new_value) -> tuple[bool, str]:
    if not str(target_name or "").strip():
        return (False, "dimension_name_empty")
    if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
        return (False, "dimension_value_not_number")
    if new_value < _VALUE_MIN or new_value > _VALUE_MAX:
        return (False, "dimension_value_out_of_range")
    return _OK

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
    "hermes", "claude", "claude code", "claude desktop", "claude-code",
    "claude-desktop", "claude.ai", "anthropic", "openclaw", "open-claw",
    "open claw", "cursor", "chatgpt", "chat-gpt", "gpt", "gpt-4", "gpt-4o",
    "gpt-5", "openai", "openrouter", "gemini", "assistant", "ai", "bot",
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

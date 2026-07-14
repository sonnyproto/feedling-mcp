"""Deterministic, sanitized profile rows for interrupted diagnostic workers.

The headless qualification agent normally authors a semantic ``profileResult``.
Local diagnostic runs still need a complete selected matrix when that agent
times out, exits non-zero, or emits malformed evidence.  This module builds the
smallest honest fallback row: it preserves only locked profile metadata and
non-secret provisioning facts, marks all behavioral evidence unavailable, and
can never qualify a release.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


class DiagnosticResultError(RuntimeError):
    """A fixed diagnostic fallback contract failure."""


_PROFILE_METADATA: dict[str, tuple[str, str, str]] = {
    "official-deepseek": ("official", "deepseek", "deepseek"),
    "official-anthropic": ("official", "claude", "anthropic"),
    "official-openai": ("official", "openai", "openai"),
    "official-gemini": ("official", "gemini", "gemini"),
    "openrouter-claude": ("openrouter", "claude", "openrouter"),
    "openrouter-openai": ("openrouter", "openai", "openrouter"),
    "openrouter-glm": ("openrouter", "glm", "openrouter"),
    "relay-kongbeiqie": ("relay", "claude", "openai_compatible"),
}
_TRACE_STAGES = ("routing", "queue", "provider", "persistence", "delivery")
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ALLOWED_OBSERVED_RUNTIMES = frozenset(("hosted_resident", "resident_cli"))


def _safe_model(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        return "unavailable"
    if any(
        character.isspace() or not character.isprintable() for character in value
    ):
        return "unavailable"
    return value


def _safe_user_id(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_USER_ID_RE.fullmatch(value):
        return value
    return None


def agent_error_profile(
    manifest_profile: Mapping[str, Any],
    *,
    profile_id: str,
    expected_runtime: str,
) -> dict[str, Any]:
    """Build one schema-compatible, evidence-negative ``AGENT_ERROR`` row."""

    metadata = _PROFILE_METADATA.get(profile_id)
    if metadata is None or manifest_profile.get("profile_id") != profile_id:
        raise DiagnosticResultError("diagnostic fallback profile is invalid")
    if expected_runtime != "hosted_resident":
        raise DiagnosticResultError("diagnostic fallback runtime is invalid")

    route_family, model_family, provider = metadata
    runtime = manifest_profile.get("runtime_mode")
    observed_runtime = runtime if runtime in _ALLOWED_OBSERVED_RUNTIMES else None
    trace_enabled = manifest_profile.get("trace_enabled") is True

    return {
        "profile_id": profile_id,
        "route_family": route_family,
        "model_family": model_family,
        "provider": provider,
        "model": _safe_model(manifest_profile.get("configured_model")),
        "reasoning_effort": "medium",
        "user_id": _safe_user_id(manifest_profile.get("user_id")),
        "expected_runtime": expected_runtime,
        "observed_runtime": observed_runtime,
        "status": "AGENT_ERROR",
        "scenarios": [],
        "turns": [],
        "latency": {
            "sample_count": 0,
            "ack_p50_ms": None,
            "reply_p50_ms": None,
            "reply_p95_ms": None,
            "stage_p50_ms": {stage: None for stage in _TRACE_STAGES},
            "missing_stages": list(_TRACE_STAGES),
        },
        "reasoning": {
            "expected": True,
            "capability_enabled": False,
            "requested_effort": "medium",
            "configured_effort": "medium",
            "effective_effort": "unknown",
            "reasoning_event_count": 0,
            "metadata_present": False,
            "token_metadata_present": False,
            "user_visible_disclosure_present": False,
            "request_id": "unavailable",
            "turn_id": "unavailable",
            "trace_id": "unavailable",
            "kind": None,
            "source": None,
            "model": None,
            "reasoning_token_count": None,
            "disclosure_length": None,
            "raw_private_reasoning_stored": False,
        },
        "trace": {
            "enabled": trace_enabled,
            "deploy_enabled": trace_enabled,
            "correlated_event_count": 0,
            "observed_event_types": [],
            "missing_required_event_types": list(_TRACE_STAGES),
            "raw_trace_stored": False,
        },
        "cleanup": {
            "attempted": False,
            "provider_config_deleted": False,
            "account_reset": False,
            "old_credential_rejected": False,
            "status": "AGENT_ERROR",
        },
        "diagnostic_codes": [
            "AGENT_EXECUTION_ERROR",
            "TRACE_PARTIAL",
            "STAGE_TIMING_UNAVAILABLE",
        ],
        "redaction": {
            "provider_keys_omitted": True,
            "feedling_api_keys_omitted": True,
            "content_private_keys_omitted": True,
            "raw_chat_omitted": True,
            "raw_trace_omitted": True,
            "raw_reasoning_omitted": True,
            "synthetic_users_only": True,
            "prompt_injection_detected": False,
        },
    }

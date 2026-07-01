"""Genesis v2 foreground — reliable Identity Card derivation (B-first, orchestration-only).

The foreground-ready contract (matching the legacy chat_ready) needs a non-empty
Identity Card BEFORE the job is marked done. Identity DERIVATION ITSELF IS UNCHANGED:
we reuse the proven, history-only-validated hosted deriver
(history_import._derive_identity_with_provider, AI-persona-primary). This wrapper only
adapts genesis-shaped inputs and adds a small retry-on-empty so one transient/empty
response doesn't collapse to the guard fallback. No new identity prompt/logic — this is
a calling-order (architecture) change, not a behavior change.
"""
from __future__ import annotations

import provider_client
from hosted import history_import


def has_identity_signal(identity: dict | None) -> bool:
    """A usable Identity Card = a name OR at least one real dimension."""
    if not isinstance(identity, dict):
        return False
    if str(identity.get("agent_name") or "").strip():
        return True
    dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    return any(isinstance(d, dict) for d in dims)


def derive_foreground_identity(
    *,
    runtime: provider_client.ProviderConfig,
    analysis_messages: list[dict],
    core_memories: list[dict],
    days_with_user: int,
    language: str = "zh",
    max_attempts: int = 2,
) -> tuple[dict, list[str]]:
    """Derive the foreground Identity Card by calling the EXISTING hosted deriver
    unchanged. Returns (identity_payload, warnings). Retries only when the result has no
    identity signal (transient/empty), up to max_attempts. Empty name + empty dims on
    return means the sources truly carry no identity — the caller must NOT mark the job
    done (that's the 'ask a minimal seed' branch, never a fake-complete)."""
    identity: dict = {}
    warnings: list[str] = []
    for _ in range(max(1, int(max_attempts))):
        identity, warnings = history_import._derive_identity_with_provider(
            runtime, analysis_messages, core_memories, days_with_user, language,
        )
        if has_identity_signal(identity):
            break
    return identity, warnings

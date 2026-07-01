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


def _provider_failed(warnings: list[str]) -> bool:
    """The deriver swallows provider errors and returns a generic fallback, tagging
    `provider_identity_failed:...` in warnings. That's a TRANSIENT failure worth
    retrying (vs `identity_guard_no_ai_source...`, a legit 'no signal' we don't spin on)."""
    return any("provider_identity_failed" in str(w) for w in (warnings or []))


def derive_foreground_identity(
    *,
    runtime: provider_client.ProviderConfig,
    analysis_messages: list[dict],
    core_memories: list[dict],
    days_with_user: int,
    language: str = "zh",
    max_attempts: int = 3,
) -> tuple[dict, list[str]]:
    """Derive the foreground Identity Card by calling the EXISTING hosted deriver
    unchanged (orchestration only). Identity is the foreground-ready GATE, so this
    RETRIES (capped) on a transient provider failure OR an empty result — a single
    502/timeout must not collapse the whole onboarding to a blank home. Returns
    (identity_payload, warnings). A genuinely empty result after retries (no AI source /
    no signal) means the caller must NOT mark done (the minimal-seed branch, never a
    fake-complete). NOTE: the deriver hides the status code, so 402/quota also gets a few
    (capped) retries — the cap bounds the waste; refine if we surface the code later."""
    identity: dict = {}
    warnings: list[str] = []
    for _ in range(max(1, int(max_attempts))):
        identity, warnings = history_import._derive_identity_with_provider(
            runtime, analysis_messages, core_memories, days_with_user, language,
        )
        # good result = real signal AND the provider actually answered (not a fallback)
        if has_identity_signal(identity) and not _provider_failed(warnings):
            break
    return identity, warnings

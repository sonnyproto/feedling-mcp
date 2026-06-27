"""Voice/persona backfill for pre-genesis host users (cutover gate 4).

Old host users (onboarded before genesis) have an identity record but no
``genesis_persona`` blob, so after the agent_runtime cutover they boot generic and
their route-B persona signals (``tone_style`` / ``custom_persona_prompt`` /
``self_introduction``) become orphaned (route-B retiring, nothing reads them).

Backfill reuses the genesis production line: assemble a persona material from the
identity record, submit it as ONE chunk under source_kind
``companion_persona_backfill`` (routes to the ``ai_persona`` family →
``persona_build`` → ``genesis_persona`` blob). NOT a transcript, NOT memory/capture.

This module holds only the PURE pieces (assembly / hash / idempotency / signal
check) so they unit-test without DB or enclave. Job creation + the enqueue
trigger + supervisor pickup live in their callers (cutover gate 4 B/C).
"""
from __future__ import annotations

import hashlib

# Identity fields that carry persona/voice signal worth backfilling. Order =
# priority: the user-authored directive first, then derived tone, then self-intro.
_SIGNAL_FIELDS = ("custom_persona_prompt", "tone_style", "self_introduction")

_SOURCE_KIND = "companion_persona_backfill"
_KEY_VERSION = "v1"


def _clean(value) -> str:
    return str(value or "").strip()


def has_persona_signal(identity: dict | None) -> bool:
    """True when the identity record carries any persona/voice signal to backfill.

    No signal → caller leaves the persona empty (Dream grows it from real chat);
    do NOT enqueue a backfill that would only produce a hollow persona.
    """
    if not isinstance(identity, dict):
        return False
    return any(_clean(identity.get(f)) for f in _SIGNAL_FIELDS)


def assemble_persona_material(identity: dict | None) -> str:
    """Assemble a persona-build material from the identity record (plaintext in,
    plaintext out — the caller decrypts identity and encrypts the resulting chunk).

    Shaped to read like an uploaded AI-persona/system-prompt so persona_build (§7.B)
    treats it as the persona spec: the user-authored ``custom_persona_prompt`` is the
    backbone, ``tone_style`` the voice, ``self_introduction`` supplementary. Empty
    fields are dropped, never invented (grounding).
    """
    if not isinstance(identity, dict):
        return ""
    agent_name = _clean(identity.get("agent_name"))
    custom = _clean(identity.get("custom_persona_prompt"))
    tone = _clean(identity.get("tone_style"))
    intro = _clean(identity.get("self_introduction"))

    lines: list[str] = []
    if agent_name:
        lines.append(f"Name: {agent_name}")
    if custom:
        lines.append("Persona directive (the user's own, highest priority):")
        lines.append(custom)
    if tone:
        lines.append(f"Voice / tone: {tone}")
    if intro:
        lines.append("Self-introduction (in the companion's own words):")
        lines.append(intro)
    return "\n".join(lines).strip()


def material_sha256(material: str) -> str:
    """Stable digest of the assembled material — drives the idempotency key so the
    same identity material is not re-backfilled every supervisor tick."""
    return hashlib.sha256(_clean(material).encode("utf-8")).hexdigest()


def backfill_idempotency_key(user_id: str, material_hash: str) -> str:
    """``persona_backfill:v1:<user_id>:<material_hash>`` — stable across ticks for a
    given (user, material). Used to dedupe: an existing genesis job with this key
    (uploaded/processing/done) means backfill is in-flight or done, so skip re-enqueue.
    """
    return f"persona_backfill:{_KEY_VERSION}:{_clean(user_id)}:{_clean(material_hash)}"


def backfill_source_kind() -> str:
    return _SOURCE_KIND

"""Framework-neutral /v1/onboarding/validate core (ASGI-migration plan §5.3 / §9).

The onboarding acceptance check is deliberately server-side and artifact-based:
the payload is built from what Feedling can actually observe (writes, resident-
consumer heartbeat, verify-loop events, real user→agent exchange). The whole
builder stays in ``hosted.onboarding_validation`` and is reached through the
injected ``build_payload`` seam so both Flask and ASGI return byte-identical
bodies and there is no core↔routes import cycle.
"""

from __future__ import annotations

from core.store import UserStore


def validate(store: UserStore, *, build_payload):
    """GET /v1/onboarding/validate — always 200 with the validation payload."""
    return build_payload(store), 200

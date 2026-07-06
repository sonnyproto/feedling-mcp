"""Cheap context fields for every agent wake.

hosted/context.py splices snapshot_for_wake(user_id) into the per-turn
context_payload so
the agent always has the user's coarse current state (place_label, motion,
battery, user_state, etc.) without spending a tool call. Unauthorized/stale
fields are null — the agent treats null as "not permitted, don't infer."

This is intentionally the SAME shape as the context_snapshot tool, so what the
agent sees passively and what it can pull on demand never diverge.
"""
from __future__ import annotations

import logging

from . import service

log = logging.getLogger("perception.wake")


def snapshot_for_wake(user_id: str) -> dict:
    try:
        return service.snapshot(user_id)
    except Exception as e:
        log.error("snapshot_for_wake(%s) failed: %s", user_id, e)
        return {}

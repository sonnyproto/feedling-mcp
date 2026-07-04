"""Extended Perception — a self-contained backend feature module.

Gives the resident agent coarse, permission-gated awareness of the user's
context (location label, wifi label, app category, motion, device signals, iOS
Focus, plus Tier 2: calendar / health / photos / bluetooth / now playing /
weather / region) so it can act like a companion even when screen broadcast is
off.

The HTTP surface is native ASGI (``perception.routes_asgi``, wired by
``asgi_app``); the Flask blueprint was deleted in the ASGI cutover. The per-turn
wake snapshot is still exported here:
    from perception import snapshot_for_wake
    context_payload["perception"] = snapshot_for_wake(store.user_id)
"""
from __future__ import annotations

from .wake import snapshot_for_wake

__all__ = ["snapshot_for_wake"]

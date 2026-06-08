"""Extended Perception — a self-contained backend feature module.

Gives the resident agent coarse, permission-gated awareness of the user's
context (location label, wifi label, app category, motion, device signals, iOS
Focus, plus Tier 2: calendar / health / photos / bluetooth / now playing /
weather / region) so it can act like a companion even when screen broadcast is
off.

Integration with the rest of the backend is two one-liners in app.py:
    from perception import register as register_perception
    register_perception(app)
and, in the per-turn wake context (context_payload):
    from perception import snapshot_for_wake
    context_payload["perception"] = snapshot_for_wake(store.user_id)

Everything else (routing, DB access, resolution, wake triggering) lives here.
"""
from __future__ import annotations

from .routes import bp
from .wake import snapshot_for_wake

__all__ = ["register", "snapshot_for_wake"]


def register(app) -> None:
    """Mount the /v1/perception blueprint onto the Flask app."""
    app.register_blueprint(bp)

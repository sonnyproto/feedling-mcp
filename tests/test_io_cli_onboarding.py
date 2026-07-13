"""io_cli onboarding/chat verb payload builders (pure).

Covers the thin io_cli verbs added for onboarding acceptance tooling:
``onboarding-validate`` (GET /v1/onboarding/validate) and ``chat-verify-loop``
(POST /v1/chat/verify_loop).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_next_step_from_bootstrap():
    from io_cli import _next_onboarding_step
    s0 = {"identity_written": False, "chat_loop_verified": False, "agent_messages_count": 0}
    assert _next_onboarding_step(s0)["next_cmd"].startswith("io_cli identity-init")
    s1 = {"identity_written": True, "chat_loop_verified": False, "agent_messages_count": 0}
    assert "verify" in _next_onboarding_step(s1)["next_cmd"]
    s2 = {"identity_written": True, "chat_loop_verified": True, "agent_messages_count": 0}
    assert "greet" in _next_onboarding_step(s2)["next_cmd"]
    s3 = {"identity_written": True, "chat_loop_verified": True, "agent_messages_count": 1}
    assert _next_onboarding_step(s3)["done"] is True

"""io_cli onboarding/chat verb payload builders (pure).

Covers the thin io_cli verbs added for onboarding acceptance tooling:
``onboarding-validate`` (GET /v1/onboarding/validate), ``chat-verify-loop``
(POST /v1/chat/verify_loop), and ``chat-greet`` (POST /v1/chat/response —
the agent-authored-reply endpoint; see ``_greet_payload`` docstring in
io_cli.py for why the payload field is ``content``, not ``text``).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_greet_requires_message():
    from io_cli import _greet_payload
    assert _greet_payload("") is None
    assert _greet_payload("嗨") == {"content": "嗨"}


def test_greet_payload_strips_whitespace():
    from io_cli import _greet_payload
    assert io_cli._greet_payload("   ") is None
    assert io_cli._greet_payload("  hi there  ") == {"content": "hi there"}


def test_greet_payload_none_input_is_none():
    from io_cli import _greet_payload
    assert io_cli._greet_payload(None) is None

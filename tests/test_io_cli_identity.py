"""io_cli identity-write payload builder (pure).

The hosted agent uses `io_cli.py identity-write` in post-respawn (7.D) to write its
own self_introduction / signature via /v1/identity/actions (identity.profile_patch).
The server does the crypto (decrypt existing → merge → re-encrypt), so the CLI just
shapes the action body.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_identity_write_payload_self_introduction_and_signature():
    p = io_cli._identity_write_payload("I keep you honest.", ["always direct", "never coddles"])
    assert p == {"action": {"type": "identity.profile_patch", "patch": {
        "self_introduction": "I keep you honest.",
        "signature": ["always direct", "never coddles"],
    }}}


def test_identity_write_payload_partial_fields():
    assert io_cli._identity_write_payload("hi", [])["action"]["patch"] == {"self_introduction": "hi"}
    assert io_cli._identity_write_payload(None, ["sig"])["action"]["patch"] == {"signature": ["sig"]}


def test_identity_write_payload_empty_is_none():
    assert io_cli._identity_write_payload(None, []) is None


def test_identity_init_payload_fresh_start_and_sanitize():
    from io_cli import _identity_init_payload
    body = _identity_init_payload(
        agent_name="阿锐", self_introduction="hi",
        dimensions=[{"name": "锐利", "value": 150, "description": "x"}],
        days_with_user=None, anchor=None, fresh_start=True)
    assert body["days_with_user"] == 0
    assert len(body["relationship_anchor_evidence"]) >= 8  # fresh-start 标准证据
    assert body["identity"]["dimensions"][0]["value"] == 100  # sanitize 夹过

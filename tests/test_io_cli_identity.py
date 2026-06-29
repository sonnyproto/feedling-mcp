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

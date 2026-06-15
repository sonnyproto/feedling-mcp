"""Pure-unit tests for history-import identity normalization
(hosted/history_import.py _normalize_identity_payload).

P2 distills the companion's VOICE — not just facts — at import time:
tone_style / agent_role / do_not_say / boundaries. These must round-trip through
normalization (sanitized, empties dropped) so they reach the encrypted identity
body and, via P1a, the hosted chat prompt. No Flask, no DB — history_import
imports cleanly with backend on sys.path.

Run:  python -m pytest tests/test_history_import_identity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import history_import as hi  # noqa: E402


def _valid_dimensions():
    return [{"name": f"d{i}", "value": 50 + i, "description": "x"} for i in range(7)]


def _raw(**extra):
    base = {
        "agent_name": "Kai",
        "self_introduction": "I help you write and I keep you honest.",
        "category": "Sharp",
        "signature": ["always direct", "never coddles"],
        "dimensions": _valid_dimensions(),
    }
    base.update(extra)
    return base


def test_normalize_keeps_persona_voice_fields():
    out = hi._normalize_identity_payload(
        _raw(
            tone_style="Terse. Calls you boss. Drops 哈 at the end.",
            agent_role="coding partner",
            do_not_say=["亲爱的", "宝贝"],
            boundaries=["no fake praise"],
        ),
        [],
        10,
        "en",
    )
    assert out["tone_style"].startswith("Terse")
    assert out["agent_role"] == "coding partner"
    assert out["do_not_say"] == ["亲爱的", "宝贝"]
    assert out["boundaries"] == ["no fake praise"]


def test_normalize_drops_empty_persona_fields():
    out = hi._normalize_identity_payload(
        _raw(tone_style="", agent_role="   ", do_not_say=[], boundaries="not-a-list"),
        [],
        10,
        "en",
    )
    assert "tone_style" not in out
    assert "agent_role" not in out
    assert "do_not_say" not in out
    assert "boundaries" not in out


def test_normalize_cleans_blank_list_items():
    out = hi._normalize_identity_payload(
        _raw(do_not_say=["老板", "", "   ", "boss"]),
        [],
        10,
        "en",
    )
    assert out["do_not_say"] == ["老板", "boss"]


def test_normalize_caps_tone_style_length():
    out = hi._normalize_identity_payload(_raw(tone_style="x" * 5000), [], 10, "en")
    assert len(out["tone_style"]) == 1200


def test_normalize_without_persona_fields_is_unaffected():
    # A model that returns no persona fields must still produce a valid card.
    out = hi._normalize_identity_payload(_raw(), [], 10, "en")
    assert out["agent_name"] == "Kai"
    assert len(out["dimensions"]) == 7
    for key in ("tone_style", "agent_role", "do_not_say", "boundaries"):
        assert key not in out

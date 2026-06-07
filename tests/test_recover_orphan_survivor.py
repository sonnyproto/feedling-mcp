"""Survivor-selection logic for the orphan-account recovery tool.

The survivor is the lineage's currently-ACTIVE account — the one whose API key
the app still holds — so re-owned history lands where the user can see it.
Chat presence must NOT dominate selection: a freshly re-registered active
account often has little/no chat yet, while the dead orphan holds all the
history. Picking by chat would move data INTO the dead account.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import recover_orphan_accounts as rec  # noqa: E402


def _acct(uid, created, *, last_seen="", chat=0, chat_max=0.0, live_keys=1):
    return {"uid": uid, "created": created, "last_seen": last_seen,
            "chat": chat, "chat_max": chat_max, "mem": 0, "blobs": [],
            "live_keys": live_keys}


def test_survivor_is_newest_active_even_with_no_chat():
    # Reinstall case: the active (newest) account has no chat yet; the dead
    # orphan holds all the history. Survivor must be the active one.
    accts = [
        _acct("active_new", "2026-06-05T04:23:06", last_seen="", chat=0, chat_max=0.0),
        _acct("dead_old", "2026-06-03T15:10:03", last_seen="2026-06-03T15:44:40",
              chat=207, chat_max=1780633406.0),
    ]
    assert rec._pick_survivor(accts)["uid"] == "active_new"


def test_survivor_requires_live_key_over_newer_keyless_account():
    # An account with no live api_key can't be authenticated against, so it
    # can't be the active account — even if it's newer / has more chat.
    accts = [
        _acct("has_key", "2026-06-01T00:00:00", live_keys=1),
        _acct("newer_no_key", "2026-06-09T00:00:00", chat=50, chat_max=1780900000.0,
              live_keys=0),
    ]
    assert rec._pick_survivor(accts)["uid"] == "has_key"

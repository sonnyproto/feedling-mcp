"""Cross-worker `users` channel coverage (Codex review follow-ups).

Under -w N a registry edit on one worker must broadcast so the others reload —
otherwise a worker keeps serving a stale _users / _key_to_user snapshot and a
new user/key 401s, or a public-key / preference / access edit is invisible. The
full-rewrite path (_save_users) and the single-row db.upsert_user paths must
both fire wake_bus.notify("users").

Run:  python -m pytest tests/test_users_channel_broadcast.py -q
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from accounts import registry  # noqa: E402
from core import wake_bus  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def captured(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wake_bus, "notify", lambda ch, uid="": calls.append((ch, uid)))
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()  # not broadcast: default
    return calls


def _register(calls) -> str:
    res = appmod.app.test_client().post(
        "/v1/users/register",
        json={"public_key": _b64(os.urandom(32)), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def test_register_broadcasts_users(captured):
    _register(captured)
    assert ("users", "") in captured  # _save_users(broadcast=True) on register


def test_set_public_key_broadcasts_users(captured):
    uid = _register(captured)
    captured.clear()
    assert registry._set_user_public_key(uid, _b64(os.urandom(32))) is True
    assert ("users", "") in captured  # single-row db.upsert_user path now broadcasts


def test_no_op_public_key_update_does_not_broadcast(captured):
    _register(captured)
    captured.clear()
    # Unknown user -> no write -> no broadcast.
    assert registry._set_user_public_key("usr_does_not_exist", "x") is False
    assert ("users", "") not in captured


def test_load_users_is_lock_guarded_and_reloads(captured):
    uid = _register(captured)
    # Reload (as the wake-bus listener would) must run under _users_lock without
    # deadlock and rebuild the key cache so the user still resolves.
    registry.load_users()
    assert any(u.get("user_id") == uid for u in registry._users)

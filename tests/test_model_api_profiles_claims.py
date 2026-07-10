"""Tests for Task 8: db.chat_expire_reply_claims.

The release is invoked from the supervisor's respawn branch AFTER the old
consumer is killed (see tests/test_agent_runtime_supervisor.py for the ordering
guarantee) — NOT from the activate endpoint, which would open a double-burn
window while the old consumer is still alive. This file covers only the db-layer
primitive.

Kept separate from tests/test_model_api_profiles_db.py and
tests/test_model_api_profiles_endpoints.py per the task brief so the three
tasks' test suites don't collide.

Requires a real PostgreSQL — see tests/conftest.py.
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402

from conftest import seed_user  # noqa: E402


def _uid() -> str:
    return f"usr_{uuid.uuid4().hex[:16]}"


# ─────────────────────────── db-layer tests ───────────────────────────


def test_expire_reply_claims_releases_inflight_unanswered_claim(backend_env):
    uid = _uid()
    seed_user(uid)
    now = 1_000_000.0
    db.chat_append(uid, "m1", now, {
        "role": "user", "text": "in-flight",
        "reply_claimed_by": "consumer-A",
        "reply_claim_expires_at": str(now + 600)}, 500)

    freed = db.chat_expire_reply_claims(uid)
    assert freed == 1

    docs = {d["text"]: d for d in db.chat_load(uid)}
    assert docs["in-flight"]["reply_claimed_by"] == ""
    assert docs["in-flight"]["reply_claim_expires_at"] == ""


def test_expire_reply_claims_leaves_already_replied_row_untouched(backend_env):
    uid = _uid()
    seed_user(uid)
    now = 1_000_000.0
    db.chat_append(uid, "m2", now, {
        "role": "user", "text": "already-replied",
        "reply_claimed_by": "consumer-A",
        "reply_claim_expires_at": str(now + 600),
        "reply_status": "replied"}, 500)

    freed = db.chat_expire_reply_claims(uid)
    assert freed == 0

    docs = {d["text"]: d for d in db.chat_load(uid)}
    assert docs["already-replied"]["reply_claimed_by"] == "consumer-A"
    assert docs["already-replied"]["reply_claim_expires_at"] == str(now + 600)


def test_expire_reply_claims_leaves_row_with_reply_message_id_untouched(backend_env):
    uid = _uid()
    seed_user(uid)
    now = 1_000_000.0
    db.chat_append(uid, "m3", now, {
        "role": "user", "text": "has-reply-message-id",
        "reply_claimed_by": "consumer-A",
        "reply_claim_expires_at": str(now + 600),
        "reply_message_id": "assistant-reply-1"}, 500)

    freed = db.chat_expire_reply_claims(uid)
    assert freed == 0

    docs = {d["text"]: d for d in db.chat_load(uid)}
    assert docs["has-reply-message-id"]["reply_claimed_by"] == "consumer-A"


def test_expire_reply_claims_leaves_unclaimed_row_untouched(backend_env):
    uid = _uid()
    seed_user(uid)
    now = 1_000_000.0
    db.chat_append(uid, "m4", now, {"role": "user", "text": "unclaimed"}, 500)

    freed = db.chat_expire_reply_claims(uid)
    assert freed == 0

    docs = {d["text"]: d for d in db.chat_load(uid)}
    assert docs["unclaimed"].get("reply_claimed_by", "") == ""


def test_expire_reply_claims_only_touches_calling_user(backend_env):
    uid_a = _uid()
    uid_b = _uid()
    seed_user(uid_a)
    seed_user(uid_b)
    now = 1_000_000.0
    db.chat_append(uid_a, "ma", now, {
        "role": "user", "text": "a-in-flight",
        "reply_claimed_by": "consumer-A",
        "reply_claim_expires_at": str(now + 600)}, 500)
    db.chat_append(uid_b, "mb", now, {
        "role": "user", "text": "b-in-flight",
        "reply_claimed_by": "consumer-B",
        "reply_claim_expires_at": str(now + 600)}, 500)

    freed = db.chat_expire_reply_claims(uid_a)
    assert freed == 1

    a_docs = {d["text"]: d for d in db.chat_load(uid_a)}
    b_docs = {d["text"]: d for d in db.chat_load(uid_b)}
    assert a_docs["a-in-flight"]["reply_claimed_by"] == ""
    assert b_docs["b-in-flight"]["reply_claimed_by"] == "consumer-B"  # 别的用户不动


def test_expire_reply_claims_no_claimed_rows_returns_zero_no_error(backend_env):
    uid = _uid()
    seed_user(uid)
    now = 1_000_000.0
    db.chat_append(uid, "m5", now, {"role": "user", "text": "plain"}, 500)
    db.chat_append(uid, "m6", now + 1, {"role": "assistant", "text": "reply text"}, 500)

    freed = db.chat_expire_reply_claims(uid)
    assert freed == 0


def test_expire_reply_claims_unknown_user_returns_zero_no_error(backend_env):
    # No chat_messages row at all for this user — must not raise.
    assert db.chat_expire_reply_claims(_uid()) == 0

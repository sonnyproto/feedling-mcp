"""/v1/chat/response role 白名单 + system 消息的存储/下发/隔离语义。

spec: docs/superpowers/specs/2026-07-06-upstream-error-surfacing-design.md
Run:  python -m pytest tests/test_chat_system_notice_role.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from chat import chat_core  # noqa: E402
from chat import service as chat_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _env(user_id: str, marker: str) -> dict:
    return {
        "v": 1, "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared", "owner_user_id": user_id,
    }


@pytest.fixture()
def store(backend_env):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


def _post_response(store, payload):
    return chat_core.write_response(
        store, payload, consumer_id="test-consumer",
        consumer_info={}, allow_verify_reply=False)


def test_system_role_stored_with_notice_kind(store):
    body, status = _post_response(store, {
        "envelope": _env(store.user_id, "sysmsg1"),
        "role": "system", "notice_kind": "upstream_error",
    })
    assert status in (200, 201), body
    msgs = store.chat_messages
    m = next(x for x in msgs if x["id"] == "sysmsg1")
    assert m["role"] == "system"
    assert m["notice_kind"] == "upstream_error"


def test_invalid_role_falls_back_to_openclaw(store):
    body, status = _post_response(store, {
        "envelope": _env(store.user_id, "badrole1"), "role": "hacker",
    })
    assert status in (200, 201), body
    m = next(x for x in store.chat_messages if x["id"] == "badrole1")
    assert m["role"] == "openclaw"
    assert "notice_kind" not in m


def test_notice_kind_ignored_for_openclaw_and_truncated_for_system(store):
    _post_response(store, {
        "envelope": _env(store.user_id, "oc1"), "notice_kind": "upstream_error"})
    m = next(x for x in store.chat_messages if x["id"] == "oc1")
    assert "notice_kind" not in m

    _post_response(store, {
        "envelope": _env(store.user_id, "sys2"), "role": "system",
        "notice_kind": "k" * 200})
    m = next(x for x in store.chat_messages if x["id"] == "sys2")
    assert len(m["notice_kind"]) == 64


def test_history_item_system_sender_is_assistant():
    # 老版 iOS 的 sender Decodable 不能见到未知值 → system 映射到 assistant，
    # 新版靠 role=="system" 区分（spec §组件2 老版兼容）
    item = chat_service._chat_history_item({
        "id": "x", "role": "system", "notice_kind": "upstream_error",
        "body_ct": "", "content_type": "text"})
    assert item["sender"] == "assistant"
    assert item["is_from_openclaw"] is False
    assert item["role"] == "system"
    assert item["notice_kind"] == "upstream_error"


def test_system_message_not_claimable(store):
    _post_response(store, {
        "envelope": _env(store.user_id, "sysmsg3"), "role": "system",
        "notice_kind": "upstream_error"})
    m = next(x for x in store.chat_messages if x["id"] == "sysmsg3")
    assert not chat_service._chat_message_claimable(m, "any-consumer", 9e12)


def test_system_message_does_not_mark_replied(store):
    user_msg = store.append_chat("user", "chat", _env(store.user_id, "umsg1"))
    _post_response(store, {
        "envelope": _env(store.user_id, "sysmsg4"), "role": "system",
        "notice_kind": "upstream_error",
        "reply_to_message_id": user_msg["id"],
    })
    m = next(x for x in store.chat_messages if x["id"] == "umsg1")
    # system 消息不承担已回复标记（那是兜底话术的职责，spec role 审计表）
    assert m.get("reply_status") != "replied"

"""
Regression tests for tools/chat_resident_consumer.py
=====================================================

Run with: pytest tests/test_chat_resident_consumer.py -v
"""

import importlib
import base64
import json
import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars before the module is imported.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Add repo root + backend dir to path (needed for real import in non-test environments).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Stub out content_encryption only when the backend tree is unavailable. In the
# full backend suite, app.py needs the real module; poisoning sys.modules here
# makes later envelope tests import a fake build_envelope.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(role="user", content="hello", ts=None, timestamp=None):
    msg = {"role": role, "content": content}
    if ts is not None:
        msg["ts"] = ts
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def _make_image_msg(ts=1.0, image_bytes=b"fake-jpeg"):
    msg = _make_msg(role="user", content="", ts=ts)
    msg["content_type"] = "image"
    msg["image_b64"] = base64.b64encode(image_bytes).decode("ascii")
    return msg


# ---------------------------------------------------------------------------
# Case 1: user message with empty content → no fallback, checkpoint advances
# ---------------------------------------------------------------------------

def test_empty_content_no_fallback():
    """poll returns user message with empty content (encrypted envelope) —
    _process_messages must skip it without calling post_reply."""
    msgs = [_make_msg(role="user", content="", ts=1000.0)]

    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages(msgs)

    mock_agent.assert_not_called()
    mock_post.assert_not_called()
    assert result_ts == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Case 2: message has only "ts" key (no "timestamp") → checkpoint advances
# ---------------------------------------------------------------------------

def test_ts_key_only_advances_checkpoint():
    """API returns {"ts": 1234.5} with no "timestamp" key.
    _process_messages must still return 1234.5 so the checkpoint advances."""
    msgs = [_make_msg(role="user", content="what time is it?", ts=1234.5)]
    # ts=1234.5, no "timestamp" key

    with patch.object(crc, "call_agent", return_value="It's noon."), \
         patch.object(crc, "post_reply"):
        result_ts = crc._process_messages(msgs)

    assert result_ts == pytest.approx(1234.5)


def test_timestamp_key_only_advances_checkpoint():
    """API returns {"timestamp": 5678.9} with no "ts" key — same result."""
    msgs = [_make_msg(role="user", content="hi", timestamp=5678.9)]

    with patch.object(crc, "call_agent", return_value="hey"), \
         patch.object(crc, "post_reply"):
        result_ts = crc._process_messages(msgs)

    assert result_ts == pytest.approx(5678.9)


def test_filter_messages_to_poll_ids_keeps_only_claimed_rows():
    decrypted = [
        {"id": "msg-a", "role": "user", "content": "ours"},
        {"id": "msg-b", "role": "user", "content": "claimed by someone else"},
    ]
    poll_messages = [{"id": "msg-a", "role": "user"}]

    assert crc._filter_messages_to_poll_ids(decrypted, poll_messages) == [decrypted[0]]


def test_process_messages_posts_reply_with_source_message_id():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = {"id": "user-msg-1", "role": "user", "content": "hi", "ts": 1111.0}

    with patch.object(crc, "call_agent", return_value="hey"), \
         patch.object(crc, "post_reply", return_value={"id": "reply-msg-1"}) as mock_post:
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(1111.0)
    assert mock_post.call_args.kwargs["reply_to_message_id"] == "user-msg-1"


def test_process_messages_uses_resident_v2_tool_loop_when_chat_flag_on(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._update_chat_runtime_v2_profile({crc.RESIDENT_CHAT_RUNTIME_V2_FLAG: True})
    msg = {"id": "user-msg-v2", "role": "user", "content": "天气怎么样？", "ts": 1112.0}
    captured = {}

    def fake_run(message, *, foreground_chat=False):
        captured["message"] = message
        captured["foreground_chat"] = foreground_chat
        return {"messages": ["外面下雨。"]}

    try:
        with patch.object(crc, "_resident_run_agent_v2", side_effect=fake_run) as mock_v2, \
             patch.object(crc, "call_agent") as mock_legacy, \
             patch.object(crc, "post_reply", return_value={"id": "reply-msg-v2"}) as mock_post:
            result_ts = crc._process_messages([msg])
    finally:
        crc._update_chat_runtime_v2_profile({})

    assert result_ts == pytest.approx(1112.0)
    mock_v2.assert_called_once()
    mock_legacy.assert_not_called()
    assert captured["foreground_chat"] is True
    assert "perception.weather" in captured["message"]
    assert "User message:\n天气怎么样？" in captured["message"]
    assert mock_post.call_args.args[0] == "外面下雨。"
    assert mock_post.call_args.kwargs["reply_to_message_id"] == "user-msg-v2"


def test_process_messages_v2_needs_background_acks_and_queues(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    crc._update_chat_runtime_v2_profile({crc.RESIDENT_CHAT_RUNTIME_V2_FLAG: True})
    msg = {"id": "user-msg-bg", "role": "user", "content": "今天多少步？", "ts": 1113.0}
    queued = []

    try:
        with patch.object(
            crc,
            "_resident_run_agent_v2",
            return_value={"actions": [{"type": "needs_background", "request": {"tool": "perception.steps", "args": {}}}], "messages": []},
        ), patch.object(
            crc,
            "_queue_resident_background_request_v2",
            side_effect=lambda request, source_message_id="": queued.append((request, source_message_id)) or {"status": "queued", "job_id": "bg_1"},
        ), patch.object(crc, "execute_agent_actions") as mock_actions, \
             patch.object(crc, "post_reply", return_value={"id": "reply-bg"}) as mock_post:
            result_ts = crc._process_messages([msg])
    finally:
        crc._update_chat_runtime_v2_profile({})

    assert result_ts == pytest.approx(1113.0)
    assert queued == [({"tool": "perception.steps", "args": {}}, "user-msg-bg")]
    mock_actions.assert_not_called()
    assert mock_post.call_args.args[0] == "我看一下，查完再告诉你。"


def test_process_messages_keeps_checkpoint_when_post_reply_fails():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    msg = {"id": "user-msg-2", "role": "user", "content": "hi", "ts": 2222.0}

    with patch.object(crc, "call_agent", return_value="hey"), \
         patch.object(crc, "post_reply", side_effect=RuntimeError("write failed")):
        result_ts = crc._process_messages([msg])

    assert result_ts == 0.0


# ---------------------------------------------------------------------------
# Case 3: invalid API key → run() exits non-zero
# ---------------------------------------------------------------------------

def test_invalid_key_exits_on_startup():
    """If whoami returns 401 / can't get user_id at startup, run() must
    call sys.exit(1) rather than entering the poll loop silently."""
    with patch.object(crc, "_load_whoami", return_value=False), \
         patch.object(crc, "WHOAMI_STARTUP_RETRIES", 1), \
         patch.object(crc, "_ENCRYPTION_AVAILABLE", True), \
         pytest.raises(SystemExit) as exc_info:
        crc.run()

    assert exc_info.value.code != 0


def test_whoami_startup_retries_transient_failure(monkeypatch):
    """Startup whoami should tolerate transient network failures."""
    calls = []

    def _load():
        calls.append(1)
        return len(calls) >= 3

    monkeypatch.setattr(crc, "_load_whoami", _load)
    monkeypatch.setattr(crc, "WHOAMI_STARTUP_RETRIES", 4)
    monkeypatch.setattr(crc, "WHOAMI_STARTUP_RETRY_DELAY_SEC", 0)

    assert crc._load_whoami_with_retries() is True
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Bonus: agent failures are fail-hard by default
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 2: enclave source + dedup
# ---------------------------------------------------------------------------

def test_enclave_history_used_when_configured(monkeypatch):
    """When FEEDLING_ENCLAVE_URL is set and enclave returns decrypted messages,
    _process_messages receives actual content (not the empty poll payload)."""
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://127.0.0.1:5003")
    decrypted = [_make_msg(role="user", content="decrypted hello", ts=2000.0)]
    monkeypatch.setattr(crc, "get_decrypted_history", lambda since, limit=20: decrypted)

    with patch.object(crc, "call_agent", return_value="hi back") as mock_agent, \
         patch.object(crc, "post_reply"):
        result_ts = crc._process_messages(decrypted)

    mock_agent.assert_called_once_with("decrypted hello")
    assert result_ts == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Verify-loop liveness ping must be answered WITHOUT a full agent turn.
# Regression for the prod 2026-06-03 wedge (account stuck at
# needs_live_connection): the synthetic ping was routed through the hermes
# agent (slow / SIGTERM-fragile) and, over the enclave decrypt path, arrived
# with content=None (the ping is visibility=local_only so the enclave returns
# null) — falling into the empty-content skip and never replying. Detection
# keys off `source == "verify_ping"`, which survives BOTH the poll and the
# enclave/MCP decrypt paths.
# ---------------------------------------------------------------------------

def test_verify_ping_enclave_path_short_circuits_without_agent():
    """Enclave path delivers the local_only ping with content=None. The
    consumer must still recognise it (via source) and reply immediately —
    not crash on None and not skip it as empty content."""
    ping = {
        "role": "user",
        "ts": 4242.0,
        "source": "verify_ping",
        "content": None,            # enclave returns null for local_only
        "content_type": "text",
    }
    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([ping])

    mock_agent.assert_not_called()
    mock_post.assert_called_once()
    assert result_ts == pytest.approx(4242.0)


def test_verify_ping_poll_marker_short_circuits_without_agent():
    """Direct /v1/chat/poll path carries the plaintext __VERIFY_PING__ marker
    (source still verify_ping). Still short-circuits — no agent turn."""
    ping = _make_msg(role="user", content="__VERIFY_PING__:deadbeef0001", ts=4343.0)
    ping["source"] = "verify_ping"

    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([ping])

    mock_agent.assert_not_called()
    mock_post.assert_called_once()
    assert result_ts == pytest.approx(4343.0)


def test_verify_ping_short_circuit_suppresses_push():
    """The short-circuit must ask post_reply to suppress the user-visible push.
    A private liveness ack must never surface as an APNs notification while the
    app is backgrounded — the verify GC removes the chat row but cannot recall
    an already-delivered push."""
    ping = {
        "role": "user",
        "ts": 4444.0,
        "source": "verify_ping",
        "content": None,
        "content_type": "text",
    }
    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        crc._process_messages([ping])

    mock_agent.assert_not_called()
    mock_post.assert_called_once_with(crc.VERIFY_PING_REPLY, suppress_push=True)


def test_post_reply_suppress_push_omits_alert_and_push_fields(monkeypatch):
    """post_reply(..., suppress_push=True) sends an empty alert_body and no
    push_body / push_live_activity, so /v1/chat/response's push policy is a
    no-op. The envelope still posts, so verify_loop still sees the reply."""
    captured: dict = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": "m1", "ts": 1.0, "v": 1}

    def _post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(crc, "_load_whoami", lambda: True)
    monkeypatch.setattr(
        crc, "_whoami_cache",
        {"user_id": "usr_abc", "user_pk": b"\x01" * 32, "enclave_pk": None},
    )
    monkeypatch.setattr(crc, "_build_envelope", lambda **kw: {"stub": "env"})
    monkeypatch.setattr(crc.httpx, "post", _post)

    crc.post_reply(crc.VERIFY_PING_REPLY, suppress_push=True)
    body = captured["json"]
    assert body["alert_body"] == ""
    assert not body.get("push_body")
    assert not body.get("push_live_activity")

    # Contrast: an ordinary reply still carries a visible alert_body.
    crc.post_reply("hello there")
    assert captured["json"]["alert_body"] == "hello there"


def test_user_message_containing_verify_marker_is_not_short_circuited():
    """A real user message that merely CONTAINS the literal __VERIFY_PING__
    text (e.g. someone debugging this very feature) must still reach the agent.
    Detection keys ONLY on source — the server stamps source='verify_ping' on
    the synthetic probe across all three delivery paths (poll, enclave, MCP),
    so matching arbitrary content would just create false positives that
    silently swallow real chat input."""
    msg = _make_msg(
        role="user",
        content="why does __VERIFY_PING__ keep showing up in my logs?",
        ts=4545.0,
    )  # NB: no source key → ordinary chat, not a verify probe

    with patch.object(crc, "call_agent", return_value="here's why") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_agent.assert_called_once()
    mock_post.assert_called_once_with("here's why")
    assert result_ts == pytest.approx(4545.0)


def test_enclave_fetch_logs_response_body_on_http_error(monkeypatch, caplog):
    """When the enclave returns a non-2xx, the consumer must log the response
    BODY, not just the bare status line.

    Regression (2026-06-03): the enclave now maps transient dependency
    failures to self-describing codes — 502 `backend_unreachable` vs 503
    `key_derivation_unavailable`. httpx's HTTPStatusError string carries the
    status + URL but NOT the body, so `log.warning("... %s", e)` hid exactly
    the field that distinguishes the two failure modes. The operator was left
    with an opaque "all decrypt sources failed" and no way to tell which
    dependency broke without shelling into the CVM.
    """
    import httpx as _httpx

    url = "https://127.0.0.1:5003/v1/chat/history"
    resp = _httpx.Response(
        503,
        json={"error": "key_derivation_unavailable: dstack socket unavailable"},
        request=_httpx.Request("GET", url),
    )
    mock_client = MagicMock()
    mock_client.get.return_value = resp
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://127.0.0.1:5003")
    monkeypatch.setattr(crc, "_ENCLAVE_CLIENT", mock_client)

    with caplog.at_level("WARNING"):
        result = crc._fetch_from_enclave(since=0.0, limit=20)

    assert result is None
    assert "503" in caplog.text
    assert "key_derivation_unavailable" in caplog.text


def test_image_message_passes_image_context_to_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(crc, "IMAGE_TEMP_DIR", tmp_path)
    msg = _make_image_msg(ts=2100.0)

    with patch.object(crc, "call_agent", return_value="I can see it.") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_post.assert_called_once()
    assert result_ts == pytest.approx(2100.0)
    _, kwargs = mock_agent.call_args
    assert kwargs["images"][0]["data"] == msg["image_b64"]
    assert kwargs["images"][0]["data_url"].startswith("data:image/jpeg;base64,")
    assert kwargs["image_paths"]
    assert Path(kwargs["image_paths"][0]).exists()


def test_screen_question_attaches_decrypted_screen_context(monkeypatch):
    msg = _make_msg(role="user", content="你能看到我的屏幕吗", ts=2200.0)
    screen_image = {
        "mime_type": "image/jpeg",
        "data": base64.b64encode(b"screen-jpeg").decode("ascii"),
        "data_url": "data:image/jpeg;base64,c2NyZWVuLWpwZWc=",
    }
    monkeypatch.setattr(
        crc,
        "_screen_context_for_message",
        lambda content: (
            "[Live Feedling screen-sharing context]\napp: com.feedling.mcp\nocr_text:\nhello screen",
            [screen_image],
            ["/tmp/feedling_chat_images/screen.jpg"],
        ),
    )

    with patch.object(crc, "call_agent", return_value="I can see it.") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_post.assert_called_once()
    assert result_ts == pytest.approx(2200.0)
    args, kwargs = mock_agent.call_args
    assert args[0].startswith("你能看到我的屏幕吗")
    assert "hello screen" in args[0]
    assert kwargs["images"] == [screen_image]
    assert kwargs["image_paths"] == ["/tmp/feedling_chat_images/screen.jpg"]


def test_dedup_prevents_reprocessing_same_message():
    """The same message processed twice (e.g. on restart with stale checkpoint)
    must not trigger a second agent call."""
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    msg = _make_msg(role="user", content="hello again", ts=3000.0)

    with patch.object(crc, "call_agent", return_value="reply") as mock_agent, \
         patch.object(crc, "post_reply"):
        crc._process_messages([msg])   # first time → processed
        crc._process_messages([msg])   # second time → deduped

    assert mock_agent.call_count == 1


def test_process_messages_executes_identity_actions_before_reply():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "identity.profile_patch",
        "patch": {"agent_name": "小秘"},
        "reason": "User asked for a displayed name change.",
    }
    msg = _make_msg(role="user", content="call yourself 小秘", ts=3100.0)

    def _execute(actions):
        events.append(("actions", actions))
        return {"status": "ok", "effects": [{"type": "identity_updated"}], "results": []}

    def _post(reply, **kwargs):
        events.append(("reply", reply))
        return {"id": "msg_1"}

    with patch.object(crc, "call_agent", return_value={"actions": [action], "messages": ["改好了。"]}), \
         patch.object(crc, "execute_agent_actions", side_effect=_execute), \
         patch.object(crc, "post_reply", side_effect=_post):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(3100.0)
    assert events[0] == ("actions", [action])
    assert events[1] == ("reply", "改好了。")


def test_process_messages_does_not_post_optimistic_reply_when_identity_action_fails():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    action = {
        "type": "identity.profile_patch",
        "patch": {"agent_name": "小秘"},
    }
    msg = _make_msg(role="user", content="把你的名字改成小秘", ts=3200.0)

    with patch.object(crc, "call_agent", return_value={"actions": [action], "messages": ["改好了。"]}), \
         patch.object(crc, "execute_agent_actions", side_effect=RuntimeError("write failed")), \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(3200.0)
    mock_post.assert_called_once()
    assert "没能" in mock_post.call_args.args[0]
    assert mock_post.call_args.args[0] != "改好了。"


def test_process_messages_executes_memory_actions_before_reply():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    events = []
    action = {
        "type": "memory.content_patch",
        "memory_id": "mom_1",
        "patch": {"description": "corrected"},
    }
    msg = _make_msg(role="user", content="这张记忆改一下", ts=3300.0)

    def _execute(actions):
        events.append(("actions", actions))
        return {"status": "ok", "effects": [{"type": "memory_updated"}], "results": []}

    def _post(reply, **kwargs):
        events.append(("reply", reply))
        return {"id": "msg_1"}

    with patch.object(crc, "call_agent", return_value={"actions": [action], "messages": ["改好了。"]}), \
         patch.object(crc, "execute_agent_actions", side_effect=_execute), \
         patch.object(crc, "post_reply", side_effect=_post):
        result_ts = crc._process_messages([msg])

    assert result_ts == pytest.approx(3300.0)
    assert events[0] == ("actions", [action])
    assert events[1] == ("reply", "改好了。")


# ---------------------------------------------------------------------------
# Phase 3: decrypt source unavailable cases
# ---------------------------------------------------------------------------

def test_empty_content_decrypt_source_available_replies(monkeypatch):
    """poll returns content="" but decrypt source is available and returns
    plaintext — consumer must reply using the decrypted content."""
    # Simulate poll returning empty-content message
    empty_msg = _make_msg(role="user", content="", ts=4000.0)
    # Decrypt source returns the plaintext version
    decrypted_msg = _make_msg(role="user", content="what's the weather?", ts=4000.0)

    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "https://127.0.0.1:5003")
    monkeypatch.setattr(
        crc, "get_decrypted_history",
        lambda since, limit=20: [decrypted_msg],
    )

    with patch.object(crc, "call_agent", return_value="sunny") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        # Consumer uses get_decrypted_history result, not the empty poll message
        result_ts = crc._process_messages([decrypted_msg])

    mock_agent.assert_called_once_with("what's the weather?")
    mock_post.assert_called_once()
    assert result_ts == pytest.approx(4000.0)


def test_empty_content_no_decrypt_source_no_reply_no_fallback(monkeypatch):
    """poll returns content="" and no decrypt source is configured —
    consumer must skip the message silently (no reply, no fallback)."""
    monkeypatch.setattr(crc, "FEEDLING_ENCLAVE_URL", "")
    crc._last_fallback_ts = 0.0

    msg = _make_msg(role="user", content="", ts=5000.0)

    with patch.object(crc, "call_agent") as mock_agent, \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([msg])

    mock_agent.assert_not_called()
    mock_post.assert_not_called()
    assert result_ts == pytest.approx(5000.0)


def test_agent_failure_no_fallback_by_default(monkeypatch):
    """Agent backend failure should not post a fake user-visible template."""
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", False)
    crc._last_fallback_ts = 0.0

    with patch.object(crc, "call_agent", side_effect=RuntimeError("agent down")), \
         patch.object(crc, "post_reply") as mock_post:
        result_ts = crc._process_messages([
            _make_msg(role="user", content="msg1", ts=100.0)
        ])

    mock_post.assert_not_called()
    assert result_ts == pytest.approx(100.0)


def test_fallback_is_explicit_opt_in(monkeypatch):
    """The legacy fallback path still exists only when explicitly enabled."""
    monkeypatch.setattr(crc, "SEND_FALLBACK_ON_AGENT_ERROR", True)
    crc._last_fallback_ts = 0.0

    with patch.object(crc, "call_agent", side_effect=RuntimeError("agent down")), \
         patch.object(crc, "post_reply") as mock_post, \
         patch("time.time", return_value=200.0):
        crc._process_messages([_make_msg(role="user", content="msg1", ts=101.0)])

    mock_post.assert_called_once()


def test_sanitize_reply_text_strips_leaks_and_duplicates():
    raw = """— ✵ Hermes

------
在，Seven。
在，Seven。
我在这儿，直接说你要我现在做什么。
Reasoning:
"""
    cleaned = crc._sanitize_reply_text(raw)
    assert "Hermes" not in cleaned
    assert "Reasoning" not in cleaned
    assert cleaned == "在，Seven。\n我在这儿，直接说你要我现在做什么。"


def test_sanitize_reply_text_prefers_cjk_and_drops_english_reasoning():
    raw = """I need to interpret this as a greeting.
I'm thinking a warm tone is best.
在呢，Seven。
你继续说，我在听。"""
    cleaned = crc._sanitize_reply_text(raw)
    assert cleaned == "在呢，Seven。\n你继续说，我在听。"


def test_sanitize_reply_text_pure_english_reasoning_returns_empty():
    raw = """The user wrote \"甜！\" which likely means they are giving a compliment.
I think it's best to respond warmly and playfully."""
    cleaned = crc._sanitize_reply_text(raw)
    assert cleaned == ""


def test_sanitize_reply_text_allows_direct_english_reply():
    raw = """Hello Seven — I see your message now.
Tell me what you want to work on next."""
    cleaned = crc._sanitize_reply_text(raw)
    assert cleaned == "Hello Seven — I see your message now.\nTell me what you want to work on next."


def test_sanitize_reply_text_drops_unlabeled_english_meta_before_cjk_answer():
    raw = """specific tool is required for this factual question, so I can rely on my memory
or general knowledge up to 2024. I remember Philip Daian as an Ethereum researcher
who analyzed the DAO exploit. I'll craft a concise answer.
Philip Daian 主要是把「区块链里看不见的交易操控」这件事讲明白、定义清楚，并推动行业修它。
最核心的几件事：
1) 把 MEV 这件事系统化
2) 揭示 DEX 和链上交易排序的结构性风险
"""
    cleaned = crc._sanitize_reply_text(raw)
    assert "specific tool is required" not in cleaned
    assert "general knowledge up to 2024" not in cleaned
    assert cleaned.startswith("Philip Daian 主要是")
    assert "把 MEV 这件事系统化" in cleaned


def test_extract_cli_output_preserves_full_answer_after_reasoning_block():
    raw = """💭 Reasoning:
```copy
**Executing updates**
I need to locate the repo root and think through the answer.
It's important to keep this concise.
```

我看到了。

这张图里有一张搜索结果卡片，下面还有一条学术资料链接。
Project founder
Research profile

如果你愿意，我可以继续帮你拆图里的关键信息。

session_id: sess_123
"""
    extracted = crc._extract_text_from_cli_output(raw)
    cleaned = crc._sanitize_reply_text(extracted)

    assert "Reasoning" not in cleaned
    assert "I need to" not in cleaned
    assert "Project founder" in cleaned
    assert "Research profile" in cleaned
    assert "如果你愿意" in cleaned


def test_agent_turn_splits_tagged_thinking_from_cli_text():
    raw = """<think>
比较了用户最新问题和已有上下文。
</think>

这是最终回复。"""

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["这是最终回复。"]
    assert turn.thinking_summary == "比较了用户最新问题和已有上下文。"
    assert turn.thinking_kind == "provider_reasoning_summary"
    assert turn.thinking_source == "tagged_content"
    assert turn.thinking_native is False


def test_agent_turn_splits_reasoning_and_thought_tags_from_cli_text():
    raw = """<reasoning>先查记忆。</reasoning>
<thought>再组织语气。</thought>
好，我在。"""

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["好，我在。"]
    assert turn.thinking_summary == "先查记忆。\n再组织语气。"
    assert turn.thinking_kind == "provider_reasoning_summary"


def test_extract_cli_output_prefers_structured_json_reply():
    raw = json.dumps(
        {
            "session_id": "sess_json",
            "reasoning": "internal text that must never be shown",
            "reply": "在，Seven。\n我看到了你发来的屏幕。",
        },
        ensure_ascii=False,
    )

    assert crc._extract_text_from_cli_output(raw) == "在，Seven。\n我看到了你发来的屏幕。"
    assert crc._extract_session_id(raw) == "sess_json"


def test_extract_cli_output_reads_jsonl_final_answer():
    raw = """
debug line for human terminal
{"event":"thinking","content":"do not use this"}
{"session_id":"sess_jsonl","final_answer":"这是最终回复。"}
"""

    assert crc._extract_text_from_cli_output(raw) == "这是最终回复。"
    assert crc._extract_session_id(raw) == "sess_jsonl"


def test_extract_cli_output_ignores_non_final_json_events():
    raw = """
{"session_id":"sess_jsonl","final_answer":"这是最终回复。"}
{"event":"thinking","content":"do not use this even if it appears last"}
"""

    assert crc._extract_text_from_cli_output(raw) == "这是最终回复。"
    assert crc._extract_session_id(raw) == "sess_jsonl"


def test_extract_cli_output_reads_openai_style_json():
    raw = json.dumps(
        {"choices": [{"message": {"content": "structured reply"}}]},
        ensure_ascii=False,
    )

    assert crc._extract_text_from_cli_output(raw) == "structured reply"


def test_extract_cli_output_reads_claude_code_json_result():
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "123e4567-e89b-12d3-a456-426614174000",
            "result": "在，我接上了。",
        },
        ensure_ascii=False,
    )

    assert crc._extract_text_from_cli_output(raw) == "在，我接上了。"
    assert crc._extract_session_id(raw) == "123e4567-e89b-12d3-a456-426614174000"


def test_extract_cli_output_strips_resumed_session_banner():
    raw = """↻ Resumed session
20260522_222908_60d12e (1 user message, 2 total messages)
在，Seven。
要我现在用「小哆啦」模式，陪你过一遍今天最关键的三件事吗？
"""
    extracted = crc._extract_text_from_cli_output(raw)
    cleaned = crc._sanitize_reply_text(extracted)

    assert "Resumed session" not in cleaned
    assert "20260522_222908_60d12e" not in cleaned
    assert "total messages" not in cleaned
    assert cleaned == "在，Seven。\n要我现在用「小哆啦」模式，陪你过一遍今天最关键的三件事吗？"


def test_sanitize_mixed_cli_output_drops_leading_non_cjk_block_and_duplicate_answer():
    raw = """Some unlabelled English transcript text from a CLI wrapper.
It may span multiple lines before the actual answer starts.
└──────────────────────────────────────────────────────────────────────────────┘
看到了，这次可以明确说：我已经看到了你共享出来的屏幕内容（至少这一帧）。
我看到的是一张社交平台帖子详情页（界面像小红书），主题在讲 Mentra Live 智能眼镜。
所以结论是：能看到你共享的画面内容了。
看到了，这次可以明确说：我已经看到了你共享出来的屏幕内容（至少这一帧）。
我看到的是一张社交平台帖子详情页（界面像小红书），主题在讲 Mentra Live 智能眼镜。
所以结论是：能看到你共享的画面内容了。
但严格说这是“已收到并读到共享帧”，不是持续实时遥控视角。
"""
    cleaned = crc._sanitize_reply_text(raw)

    assert "Some unlabelled English transcript" not in cleaned
    assert "actual answer starts" not in cleaned
    assert "└" not in cleaned
    assert cleaned.count("看到了，这次可以明确说") == 1
    assert "Mentra Live 智能眼镜" in cleaned
    assert cleaned.endswith("不是持续实时遥控视角。")


def test_normalize_agent_replies_supports_messages_array_json():
    raw = '{"messages":["在。","我在听。","继续说。"]}'
    out = crc._normalize_agent_replies(raw)
    assert out == ["在。", "我在听。", "继续说。"]


def test_extract_session_id_from_cli_output():
    raw = "some line\nsession_id: 20260503_024038_b526cf\n"
    assert crc._extract_session_id(raw) == "20260503_024038_b526cf"


def test_resolve_cli_executable_uses_agent_cli_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "hermes"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)

    monkeypatch.setattr(crc, "AGENT_CLI_PATH", str(bin_dir))
    monkeypatch.setenv("PATH", "")

    resolved = crc._resolve_cli_executable(["hermes", "chat"])
    assert resolved == [str(exe), "chat"]


def test_resolve_cli_executable_error_mentions_systemd(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_PATH", "")
    monkeypatch.setenv("PATH", "")

    with pytest.raises(FileNotFoundError, match="systemd service"):
        crc._resolve_cli_executable(["missing-agent", "chat"])


def test_agent_session_file_scoped_by_user_id(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_SESSION_FILE_TEMPLATE", "/tmp/feedling_{user_id}.txt")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc", "user_pk": None, "enclave_pk": None})
    p = crc._agent_session_file_for_user()
    assert str(p) == "/tmp/feedling_usr_abc.txt"


def test_prepare_hermes_cli_strips_continue_and_injects_resume(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --continue --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--continue" not in cmd
    assert cmd[:4] == ["hermes", "--resume", "sess_123", "chat"]
    assert "hello" in cmd


def test_prepare_hermes_cli_injects_resume_before_chat_with_top_level_flags(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes --yolo chat -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert cmd[:5] == ["hermes", "--resume", "sess_123", "--yolo", "chat"]
    assert "hello" in cmd


def test_prepare_hermes_cli_strips_unsupported_output_mode(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat --output-mode json -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_123")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--output-mode" not in cmd
    assert "json" not in cmd
    assert cmd[:4] == ["hermes", "--resume", "sess_123", "chat"]
    assert "hello" in cmd


def test_prepare_hermes_cli_first_turn_removes_continue(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --continue --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--continue" not in cmd
    assert "--resume" not in cmd


def test_prepare_claude_cli_first_turn_forces_print_json_and_strips_continue(monkeypatch):
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'claude --continue "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert "--continue" not in cmd
    assert "--resume" not in cmd
    assert "--print" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "hello" in cmd


def test_prepare_claude_cli_injects_stored_resume(monkeypatch):
    sid = "123e4567-e89b-12d3-a456-426614174000"
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'claude -p "{message}"',
    )
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: sid)
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("hello")

    assert cmd[:3] == ["claude", "--resume", sid]
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "hello" in cmd


def test_warn_if_hermes_cli_may_drift_logs_profile_and_turns(monkeypatch, caplog):
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --max-turns 1 -q "You are Dora. User message: {message}"',
    )
    monkeypatch.delenv("HERMES_HOME", raising=False)

    crc._warn_if_agent_entry_may_drift()

    text = caplog.text
    assert "wrap {message}" in text
    assert "without HERMES_HOME" in text
    assert "--max-turns 1" in text


def test_warn_if_hermes_cli_good_profile_is_quiet(monkeypatch, caplog):
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(
        crc,
        "AGENT_CLI_CMD",
        'hermes chat -Q --source tool --max-turns 60 -q "{message}"',
    )
    monkeypatch.setenv("HERMES_HOME", "/home/openclaw/.hermes/profiles/daily")

    crc._warn_if_agent_entry_may_drift()

    assert "wrap {message}" not in caplog.text
    assert "without HERMES_HOME" not in caplog.text
    assert "Very small turn" not in caplog.text


def test_prepare_cli_preserves_message_with_quotes(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command('say "hello" now')

    assert cmd == ["mycli", "ask", 'say "hello" now']


def test_prepare_cli_appends_image_path_when_template_has_no_image_slot(monkeypatch, tmp_path):
    image_path = str(tmp_path / "photo.jpg")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("look at this", image_paths=[image_path])

    assert cmd[:2] == ["mycli", "ask"]
    assert "look at this" in cmd[2]
    assert image_path in cmd[2]


def test_prepare_cli_uses_image_path_template(monkeypatch, tmp_path):
    image_path = str(tmp_path / "photo.jpg")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask --image "{image_path}" "{message}"')
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    cmd = crc._prepare_cli_command("look at this", image_paths=[image_path])

    assert cmd == ["mycli", "ask", "--image", image_path, "look at this"]


def test_cli_nonzero_exit_fails_even_with_stdout(monkeypatch):
    class _Result:
        returncode = 2
        stdout = "stale text that must not be posted"
        stderr = "bad command"

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None: ["mycli", "ask", message])
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _Result())

    with pytest.raises(RuntimeError, match="cli agent exited 2"):
        crc.call_agent_cli("hi")


def test_openai_http_protocol_uses_session_headers(monkeypatch):
    captured = {}

    class _Resp:
        headers = {"X-Hermes-Session-Id": "sess_new"}
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "choices": [
                    {"message": {"content": "real reply"}}
                ]
            }

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    saved = []
    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "AGENT_HTTP_MODEL", "hermes-agent")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc", "user_pk": None, "enclave_pk": None})
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "sess_old")
    monkeypatch.setattr(crc, "_save_agent_session_id", lambda sid: saved.append(sid))
    monkeypatch.setattr(crc.httpx, "post", _post)

    assert crc.call_agent_http("hi") == "real reply"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["headers"]["X-Hermes-Session-Id"] == "sess_old"
    assert captured["headers"]["X-Hermes-Session-Key"] == "feedling:usr_abc"
    assert saved == ["sess_new"]


def test_openai_http_protocol_sends_multimodal_image_block(monkeypatch):
    captured = {}

    class _Resp:
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content": "vision reply"}}]}

    def _post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc.httpx, "post", _post)

    image = {"data_url": "data:image/jpeg;base64,abcd", "data": "abcd", "mime_type": "image/jpeg"}

    assert crc.call_agent_http("see image", images=[image]) == "vision reply"
    content = captured["json"]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "see image"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcd"}}


def test_process_proactive_wake_routes_through_agent_and_posts_metadata(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {}

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return ["我看到了这个时机。"]

    def _post(reply, **kwargs):
        captured["reply"] = reply
        captured["post_kwargs"] = kwargs
        return {"id": "msg_1"}

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", _post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured.setdefault("statuses", []).append(
            (job_id, status, reason, kwargs)
        ),
    )
    monkeypatch.setattr(
        crc,
        "_screen_context_for_frame_ids",
        lambda frame_ids: ("screen: user is reading docs", [{"data": "x"}], ["/tmp/frame.jpg"]),
    )
    monkeypatch.setattr(
        crc,
        "recent_chat_context_for_proactive",
        lambda limit=None: "- user: 你刚刚问我这段要不要压成一句话。\n- agent: 我可以帮你压。",
    )

    job = {
        "schema_version": 2,
        "job_id": "pj_1",
        "wake_id": "wake_1",
        "gate_decision_id": "gd_1",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 123.0,
        "trigger": "screen_tick",
        "wake_kind": "screen",
        "user_state": "default",
        "ai_state": "present",
        "broadcast_state": "on",
        "current_app": "Docs",
        "frame_ids": ["frame_1"],
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(123.0)
    assert "Feedling proactive wake" in captured["message"]
    assert "platform did not judge" in captured["message"].lower()
    assert "wake_id: wake_1" in captured["message"]
    assert "wake_kind: screen" in captured["message"]
    assert "recent_chat_context" in captured["message"]
    assert "你刚刚问我这段要不要压成一句话" in captured["message"]
    assert "own runtime identity" in captured["message"]
    assert "screen: user is reading docs" in captured["message"]
    assert "possible_connections" not in captured["message"]
    assert captured["images"] == [{"data": "x"}]
    assert captured["image_paths"] == ["/tmp/frame.jpg"]
    assert captured["reply"] == "我看到了这个时机。"
    assert captured["post_kwargs"] == {
        "source": crc.PROACTIVE_JOB_SOURCE,
        "gate_decision_id": "gd_1",
        "proactive_job_id": "pj_1",
    }
    assert any(s[:3] == ("pj_1", "realizing", "") for s in captured["statuses"])
    assert any(s[0] == "pj_1" and s[1] == "posted" for s in captured["statuses"])


def test_process_proactive_v2_wake_routes_without_gate_judgment(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"posted": [], "statuses": []}

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        captured["images"] = images
        captured["image_paths"] = image_paths
        return {"messages": ["第一条", "第二条"]}

    def _post(reply, **kwargs):
        captured["posted"].append((reply, kwargs))
        return {"id": f"msg_{len(captured['posted'])}"}

    def _status(job_id, status, reason="", **kwargs):
        captured["statuses"].append((job_id, status, reason, kwargs))

    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", _post)
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(crc, "update_proactive_job_status", _status)
    monkeypatch.setattr(
        crc,
        "_screen_context_for_frame_ids",
        lambda frame_ids: ("screen: user is reading docs", [{"data": "x"}], ["/tmp/frame.jpg"]),
    )
    monkeypatch.setattr(
        crc,
        "recent_chat_context_for_proactive",
        lambda limit=None: "- user: 刚刚聊过这个问题。",
    )

    job = {
        "schema_version": 2,
        "job_id": "pj_v2",
        "wake_id": "wake_1",
        "gate_decision_id": "gd_1",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 124.0,
        "trigger": "screen_tick",
        "manual": False,
        "forced": False,
        "user_state": "default",
        "ai_state": "present",
        "broadcast_state": "on",
        "current_app": "xhs",
        "frame_ids": ["frame_1"],
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(124.0)
    assert "Feedling proactive wake" in captured["message"]
    assert "platform did not judge" in captured["message"].lower()
    assert "awareness / presence check" in captured["message"]
    assert "genuinely want to appear" in captured["message"]
    assert "Feedling Gate decided" not in captured["message"]
    assert "possible_connections" not in captured["message"]
    assert "wake_id: wake_1" in captured["message"]
    assert "wake_kind: screen" in captured["message"]
    assert "user_state: default" in captured["message"]
    assert "broadcast_state: on" in captured["message"]
    assert "screen_context_available: true" in captured["message"]
    assert captured["images"] == [{"data": "x"}]
    assert captured["image_paths"] == ["/tmp/frame.jpg"]
    assert [p[0] for p in captured["posted"]] == ["第一条", "第二条"]
    assert all(p[1]["source"] == crc.PROACTIVE_JOB_SOURCE for p in captured["posted"])
    assert sum(1 for s in captured["statuses"] if s[1] == "posted") == 2


def test_process_proactive_v2_sleep_marks_completed_without_post(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"statuses": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "actions": [{"type": "proactive.sleep", "reason": "not helpful"}],
            "messages": [],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda *args, **kwargs: captured.setdefault("posted", []).append(args))
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_sleep",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 125.0,
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(125.0)
    assert captured.get("posted") is None
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][3]["extra"]["agent_action"] == "sleep"
    assert completed[-1][3]["extra"]["wake_result"] == "sleep"


def test_process_proactive_v2_request_broadcast_posts_visible_request(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"posted": [], "statuses": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "actions": [{
                "type": "proactive.request_broadcast",
                "reason": "screen sharing is off",
                "copy": "我现在看不到你的屏幕，可以重新打开屏幕共享吗？",
            }],
            "messages": [],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_b"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_broadcast",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 126.0,
        "broadcast_state": "off",
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(126.0)
    assert captured["posted"][0][0] == "我现在看不到你的屏幕，可以重新打开屏幕共享吗？"
    posted = [s for s in captured["statuses"] if s[1] == "posted"]
    assert posted
    assert posted[-1][3]["extra"]["agent_action"] == "request_broadcast"
    assert posted[-1][3]["extra"]["wake_result"] == "posted"


def test_resident_v2_context_matches_hosted_catalog_for_same_wake(monkeypatch):
    from proactive.adapters_v2 import wake_event_v2_from_legacy_job
    from proactive.agent_protocol_v2 import build_agent_context_v2
    from proactive.runtime_v2 import merge_wakes_v2
    from proactive.tool_catalog_v2 import tool_catalog_v2_for_runtime

    monkeypatch.setitem(crc._whoami_cache, "user_id", "usr_resident_v2")
    job = {
        "schema_version": 2,
        "job_id": "pj_equiv",
        "wake_id": "wake_equiv",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 100.0,
        "trigger": "scheduled_wake",
        "change_digest": "check whether she left",
        "scheduled_note": "check whether she left",
        "timezone": "Asia/Shanghai",
        "origin_refs": ["msg_1"],
    }

    resident = crc._resident_v2_agent_context_for_job(job)
    event = wake_event_v2_from_legacy_job("usr_resident_v2", job)
    hosted = build_agent_context_v2(
        merge_wakes_v2([event], tool_catalog=tool_catalog_v2_for_runtime("hosted")),
        recent_chat=[],
    )

    assert resident["trigger"] == hosted["trigger"] == "scheduled_wake"
    assert resident["scheduled_note"] == hosted["scheduled_note"] == "check whether she left"
    assert resident["origin_refs"] == hosted["origin_refs"] == ["msg_1"]
    assert resident["tools"] == hosted["tools"]
    assert ("schedule_wake", "action", "fast") in tool_catalog_v2_for_runtime("resident").signature()
    assert tool_catalog_v2_for_runtime("resident").signature() == tool_catalog_v2_for_runtime("hosted").signature()


def test_process_proactive_runtime_v2_flag_skips_legacy_prompt_and_posts_send_message(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"posted": [], "statuses": []}

    def _legacy_prompt(*_args, **_kwargs):
        raise AssertionError("legacy proactive prompt must be unreachable for runtime v2 jobs")

    def _agent(message, images=None, image_paths=None):
        captured["message"] = message
        return {"actions": [{"type": "send_message", "text": "V2 action bubble"}], "messages": []}

    monkeypatch.setattr(crc, "_message_for_proactive_job", _legacy_prompt)
    monkeypatch.setattr(crc, "call_agent", _agent)
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg_v2"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_runtime_v2",
        "wake_id": "wake_runtime_v2",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 127.0,
        "trigger": "heartbeat_broadcast_on",
        "runtime_v2": {crc.RESIDENT_WAKE_RUNTIME_V2_FLAG: True},
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(127.0)
    assert "Feedling Runtime V2 proactive wake" in captured["message"]
    assert "v2_context_json" in captured["message"]
    assert "Allowed actions" in captured["message"]
    assert "Feedling proactive wake" not in captured["message"]
    assert captured["posted"][0][0] == "V2 action bubble"
    assert captured["posted"][0][1]["proactive_job_id"] == "pj_runtime_v2"
    assert any(s[0] == "pj_runtime_v2" and s[1] == "posted" for s in captured["statuses"])


def test_process_proactive_runtime_v2_schedule_action_calls_backend_without_chat(monkeypatch):
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()

    captured = {"scheduled": [], "statuses": [], "posted": []}

    monkeypatch.setattr(
        crc,
        "call_agent",
        lambda message, images=None, image_paths=None: {
            "actions": [{
                "type": "schedule_wake",
                "at": "2030-01-01T09:30:00",
                "tz": "Asia/Shanghai",
                "note": "check in",
                "origin_refs": ["msg_1"],
            }],
            "messages": [],
        },
    )
    monkeypatch.setattr(
        crc,
        "execute_scheduled_wake_actions",
        lambda actions, job: captured["scheduled"].append((actions, job)) or {
            "results": [{"type": "schedule_wake_result", "status": "scheduled", "timer_id": "sched_1"}],
        },
    )
    monkeypatch.setattr(crc, "post_reply", lambda reply, **kwargs: captured["posted"].append((reply, kwargs)) or {"id": "msg"})
    monkeypatch.setattr(crc, "claim_proactive_job", lambda job_id: True)
    monkeypatch.setattr(
        crc,
        "update_proactive_job_status",
        lambda job_id, status, reason="", **kwargs: captured["statuses"].append((job_id, status, reason, kwargs)),
    )
    monkeypatch.setattr(crc, "_screen_context_for_frame_ids", lambda frame_ids: ("", [], []))
    monkeypatch.setattr(crc, "recent_chat_context_for_proactive", lambda limit=None: "")

    job = {
        "schema_version": 2,
        "job_id": "pj_schedule",
        "wake_id": "wake_schedule",
        "source": crc.PROACTIVE_JOB_SOURCE,
        "ts": 128.0,
        "trigger": "heartbeat_broadcast_on",
        "runtime_v2": {crc.RESIDENT_WAKE_RUNTIME_V2_FLAG: True},
    }

    assert crc._process_proactive_jobs([job]) == pytest.approx(128.0)
    assert captured["scheduled"][0][0][0]["type"] == "schedule_wake"
    assert captured["posted"] == []
    completed = [s for s in captured["statuses"] if s[1] == "completed"]
    assert completed
    assert completed[-1][2] == "agent_scheduled_wake_actions"
    assert completed[-1][3]["extra"]["wake_result"] == "action_only"


def test_normalize_agent_replies_supports_multiple_messages_with_cap(monkeypatch):
    monkeypatch.setattr(crc, "PROACTIVE_MAX_REPLY_MESSAGES", 5)

    raw = '{"messages":["第一条","第二条","第三条","第四条","第五条","第六条"]}'

    assert crc._normalize_agent_replies(raw) == ["第一条", "第二条", "第三条", "第四条", "第五条"]


def test_normalize_agent_replies_extracts_content_field():
    raw = '{"content":"正常应该只显示 content 里的文字。","content_type":"text"}'

    assert crc._normalize_agent_replies(raw) == ["正常应该只显示 content 里的文字。"]


def test_normalize_agent_replies_unwraps_claude_result_content_json():
    raw = json.dumps(
        {
            "type": "result",
            "session_id": "123e4567-e89b-12d3-a456-426614174000",
            "result": json.dumps(
                {
                    "content": "Claude Code 内层 JSON 也应该只显示这句。",
                    "content_type": "text",
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )

    assert crc._normalize_agent_replies(raw) == ["Claude Code 内层 JSON 也应该只显示这句。"]


def test_agent_turn_classifies_runtime_json_without_leaking_debug():
    raw = json.dumps(
        {
            "type": "result",
            "session_id": "sess_should_not_render",
            "result": "这是用户应该看到的最终回复。",
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "inputTokens": 8,
                    "outputTokens": 305,
                    "costUSD": 0.16,
                }
            },
            "terminal_reason": "completed",
            "permission_denials": [],
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["这是用户应该看到的最终回复。"]
    assert "modelUsage" in turn.runtime_debug
    assert "terminal_reason" in turn.runtime_debug
    assert "modelUsage" not in turn.messages[0]
    assert "permission_denials" not in turn.messages[0]


def test_agent_turn_extracts_visible_thinking_summary_from_nested_result():
    raw = json.dumps(
        {
            "type": "result",
            "uuid": "43d846de-9d36-4943-a832-23e0650ef6e8",
            "result": json.dumps(
                {
                    "reply": "我只显示最终回复。",
                    "thinking_summary": "参考了最近对话。\n整理了可见上下文。",
                    "modelUsage": {"debug": True},
                },
                ensure_ascii=False,
            ),
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["我只显示最终回复。"]
    assert turn.thinking_summary == "参考了最近对话。\n整理了可见上下文。"
    assert "uuid" in turn.runtime_debug


def test_agent_turn_extracts_provider_reasoning_metadata_from_nested_result():
    raw = json.dumps(
        {
            "reply": "最终回复。",
            "provider_reasoning": "Provider returned this display-safe reasoning.",
            "reasoning_kind": "provider_reasoning",
            "reasoning_source": "anthropic",
            "reasoning_model": "claude-sonnet-4.5",
            "reasoning_native": True,
        },
        ensure_ascii=False,
    )

    turn = crc._split_agent_turn(raw)

    assert turn.messages == ["最终回复。"]
    assert turn.thinking_summary == "Provider returned this display-safe reasoning."
    assert turn.thinking_kind == "provider_reasoning"
    assert turn.thinking_source == "anthropic"
    assert turn.thinking_model == "claude-sonnet-4.5"
    assert turn.thinking_native is True


def test_extract_cli_output_preserves_structured_multi_messages():
    raw = '{"messages":["第一条","第二条"]}'

    extracted = crc._extract_text_from_cli_output(raw)

    assert crc._normalize_agent_replies(extracted) == ["第一条", "第二条"]


def test_message_for_proactive_job_instructs_multi_bubble_without_gate_context():
    job = {
        "schema_version": 2,
        "wake_id": "wake_test",
        "trigger": "screen_tick",
        "wake_kind": "screen",
        "frame_ids": ["frame_1"],
    }

    message = crc._message_for_proactive_job(
        job,
        screen_text="screen: dense paragraph",
        recent_chat_context="- user: 这段帮我看一下",
    )

    assert "1-5 short chat bubbles" in message
    assert '{"messages":["...","..."]}' in message
    assert "recent_chat_context" in message
    assert "possible_connections" not in message
    assert "Feedling Gate decided" not in message
    assert "screen: dense paragraph" in message


def test_recent_chat_context_defaults_to_twenty_messages(monkeypatch):
    captured = {}
    now = 1780939000.0

    def _history(since, limit):
        captured["limit"] = limit
        return [
            {"role": "user", "content": f"用户消息 {idx}", "ts": now - ((24 - idx) * 60)}
            for idx in range(25)
        ]

    monkeypatch.setattr(crc, "PROACTIVE_RECENT_CHAT_LIMIT", 20)
    monkeypatch.setattr(crc.time, "time", lambda: now)
    monkeypatch.setattr(crc, "get_decrypted_history", _history)

    context = crc.recent_chat_context_for_proactive()

    assert captured["limit"] == 50
    assert context.freshness == "fresh"
    assert context.included_count == 20
    assert context.text.count("- [") == 20
    assert "fresh] user: 用户消息 5" in context.text
    assert "fresh] user: 用户消息 24" in context.text
    assert context.last_user_message_age_sec == pytest.approx(0.0)


def test_recent_chat_context_stale_falls_back_to_two_timestamped_messages(monkeypatch):
    now = 1780939000.0

    def _history(since, limit):
        return [
            {"role": "user", "content": f"旧消息 {idx}", "ts": now - 28800 - ((4 - idx) * 60)}
            for idx in range(5)
        ]

    monkeypatch.setattr(crc, "PROACTIVE_RECENT_CHAT_LIMIT", 20)
    monkeypatch.setattr(crc, "PROACTIVE_CHAT_FRESH_WINDOW_SEC", 21600)
    monkeypatch.setattr(crc, "PROACTIVE_STALE_CHAT_FALLBACK_LIMIT", 2)
    monkeypatch.setattr(crc.time, "time", lambda: now)
    monkeypatch.setattr(crc, "get_decrypted_history", _history)

    context = crc.recent_chat_context_for_proactive()

    assert context.freshness == "stale"
    assert context.included_count == 2
    assert context.text.count("- [") == 2
    assert "stale] user: 旧消息 3" in context.text
    assert "stale] user: 旧消息 4" in context.text
    assert "旧消息 2" not in context.text
    assert "8h" in context.text


def test_proactive_tick_cadence_follows_broadcast_state(monkeypatch):
    monkeypatch.setattr(crc, "PROACTIVE_TICK_BROADCAST_ON_INTERVAL_SEC", 300)
    monkeypatch.setattr(crc, "PROACTIVE_TICK_BROADCAST_OFF_INTERVAL_SEC", 1800)

    assert crc._proactive_tick_trigger_for_broadcast_state("off") == "heartbeat_broadcast_off"
    assert crc._proactive_tick_interval_for_broadcast_state("off") == 1800
    assert crc._proactive_tick_trigger_for_broadcast_state("on") == "heartbeat_broadcast_on"
    assert crc._proactive_tick_interval_for_broadcast_state("on") == 300
    assert crc._proactive_tick_trigger_for_broadcast_state("") == "heartbeat_unknown"
    assert crc._proactive_tick_interval_for_broadcast_state("") == 300


def test_post_proactive_reply_triggers_alert_and_live_activity(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "msg_1"}

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(crc, "_ENCRYPTION_AVAILABLE", True)
    monkeypatch.setattr(
        crc,
        "_whoami_cache",
        {"user_id": "usr_abc", "user_pk": b"u" * 32, "enclave_pk": b"e" * 32},
    )
    monkeypatch.setattr(
        crc,
        "_build_envelope",
        lambda **kwargs: {"v": 1, "id": "env_1", "visibility": kwargs["visibility"]},
    )
    monkeypatch.setattr(crc, "_load_whoami", lambda: True)
    monkeypatch.setattr(crc.httpx, "post", _post)

    crc.post_reply(
        "我看到了这个时机。",
        source=crc.PROACTIVE_JOB_SOURCE,
        gate_decision_id="gd_1",
        proactive_job_id="pj_1",
    )

    body = captured["json"]
    assert body["source"] == crc.PROACTIVE_JOB_SOURCE
    assert body["gate_decision_id"] == "gd_1"
    assert body["proactive_job_id"] == "pj_1"
    assert body["alert_body"] == "我看到了这个时机。"
    assert body["push_live_activity"] is True
    assert body["push_body"] == "我看到了这个时机。"
    assert body["data"] == {
        "source": crc.PROACTIVE_JOB_SOURCE,
        "gate_decision_id": "gd_1",
        "proactive_job_id": "pj_1",
    }


# ---------------------------------------------------------------------------
# Task 5 (D11): resident V2 proactive tool loop
# ---------------------------------------------------------------------------

def test_resident_proactive_runs_tool_loop(monkeypatch):
    """_resident_run_agent_v2 should run through the tool loop:
    turn1 → tool_call(screen.read), turn2 → terminal message.
    The tool result (caption) must be fed back into the second call.
    """
    scripted = [
        '{"tool_calls": [{"name": "screen.read", "args": {}}]}',
        '{"messages": ["ok"]}',
    ]
    fed_back: list[str] = []

    def fake_call_agent(message, **kw):
        fed_back.append(message)
        return scripted.pop(0)

    posted: list[tuple[str, dict]] = []

    def fake_call_tool(name, args):
        posted.append((name, dict(args or {})))
        return {
            "name": name,
            "ok": True,
            "outcome": "ok",
            "result": {"caption": "Inbox"},
            "error_code": "",
            "needs_background": False,
        }

    monkeypatch.setattr(crc, "call_agent", fake_call_agent)
    monkeypatch.setattr(crc, "_resident_call_tool_v2", fake_call_tool)

    final = crc._resident_run_agent_v2("system+context message")

    assert json.loads(final)["messages"] == ["ok"]
    assert posted == [("screen.read", {})]
    assert any("Inbox" in m for m in fed_back), f"caption not fed back; fed_back={fed_back}"


def test_resident_proactive_tool_loop_no_python_repr_on_multi_round():
    """_resident_run_agent_v2 with a dict-returning call_agent (real HTTP mode
    shape) across TWO tool rounds must never let a Python repr like
    {'tool_calls': [...]} appear in the transcript text fed to the agent.

    Regression: before Fix 1, str(dict) was used instead of json.dumps, so
    round-2+ history arrived as Python repr, garbling the agent's context.
    """
    # Scripted agent responses — dicts (real HTTP mode shape), not strings.
    scripted = [
        {"tool_calls": [{"name": "screen.read", "args": {}}]},
        {"tool_calls": [{"name": "memory.fetch", "args": {"ids": ["m1"]}}]},
        {"messages": ["done"]},
    ]
    fed_texts: list[str] = []

    def fake_call_agent_dict(message, **kw):
        fed_texts.append(message)
        return scripted.pop(0)

    def fake_call_tool_multi(name, args):
        return {
            "name": name,
            "ok": True,
            "outcome": "ok",
            "result": {"data": f"result_of_{name}"},
            "error_code": "",
            "needs_background": False,
        }

    import unittest.mock as _mock
    with _mock.patch.object(crc, "call_agent", fake_call_agent_dict), \
         _mock.patch.object(crc, "_resident_call_tool_v2", fake_call_tool_multi):
        final = crc._resident_run_agent_v2("initial prompt")

    # Terminal return must still be the raw dict from call_agent, not a string.
    assert isinstance(final, dict)
    assert final.get("messages") == ["done"]

    # At least 3 calls were made (2 tool rounds + 1 terminal).
    assert len(fed_texts) >= 3, f"expected >=3 calls but got {len(fed_texts)}: {fed_texts}"

    # Round 2 and round 3 texts must NOT contain Python-repr dicts.
    for round_idx, text in enumerate(fed_texts[1:], start=2):
        assert "{'tool_calls'" not in text, (
            f"Round {round_idx} contains Python repr: {text!r}"
        )
        assert "{'messages'" not in text, (
            f"Round {round_idx} contains Python repr: {text!r}"
        )
        # The assistant-turn section embedded in the transcript must be
        # valid JSON (parseable with json.loads) — not Python repr.
        # Each "\n\n"-separated block is either the user prompt or an
        # assistant/tool turn; we check that none of the non-initial
        # blocks that start with '{' are Python repr.
        sections = text.split("\n\n")
        for section in sections:
            stripped = section.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise AssertionError(
                        f"Round {round_idx} section is not valid JSON (likely Python repr): "
                        f"{stripped!r}"
                    ) from exc


# ---------------------------------------------------------------------------
# Fix round 2: tool_calls survives the real call_agent normalizer (masked-bug
# regression). Monkeypatches ONLY the transport layer (call_agent_http), NOT
# call_agent itself — so the fix is proven end-to-end through the normalizer.
# ---------------------------------------------------------------------------

def test_tool_calls_survive_real_call_agent_normalizer(monkeypatch):
    """Regression for the D11 resident Critical: when the model returns ONLY
    tool_calls (no messages/actions), call_agent used to raise
    ValueError('agent produced no usable reply after sanitization') because
    AgentTurn had no tool_calls field.  This test drives the loop through the
    REAL call_agent (only call_agent_http is monkeypatched at the transport
    boundary) to prove that tool_calls now survive the normalizer and reach
    run_tool_loop_v2.
    """
    # Transport layer returns raw dicts: turn 1 = tool-call only, turn 2 = terminal.
    http_responses = [
        {"tool_calls": [{"name": "screen.read", "args": {}}]},
        {"messages": ["done"]},
    ]
    http_call_count = []

    def _fake_call_agent_http(message, images=None):
        body = http_responses[len(http_call_count)]
        http_call_count.append(1)
        return body

    tool_calls_seen: list[tuple[str, dict]] = []

    def _fake_call_tool(name, args):
        tool_calls_seen.append((name, dict(args or {})))
        return {
            "name": name,
            "ok": True,
            "outcome": "ok",
            "result": {"caption": "InboxScreen"},
            "error_code": "",
            "needs_background": False,
        }

    monkeypatch.setattr(crc, "AGENT_MODE", "http")
    # Monkeypatch the transport, NOT call_agent — session management stays real.
    monkeypatch.setattr(crc, "call_agent_http", _fake_call_agent_http)
    monkeypatch.setattr(crc, "_resident_call_tool_v2", _fake_call_tool)

    # Drive through the REAL call_agent — not monkeypatched.
    final = crc._resident_run_agent_v2("test message")

    # The tool must have been executed exactly once with correct name.
    assert tool_calls_seen == [("screen.read", {})], (
        f"Expected tool call screen.read but got: {tool_calls_seen}"
    )

    # The final result must reflect the terminal turn {"messages": ["done"]}.
    # parse_agent_response_v2 accepts a Mapping, so final (a dict) is fine.
    from proactive.agent_protocol_v2 import parse_agent_response_v2
    parsed = parse_agent_response_v2(final)
    assert "done" in parsed.messages, (
        f"Terminal message 'done' not found in parsed.messages={parsed.messages}; final={final!r}"
    )


def test_normalizer_keeps_tool_only_json_wrapped_in_log_text():
    """A tool-only JSON line wrapped in CLI log/header text must NOT be
    discarded as a plain message — tool_calls must survive normalization."""
    raw = 'INFO starting agent\n{"tool_calls": [{"name": "screen.read", "args": {}}]}\nINFO done'
    turn = crc._agent_turn_from_obj(raw)
    assert [tc["name"] for tc in turn.tool_calls] == ["screen.read"]
    # The raw JSON line must not also leak through as a junk chat message.
    assert turn.messages == []


def test_cli_tool_only_output_preserves_tool_calls(monkeypatch):
    """AGENT_MODE=cli: stdout that is a tool-only JSON line surrounded by log
    text must yield tool_calls (not be flattened into a plain message)."""
    class _Result:
        returncode = 0
        stdout = 'INFO hermes booting\n{"tool_calls": [{"name": "screen.read", "args": {}}]}\n'
        stderr = ""

    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'mycli ask "{message}"')
    monkeypatch.setattr(crc, "_prepare_cli_command", lambda message, image_paths=None: ["mycli", "ask", message])
    monkeypatch.setattr(crc.subprocess, "run", lambda *a, **kw: _Result())

    result = crc.call_agent_cli("hi")
    turn = crc._agent_turn_from_raw(result)
    assert [tc["name"] for tc in turn.tool_calls] == ["screen.read"]


def test_openai_http_tool_only_response_preserved(monkeypatch):
    """AGENT_MODE=http openai protocol: a tool-only model reply must be returned
    as structured output (not raise 'no usable reply text')."""
    class _Resp:
        headers = {}
        def raise_for_status(self):
            pass
        def json(self):
            return {"choices": [{"message": {"content":
                    '{"tool_calls": [{"name": "screen.read", "args": {}}]}'}}]}

    monkeypatch.setattr(crc, "AGENT_HTTP_URL", "http://127.0.0.1:8642/v1/chat/completions")
    monkeypatch.setattr(crc, "AGENT_HTTP_PROTOCOL", "openai")
    monkeypatch.setattr(crc, "AGENT_HTTP_MODEL", "hermes-agent")
    monkeypatch.setattr(crc, "_whoami_cache", {"user_id": "usr_abc"})
    monkeypatch.setattr(crc, "_load_agent_session_id", lambda: "")
    monkeypatch.setattr(crc, "_save_agent_session_id", lambda sid: None)
    monkeypatch.setattr(crc.httpx, "post", lambda *a, **kw: _Resp())

    result = crc.call_agent_http("hi")
    turn = crc._agent_turn_from_raw(result)
    assert [tc["name"] for tc in turn.tool_calls] == ["screen.read"]

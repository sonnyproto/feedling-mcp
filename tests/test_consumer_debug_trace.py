"""
Tests for the DebugConsole M1 consumer additions in tools/chat_resident_consumer.py:
- `_emit_debug_trace` (fire-and-forget flow-trace emit, offloaded to a daemon thread)
- `_post_debug_trace_event` (the actual network call, run on that thread)
- `_cli_turn_metrics` (driver-aware metric extraction, reusable by `_log_cli_turn_timing`)

Run with: pytest tests/test_consumer_debug_trace.py -v
"""

import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — same pattern as tests/test_chat_resident_consumer.py.
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

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


@pytest.fixture(autouse=True)
def _reset_debug_trace_cache():
    """The `_DBG_TRACE_ENABLED` module-level cache must not leak between
    tests — each test seeds it explicitly to exercise a specific state."""
    crc._DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}
    yield
    crc._DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}


def _seed_cache(val, ttl=999.0):
    crc._DBG_TRACE_ENABLED = {"val": val, "exp": time.monotonic() + ttl}


# ---------------------------------------------------------------------------
# _cli_turn_metrics — claude driver
# ---------------------------------------------------------------------------

def test_cli_turn_metrics_claude():
    stdout = json.dumps({
        "type": "result",
        "duration_ms": 1200,
        "duration_api_ms": 800,
        "num_turns": 3,
    }) + "\n"
    result = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    m = crc._cli_turn_metrics(["claude", "--output-format", "json"], result, 1500)

    assert m["driver"] == "claude"
    assert m["rc"] == 0
    assert m["wall_ms"] == 1500
    assert m["agent_ms"] == 1200
    assert m["api_ms"] == 800
    assert m["num_turns"] == 3
    assert m["out_chars"] == len(stdout)


# ---------------------------------------------------------------------------
# _cli_turn_metrics — codex driver
# ---------------------------------------------------------------------------

def test_cli_turn_metrics_codex_does_not_raise():
    stdout = json.dumps({"type": "agent_message"}) + "\n"
    result = subprocess.CompletedProcess(args=["codex", "exec", "--json"], returncode=0, stdout=stdout, stderr="")

    m = crc._cli_turn_metrics(["codex", "exec", "--json"], result, 900)

    assert m["driver"] == "codex"
    assert m["rc"] == 0
    assert m["wall_ms"] == 900
    # codex has no duration fields — these must remain None, never raise.
    assert m["agent_ms"] is None
    assert m["api_ms"] is None


def test_cli_turn_metrics_never_raises_on_garbage_stdout():
    result = subprocess.CompletedProcess(args=["claude"], returncode=1, stdout="not json at all", stderr="boom")
    m = crc._cli_turn_metrics(["claude"], result, 42)
    assert m["driver"] == "claude"
    assert m["rc"] == 1
    assert m["wall_ms"] == 42


# ---------------------------------------------------------------------------
# _post_debug_trace_event — the actual network call (runs on the daemon
# thread spawned by _emit_debug_trace). Tested synchronously/directly so
# payload/timeout assertions don't depend on thread timing.
# ---------------------------------------------------------------------------

def test_post_debug_trace_event_swallows_errors():
    with patch.object(crc._HTTP, "post", side_effect=RuntimeError("boom")):
        assert crc._post_debug_trace_event({"event": {}}) is None


def test_post_debug_trace_event_posts_expected_payload():
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout

    payload = {"event": {
        "subsystem": "agent", "type": "turn.done", "status": "ok",
        "summary": "s", "explain": "e", "detail": {"a": 1},
        "content_excerpt": {"c": "d"}, "trace_id": "t-1",
        "turn_id": "t-1", "actor": "vps_resident", "dur_ms": 12.5,
    }}

    with patch.object(crc._HTTP, "post", side_effect=_fake_post):
        crc._post_debug_trace_event(payload)

    assert captured["url"] == f"{crc.FEEDLING_API_URL}/v1/debug/trace/event"
    event = captured["json"]["event"]
    assert event["subsystem"] == "agent"
    assert event["type"] == "turn.done"
    assert event["status"] == "ok"
    assert event["summary"] == "s"
    assert event["explain"] == "e"
    assert event["detail"] == {"a": 1}
    assert event["content_excerpt"] == {"c": "d"}
    assert event["trace_id"] == "t-1"
    assert event["turn_id"] == "t-1"
    assert event["actor"] == "vps_resident"
    assert event["dur_ms"] == 12.5
    assert captured["timeout"] == 2


# ---------------------------------------------------------------------------
# _emit_debug_trace — dispatches to a daemon thread, returns immediately,
# never raises even if the network call blows up.
# ---------------------------------------------------------------------------

def test_emit_debug_trace_returns_immediately_and_never_raises():
    """`_emit_debug_trace` must not raise and must not block the calling
    thread, regardless of what happens inside `_post_debug_trace_event` on
    the daemon thread (that function's own never-raises contract is covered
    by test_post_debug_trace_event_swallows_errors). Here we use a slow
    fake to prove dispatch is instant — the caller does not wait on the
    network call at all."""
    _seed_cache(True)  # fresh + enabled, so a thread is spawned
    done = threading.Event()

    def _slow_post(payload):
        time.sleep(1.0)
        done.set()

    with patch.object(crc, "_post_debug_trace_event", side_effect=_slow_post):
        start = time.monotonic()
        result = crc._emit_debug_trace("agent", "x")
        elapsed = time.monotonic() - start
        assert not done.is_set(), "emit must return before the slow post completes"
        assert done.wait(timeout=3), "background thread did not run within timeout"

    assert result is None
    # Should return essentially instantly — dispatch only, no network wait.
    assert elapsed < 0.5


# ---------------------------------------------------------------------------
# agent.reply — the M2 "clean parsed reply + thinking excerpt" event emitted
# right after `_split_agent_turn` in `_process_messages`. We don't drive the
# full message loop here (too heavy to unit-test); instead we build an
# `AgentTurn` the same way `_process_messages` does and emit the same event
# shape, verifying the posted payload actually carries the clean reply text
# and thinking summary (not just a raw stdout head).
# ---------------------------------------------------------------------------

def test_agent_reply_trace_event_carries_clean_reply_and_thinking():
    _seed_cache(True)  # fresh + enabled, so _post_debug_trace_event actually fires
    turn = crc.AgentTurn(messages=["hello world"], thinking_summary="let me think")

    captured = {}
    done = threading.Event()

    def _fake_post(payload):
        captured["payload"] = payload
        done.set()

    with patch.object(crc, "_post_debug_trace_event", side_effect=_fake_post):
        reply_text = "\n\n".join(m for m in turn.messages if isinstance(m, str) and m.strip())
        crc._emit_debug_trace(
            "agent", "agent.reply", trace_id="t-1",
            summary=f"reply parsed ({len(turn.messages)} msg)",
            explain=("回复已解析：" + f"{len(turn.messages)} 段"
                     + ("，含思考摘要" if turn.thinking_summary else "，无思考摘要")),
            detail={"n_messages": len(turn.messages), "n_actions": len(turn.actions),
                    "thinking_kind": turn.thinking_kind or "", "thinking_model": turn.thinking_model or ""},
            content_excerpt={"reply": reply_text[:3000], "thinking": (turn.thinking_summary or "")[:2000]},
        )
        assert done.wait(timeout=3), "background dispatch thread did not post within timeout"

    assert "payload" in captured
    event = captured["payload"]["event"]
    assert event["subsystem"] == "agent"
    assert event["type"] == "agent.reply"
    assert event["trace_id"] == "t-1"
    assert "hello world" in event["content_excerpt"]["reply"]
    assert "let me think" in event["content_excerpt"]["thinking"]
    assert event["detail"]["n_messages"] == 1


def test_emit_debug_trace_dispatches_to_daemon_thread_with_expected_payload():
    """The event is posted asynchronously on a background thread; join it
    with a short timeout to confirm it actually fires with the right shape."""
    _seed_cache(True)  # fresh + enabled, so a thread is spawned and posts
    recorded = {}
    done = threading.Event()

    def _fake_post_event(payload):
        recorded["payload"] = payload
        done.set()

    with patch.object(crc, "_post_debug_trace_event", side_effect=_fake_post_event):
        crc._emit_debug_trace(
            "agent", "turn.done", status="ok", summary="s", explain="e",
            detail={"a": 1}, content_excerpt={"c": "d"}, trace_id="t-1", dur_ms=12.5,
        )
        assert done.wait(timeout=2), "background thread did not post within timeout"

    event = recorded["payload"]["event"]
    assert event["subsystem"] == "agent"
    assert event["type"] == "turn.done"
    assert event["status"] == "ok"
    assert event["summary"] == "s"
    assert event["explain"] == "e"
    assert event["detail"] == {"a": 1}
    assert event["content_excerpt"] == {"c": "d"}
    assert event["trace_id"] == "t-1"
    assert event["turn_id"] == "t-1"
    assert event["actor"] == "vps_resident"
    assert event["dur_ms"] == 12.5


def test_emit_debug_trace_swallows_thread_spawn_failure():
    """Even if threading.Thread.start() itself raises, _emit_debug_trace
    must not propagate the exception."""
    _seed_cache(True)  # fresh + enabled, so we actually attempt to spawn
    with patch.object(crc.threading, "Thread", side_effect=RuntimeError("boom")):
        assert crc._emit_debug_trace("agent", "x") is None


# ---------------------------------------------------------------------------
# TTL-cached enabled gate — the zero-cost off path, and the refresh path.
# ---------------------------------------------------------------------------

def test_emit_debug_trace_fresh_disabled_dispatches_nothing():
    """Warm cache says disabled → no thread spawned, no network at all."""
    _seed_cache(False)
    with patch.object(crc.threading, "Thread") as mock_thread, \
         patch.object(crc, "_post_debug_trace_event") as mock_post:
        result = crc._emit_debug_trace("agent", "x")

    assert result is None
    assert mock_thread.call_count == 0
    assert mock_post.call_count == 0


def test_emit_debug_trace_fresh_enabled_dispatches_and_posts():
    """Warm cache says enabled → a thread is spawned and it posts."""
    _seed_cache(True)
    done = threading.Event()
    recorded = {}

    def _fake_post_event(payload):
        recorded["payload"] = payload
        done.set()

    with patch.object(crc, "_post_debug_trace_event", side_effect=_fake_post_event):
        crc._emit_debug_trace("agent", "turn.done", trace_id="t-2")
        assert done.wait(timeout=2), "background thread did not post within timeout"

    assert recorded["payload"]["event"]["trace_id"] == "t-2"


def test_emit_debug_trace_stale_unknown_probes_and_posts_when_enabled():
    """Stale/unknown cache → try: refresh on the daemon thread, then post
    if the refreshed value is enabled."""
    crc._DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}  # stale/unknown
    done = threading.Event()
    get_calls = []
    post_calls = []

    def _fake_get(url, params=None, headers=None, timeout=None):
        get_calls.append(url)
        resp = types.SimpleNamespace()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"enabled": True, "deploy_enabled": True}
        return resp

    def _fake_post_event(payload):
        post_calls.append(payload)
        done.set()

    with patch.object(crc._HTTP, "get", side_effect=_fake_get), \
         patch.object(crc, "_post_debug_trace_event", side_effect=_fake_post_event):
        crc._emit_debug_trace("agent", "x")
        assert done.wait(timeout=2), "background thread did not complete within timeout"

    assert len(get_calls) == 1
    assert len(post_calls) == 1
    assert crc._DBG_TRACE_ENABLED["val"] is True


def test_refresh_debug_trace_enabled_caches_false_on_get_failure():
    """Any failure talking to the backend (network error, bad JSON, non-2xx)
    must be swallowed and cache False (fail-closed) — never raise."""
    crc._DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}
    with patch.object(crc._HTTP, "get", side_effect=RuntimeError("boom")):
        crc._refresh_debug_trace_enabled()  # must not raise

    assert crc._DBG_TRACE_ENABLED["val"] is False
    assert crc._DBG_TRACE_ENABLED["exp"] > time.monotonic()


def test_refresh_debug_trace_enabled_caches_true_when_both_flags_set():
    crc._DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}

    def _fake_get(url, params=None, headers=None, timeout=None):
        resp = types.SimpleNamespace()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"enabled": True, "deploy_enabled": True}
        return resp

    with patch.object(crc._HTTP, "get", side_effect=_fake_get):
        crc._refresh_debug_trace_enabled()

    assert crc._DBG_TRACE_ENABLED["val"] is True
    assert crc._DBG_TRACE_ENABLED["exp"] > time.monotonic()


def test_refresh_debug_trace_enabled_false_when_deploy_disabled():
    """Both the per-user gate AND the deploy kill-switch must be true."""
    crc._DBG_TRACE_ENABLED = {"val": None, "exp": 0.0}

    def _fake_get(url, params=None, headers=None, timeout=None):
        resp = types.SimpleNamespace()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"enabled": True, "deploy_enabled": False}
        return resp

    with patch.object(crc._HTTP, "get", side_effect=_fake_get):
        crc._refresh_debug_trace_enabled()

    assert crc._DBG_TRACE_ENABLED["val"] is False


def test_debug_trace_probably_enabled_stale_returns_unknown():
    crc._DBG_TRACE_ENABLED = {"val": True, "exp": 0.0}  # expired
    known, enabled = crc._debug_trace_probably_enabled()
    assert known is False
    assert enabled is False


def test_debug_trace_probably_enabled_fresh_returns_cached_value():
    _seed_cache(True)
    assert crc._debug_trace_probably_enabled() == (True, True)
    _seed_cache(False)
    assert crc._debug_trace_probably_enabled() == (True, False)


# ---------------------------------------------------------------------------
# _log_cli_turn_timing — regression: still logs, delegates to _cli_turn_metrics
# ---------------------------------------------------------------------------

def test_log_cli_turn_timing_calls_cli_turn_metrics():
    stdout = json.dumps({
        "type": "result",
        "duration_ms": 500,
        "duration_api_ms": 300,
        "num_turns": 1,
    }) + "\n"
    result = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=stdout, stderr="")

    with patch.object(crc, "_cli_turn_metrics", wraps=crc._cli_turn_metrics) as mock_metrics:
        crc._log_cli_turn_timing(["claude"], result, 700)

    mock_metrics.assert_called_once_with(["claude"], result, 700)

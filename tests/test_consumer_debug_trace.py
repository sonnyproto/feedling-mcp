"""
Tests for the DebugConsole M1 consumer additions in tools/chat_resident_consumer.py:
- `_emit_debug_trace` (fire-and-forget flow-trace emit)
- `_cli_turn_metrics` (driver-aware metric extraction, reusable by `_log_cli_turn_timing`)

Run with: pytest tests/test_consumer_debug_trace.py -v
"""

import json
import os
import subprocess
import sys
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
# _emit_debug_trace — must swallow all errors, never raise, never block
# ---------------------------------------------------------------------------

def test_emit_debug_trace_swallows_errors():
    with patch.object(crc.httpx, "post", side_effect=RuntimeError("boom")):
        assert crc._emit_debug_trace("agent", "x") is None


def test_emit_debug_trace_posts_expected_payload():
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout

    with patch.object(crc.httpx, "post", side_effect=_fake_post):
        crc._emit_debug_trace(
            "agent", "turn.done", status="ok", summary="s", explain="e",
            detail={"a": 1}, content_excerpt={"c": "d"}, trace_id="t-1", dur_ms=12.5,
        )

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
    assert captured["timeout"] == 3


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

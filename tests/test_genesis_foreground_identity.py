"""Genesis v2 foreground identity wrapper — reuses the hosted deriver, retries on empty.

Asserts the wrapper is orchestration-only: it calls the EXISTING
history_import._derive_identity_with_provider (never a new prompt) and just retries
when the result carries no identity signal.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import foreground_identity as fgid  # noqa: E402
from hosted import history_import  # noqa: E402


def test_has_identity_signal_rule():
    assert fgid.has_identity_signal({"agent_name": "小柒", "dimensions": []})
    assert fgid.has_identity_signal({"agent_name": "", "dimensions": [{"name": "温柔"}]})
    assert not fgid.has_identity_signal({"agent_name": "", "dimensions": []})
    assert not fgid.has_identity_signal(None)


def test_calls_hosted_deriver_and_stops_on_signal(monkeypatch):
    calls = {"n": 0}

    def fake_deriver(runtime, messages, memory_cards, days, language):
        calls["n"] += 1
        return {"agent_name": "小柒", "dimensions": [{"name": "温柔", "value": 80, "description": "x"}]}, []

    monkeypatch.setattr(history_import, "_derive_identity_with_provider", fake_deriver)
    identity, warnings = fgid.derive_foreground_identity(
        runtime=object(), analysis_messages=[{"role": "user", "content": "hi"}],
        core_memories=[{"summary": "养了狗蛋子"}], days_with_user=144, language="zh")

    assert identity["agent_name"] == "小柒"
    assert calls["n"] == 1                      # got signal first try -> no retry


def test_retries_when_empty_then_succeeds(monkeypatch):
    seq = [
        ({"agent_name": "", "dimensions": []}, ["provider_identity_failed"]),   # transient empty
        ({"agent_name": "老 A", "dimensions": []}, []),                          # retry succeeds
    ]
    calls = {"n": 0}

    def fake_deriver(*a, **k):
        item = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return item

    monkeypatch.setattr(history_import, "_derive_identity_with_provider", fake_deriver)
    identity, _ = fgid.derive_foreground_identity(
        runtime=object(), analysis_messages=[], core_memories=[], days_with_user=0,
        language="zh", max_attempts=2)

    assert identity["agent_name"] == "老 A"
    assert calls["n"] == 2                      # retried once after the empty result


def test_retries_on_provider_failure_even_with_fallback_signal(monkeypatch):
    # a transient provider failure returns a generic FALLBACK (which may carry dims) +
    # a `provider_identity_failed` warning. Identity is the foreground gate, so retry
    # even though the fallback has signal — a 502 must not lock in a degraded identity.
    seq = [
        ({"agent_name": "", "dimensions": [{"name": "泛化"}]}, ["provider_identity_failed:ReadTimeout"]),
        ({"agent_name": "小柒", "dimensions": [{"name": "温柔"}]}, []),   # retry answers clean
    ]
    calls = {"n": 0}

    def fake_deriver(*a, **k):
        item = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return item

    monkeypatch.setattr(history_import, "_derive_identity_with_provider", fake_deriver)
    identity, _ = fgid.derive_foreground_identity(
        runtime=object(), analysis_messages=[], core_memories=[], days_with_user=0,
        language="zh", max_attempts=3)

    assert identity["agent_name"] == "小柒"     # used the clean retry, not the fallback
    assert calls["n"] == 2


def test_empty_result_retries_up_to_cap(monkeypatch):
    # Codex: an empty identity is retryable (a blank could be a transient blip) but
    # CAPPED — after max_attempts we give up (caller takes the minimal-seed branch).
    calls = {"n": 0}

    def fake_deriver(*a, **k):
        calls["n"] += 1
        return {"agent_name": "", "dimensions": []}, ["identity_guard_no_ai_source_used_generic_identity"]

    monkeypatch.setattr(history_import, "_derive_identity_with_provider", fake_deriver)
    fgid.derive_foreground_identity(runtime=object(), analysis_messages=[], core_memories=[],
                                   days_with_user=0, language="zh", max_attempts=3)
    assert calls["n"] == 3   # retried to the cap, then gives up (never infinite)


def test_returns_empty_when_no_signal_after_retries(monkeypatch):
    def fake_deriver(*a, **k):
        return {"agent_name": "", "dimensions": []}, ["identity_guard_no_ai_source"]

    monkeypatch.setattr(history_import, "_derive_identity_with_provider", fake_deriver)
    identity, warnings = fgid.derive_foreground_identity(
        runtime=object(), analysis_messages=[], core_memories=[], days_with_user=0,
        language="zh", max_attempts=2)

    assert not fgid.has_identity_signal(identity)   # caller must NOT mark done on this

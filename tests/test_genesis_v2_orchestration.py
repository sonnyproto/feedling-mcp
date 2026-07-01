"""Genesis v2 Step 3b — the live job orchestration (foreground-fast wiring).

Tests the control flow of routes._run_plaintext_genesis_v2 with the heavy
collaborators (db / apply / background reduce) mocked: real DB e2e is run on test.
What must hold:
  - greetable foreground -> apply+complete, then background skips ONLY the history core
  - nothing greetable -> return False (caller falls back to the v1 full path), no greet
  - background failure -> job stays done (never fails an already-greetable onboarding)
  - the flag gate is off by default
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db  # noqa: E402
from genesis import foreground, foreground_identity, routes, service, worker  # noqa: E402
from hosted import history_import  # noqa: E402


class _Store:
    user_id = "u1"


def _groups():
    return [
        {"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c1", "c2"]},
        {"source_kind": "ai_persona_import", "source_family": "ai_persona", "chunk_texts": ["p1"]},
    ]


def _greetable_fg(**_):
    return {"memories": [{"summary": "x"}], "identity": {"agent_name": "老 A"},
            "core_fact_candidates": [{"summary": "我家狗叫蛋子"}], "source_family": "history"}


def test_v2_foreground_completes_then_background_skips_only_history_core(monkeypatch):
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    # the foreground-applied merge carries the core memory text -> threaded to background
    monkeypatch.setattr(routes, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {"memories": [{"summary": "用户养了一只狗叫蛋子"}]})
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("fg_applied", a[3]))
    monkeypatch.setattr(routes, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.update(bg_skip=k["skip_texts"], bg_family=k["skip_family"],
                                                     bg_known=k.get("known_memories")))

    handled = routes._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(), relationship_anchor=None)

    assert handled is True
    assert "memories" in calls.get("fg_applied", {})            # foreground completed the job
    assert calls["bg_family"] == "history"                       # only the history group's core is skipped
    assert calls["bg_skip"] == foreground.core_skip_texts([{"summary": "我家狗叫蛋子"}])
    # the foreground core memory text is handed to the background as "already saved"
    assert calls["bg_known"] == ["用户养了一只狗叫蛋子"]


def test_v2_returns_false_when_nothing_greetable(monkeypatch):
    applied = {"n": 0}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts",
                        lambda **k: {"memories": [], "identity": {"agent_name": ""}, "core_fact_candidates": []})
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: applied.__setitem__("n", applied["n"] + 1))

    handled = routes._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(), relationship_anchor=None)

    assert handled is False and applied["n"] == 0                # never greets/completes on nothing


def test_v2_background_failure_keeps_job_done(monkeypatch):
    last = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: last.update(output=k.get("output")))
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(routes, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"merged": True})
    monkeypatch.setattr(service, "apply_reducer_output", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("provider 402 out of credits")
    monkeypatch.setattr(routes, "_run_plaintext_background_enrichment", boom)

    handled = routes._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(), relationship_anchor=None)

    assert handled is True                                       # job NOT failed — already greetable
    assert last["output"]["stage"] == "genesis_v2_background_deferred"
    assert "402" in last["output"]["error"]


def test_v2_background_lexical_backstop_drops_near_identical(monkeypatch):
    applied = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    # background reduce yields a near-identical twin of the foreground core + a distinct fact
    monkeypatch.setattr(worker, "build_reducer_output_from_texts", lambda **k: {"memories": [
        {"summary": "用户养了一只比熊狗，叫蛋子。"},   # near-identical survivor -> backstop drops
        {"summary": "用户在杭州工作"},                # distinct -> keep
    ], "source_family": "history"})
    monkeypatch.setattr(routes, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {"memories": outs[0]["memories"]} if outs else {"memories": []})
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda store, api_key, merged: applied.update(memories=merged.get("memories")))
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda store, merged, api_key=None: applied.update(identity_applied=True))
    monkeypatch.setattr(service, "write_persona_artifact", lambda *a, **k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *a, **k: ("", ""))

    routes._run_plaintext_background_enrichment(
        _Store(), "key", "job1", runtime=object(),
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c"]}],
        relationship_anchor=None, skip_family="history", skip_texts=set(),
        known_memories=["用户养了一只比熊狗叫蛋子"])

    summaries = [m["summary"] for m in applied["memories"]]
    assert "用户在杭州工作" in summaries                 # distinct kept
    assert not any("蛋子" in s for s in summaries)       # near-identical twin dropped by backstop
    assert applied.get("identity_applied") is True       # background writes the real identity


def test_v2_foreground_writes_identity_greeting_then_completes(monkeypatch):
    # identity-first contract (restored from legacy chat_ready): when analysis_messages
    # exist and the deriver yields a real identity, the FOREGROUND writes identity +
    # greeting + core, completes the job, and the background does NOT re-write identity.
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(routes, "_plaintext_merge_reducer_outputs",
                        lambda outs, **k: {"memories": [{"summary": "用户养了一只狗叫蛋子"}]})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "小柒", "dimensions": [{"name": "温柔"}]}, []))
    monkeypatch.setattr(service, "apply_memory_outputs",
                        lambda store, api_key, out: (len(out.get("memories") or []), []))
    monkeypatch.setattr(history_import, "_store_identity_payload",
                        lambda store, payload, **k: calls.__setitem__("identity_stored", payload))
    monkeypatch.setattr(history_import, "_generate_model_api_onboarding_greeting",
                        lambda *a, **k: ("小柒: 好久不见呀", []))
    monkeypatch.setattr(history_import, "_append_model_api_onboarding_greeting",
                        lambda store, text: calls.__setitem__("greeting", text))
    monkeypatch.setattr(db, "genesis_complete_job", lambda *a, **k: {"job_id": "job1", "status": "done"})
    monkeypatch.setattr(service, "write_genesis_state",
                        lambda store, job, status=None: calls.__setitem__("completed", status))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(routes, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    handled = routes._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 144},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert handled is True
    assert calls["identity_stored"]["agent_name"] == "小柒"     # identity written in foreground
    assert "小柒" in calls["greeting"]                          # greeting written in foreground
    assert calls["completed"] == service.DONE_JOB_STATUS         # job completed after identity+greeting
    assert calls["bg_write_identity"] is False                   # background must NOT re-write identity
    assert "used_apply_reducer" not in calls                     # did NOT take the empty-identity fallback


def test_v2_foreground_falls_back_when_no_identity(monkeypatch):
    # deriver yields nothing -> fall back to the current behavior (apply_reducer_output),
    # and the background is asked to write identity (write_identity=True).
    calls = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_foreground_output_from_texts", _greetable_fg)
    monkeypatch.setattr(routes, "_plaintext_merge_reducer_outputs", lambda outs, **k: {"memories": []})
    monkeypatch.setattr(history_import, "_import_language_for_store", lambda store, msgs: "zh")
    monkeypatch.setattr(foreground_identity, "derive_foreground_identity",
                        lambda **k: ({"agent_name": "", "dimensions": []}, []))
    monkeypatch.setattr(service, "apply_reducer_output",
                        lambda *a, **k: calls.__setitem__("used_apply_reducer", True))
    monkeypatch.setattr(routes, "_run_plaintext_background_enrichment",
                        lambda *a, **k: calls.__setitem__("bg_write_identity", k.get("write_identity")))

    routes._run_plaintext_genesis_v2(
        _Store(), "key", "job1", runtime=object(), source_groups=_groups(),
        relationship_anchor={"days_with_user": 1},
        analysis_messages=[{"role": "user", "content": "hi"}])

    assert calls.get("used_apply_reducer") is True               # empty-identity fallback
    assert calls["bg_write_identity"] is True                    # background fills identity


def test_merged_has_identity_rule():
    assert routes._merged_has_identity({"identity": {"agent_name": "小柒", "dimensions": []}})
    assert routes._merged_has_identity({"identity": {"agent_name": "", "dimensions": [{"name": "温柔"}]}})
    assert not routes._merged_has_identity({"identity": {"agent_name": "", "dimensions": []}})
    assert not routes._merged_has_identity({"memories": []})


def test_v2_background_derives_baseline_identity_from_persona(monkeypatch):
    # the real bug: memories + persona generated but identity empty -> not_provided ->
    # onboarding wedges on identity_card. Background must derive a baseline from persona.
    applied = {}
    monkeypatch.setattr(db, "genesis_set_job_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "build_reducer_output_from_texts", lambda **k: {
        "memories": [{"summary": "用户在杭州工作"}],
        "persona": {"content": "你是小柒，温柔细心的陪伴者。"},
        "identity": {"agent_name": "", "dimensions": []},   # reduce produced NO identity
        "source_family": "history"})
    monkeypatch.setattr(routes, "_plaintext_merge_reducer_outputs", lambda outs, **k: {
        "memories": outs[0]["memories"], "persona": outs[0]["persona"],
        "identity": {"agent_name": "", "dimensions": []}})
    monkeypatch.setattr(worker, "derive_identity_from_persona", lambda **k: {
        "agent_name": "小柒", "category": "温柔 · 细心",
        "dimensions": [{"name": "温柔", "value": 80, "description": "历史里一贯的语气"}]})
    monkeypatch.setattr(service, "apply_memory_outputs", lambda *a, **k: None)
    monkeypatch.setattr(service, "init_identity_if_absent",
                        lambda store, merged, api_key=None: applied.update(identity=merged.get("identity")))
    monkeypatch.setattr(service, "write_persona_artifact", lambda *a, **k: ("", ""))
    monkeypatch.setattr(service, "write_voice_artifact", lambda *a, **k: ("", ""))

    routes._run_plaintext_background_enrichment(
        _Store(), "key", "job1", runtime=object(),
        source_groups=[{"source_kind": "history_import", "source_family": "history", "chunk_texts": ["c"]}],
        relationship_anchor=None, skip_family="history", skip_texts=set(), known_memories=[])

    # baseline derived from persona prose got written as the Identity Card
    assert applied["identity"]["agent_name"] == "小柒"
    assert applied["identity"]["dimensions"]


def test_genesis_v2_flag_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_V2_ENABLED", raising=False)
    assert worker.genesis_v2_enabled() is False                 # default off -> v1 path
    monkeypatch.setenv("FEEDLING_GENESIS_V2_ENABLED", "true")
    assert worker.genesis_v2_enabled() is True
    monkeypatch.setenv("FEEDLING_GENESIS_V2_ENABLED", "0")
    assert worker.genesis_v2_enabled() is False

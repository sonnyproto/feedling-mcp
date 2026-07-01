"""Genesis v2 Step 2 — phase state machine + per-task checkpoint contract.

Each test pins one of the 4 v2 safety contracts so Step 3/4 can't regress them.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import checkpoint as cp  # noqa: E402


def test_greeting_allowed_only_from_foreground_ready_onward():
    # contract #3 — greeting fires at FOREGROUND_READY, not DONE
    assert not cp.greeting_allowed(cp.PHASE_FOREGROUND_PROCESSING)
    assert cp.greeting_allowed(cp.PHASE_FOREGROUND_READY)
    assert cp.greeting_allowed(cp.PHASE_BACKGROUND_PROCESSING)
    assert cp.greeting_allowed(cp.PHASE_PROVIDER_CONFIG_BLOCKED)
    assert cp.greeting_allowed(cp.PHASE_DONE)
    assert not cp.greeting_allowed(None)
    assert not cp.greeting_allowed(cp.PHASE_FAILED_TERMINAL)


def test_new_checkpoint_starts_foreground():
    c = cp.new_checkpoint(now=1000.0)
    assert c["phase"] == cp.PHASE_FOREGROUND_PROCESSING
    assert c["tasks"] == {} and c["created_at"] == 1000.0


def test_upsert_done_and_pending_for_resume():
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="fact_map", chunk_id=0, status=cp.TASK_DONE, now=1.0)
    c = cp.upsert_task(c, task_id="fact_map", chunk_id=1, status=cp.TASK_PENDING, now=2.0)
    assert cp.is_task_done(c, "fact_map", 0)
    assert not cp.is_task_done(c, "fact_map", 1)
    assert [t["chunk_id"] for t in cp.pending_tasks(c)] == ["1"]  # done one skipped


def test_upsert_idempotent_merges_ids_and_attempts():
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="fw", chunk_id=0, status=cp.TASK_TRANSIENT_FAILED,
                       written_memory_ids=["m1"], bump_attempts=True, now=1.0)
    c = cp.upsert_task(c, task_id="fw", chunk_id=0, status=cp.TASK_DONE,
                       written_memory_ids=["m2"], bump_attempts=True, now=2.0)
    t = cp.get_task(c, "fw", 0)
    assert t["status"] == cp.TASK_DONE and t["attempts"] == 2
    assert t["written_memory_ids"] == ["m1", "m2"]   # merged
    assert len(c["tasks"]) == 1                       # same key → one entry, no dup


def test_dedup_refs_and_should_skip():
    # contracts #1 / #2 — background skips what foreground/earlier already wrote
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="fw", chunk_id=0, status=cp.TASK_DONE,
                       source_ref="hist:42", candidate_id="cand_a",
                       written_memory_ids=["mem_a"], foreground_written=True)
    c = cp.upsert_task(c, task_id="fw", chunk_id=1, status=cp.TASK_DONE,
                       source_ref="hist:99", candidate_id="cand_b",
                       written_memory_ids=["mem_b"], foreground_written=False)
    assert cp.written_refs(c) == {"hist:42", "cand_a", "hist:99", "cand_b"}
    assert cp.foreground_written_refs(c) == {"hist:42", "cand_a"}   # only foreground
    assert cp.all_written_memory_ids(c) == {"mem_a", "mem_b"}
    assert cp.should_skip_candidate(c, candidate_id="cand_a")       # fg already wrote
    assert cp.should_skip_candidate(c, source_ref="hist:99")
    assert not cp.should_skip_candidate(c, candidate_id="cand_new")
    assert not cp.should_skip_candidate(c, source_ref="", candidate_id="")  # empty never skips


def test_provider_config_blocked_then_resume_keeps_done():
    # contract #4 — blocked → user fixes → resume from checkpoint (not re-upload)
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="t", chunk_id=0, status=cp.TASK_DONE, source_ref="a")
    c = cp.upsert_task(c, task_id="t", chunk_id=1, status=cp.TASK_TRANSIENT_FAILED,
                       error_class="provider_config", source_ref="b")
    c = cp.mark_provider_config_blocked(c, reason="402 out of credits", now=5.0)
    assert c["phase"] == cp.PHASE_PROVIDER_CONFIG_BLOCKED
    assert c["resumable"] is True and "402" in c["blocked_reason"]
    c = cp.resume(c, now=6.0)
    assert c["phase"] == cp.PHASE_BACKGROUND_PROCESSING
    assert c.get("resumable") is False and "blocked_reason" not in c
    assert cp.is_task_done(c, "t", 0)                               # done kept
    assert cp.get_task(c, "t", 1)["status"] == cp.TASK_PENDING       # failed re-runs
    assert cp.get_task(c, "t", 1)["error_class"] == ""


def test_set_phase_rejects_unknown():
    with pytest.raises(ValueError):
        cp.set_phase(cp.new_checkpoint(), "bogus_phase")


def test_runnable_tasks_gated_by_phase():
    # Codex point 2 — pending lists blocked tasks (for resume), but the worker must
    # NOT auto-run them while blocked; runnable_tasks gates on phase.
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="t", chunk_id=0, status=cp.TASK_PENDING)
    c = cp.set_phase(c, cp.PHASE_BACKGROUND_PROCESSING)
    assert len(cp.runnable_tasks(c)) == 1
    c = cp.mark_provider_config_blocked(c, reason="402")
    assert len(cp.pending_tasks(c)) == 1     # resume still needs to see it
    assert cp.runnable_tasks(c) == []        # but worker must NOT run it
    c = cp.resume(c)
    assert len(cp.runnable_tasks(c)) == 1     # resume re-enables


def test_upsert_keeps_original_error_for_ops():
    # Codex point 1 — keep 402 vs ReadTimeout vs invalid_json, not just the class
    c = cp.new_checkpoint(now=0.0)
    c = cp.upsert_task(c, task_id="t", chunk_id=0, status=cp.TASK_TRANSIENT_FAILED,
                       error_class="transient_exhausted", error_type="ProviderError",
                       error_message="ReadTimeout on fucheers.top", provider_status_code=None)
    t = cp.get_task(c, "t", 0)
    assert t["error_type"] == "ProviderError"
    assert "ReadTimeout" in t["error_message"]
    assert t["error_class"] == "transient_exhausted"


def test_candidate_id_stable_across_formatting():
    # Codex rule #2 — same fact (different spacing/case) → same id, so foreground
    # and background agree and dedup doesn't drift.
    common = dict(user_id="u1", job_id="j1", source_family="history", source_pass="fact", chunk_index=2)
    a = cp.make_candidate_id(**common, fact_text="她的狗叫蛋子")
    b = cp.make_candidate_id(**common, fact_text="  她的狗叫蛋子  ")
    c = cp.make_candidate_id(**common, fact_text="她的狗叫蛋子\n")
    assert a == b == c and a.startswith("cand_")


def test_candidate_id_differs_on_fact_and_locator():
    common = dict(user_id="u1", job_id="j1", source_family="history", source_pass="fact")
    base = cp.make_candidate_id(**common, chunk_index=0, fact_text="怕香菜")
    assert base != cp.make_candidate_id(**common, chunk_index=0, fact_text="喜欢下雨")   # diff fact
    assert base != cp.make_candidate_id(**common, chunk_index=1, fact_text="怕香菜")      # diff chunk
    assert base != cp.make_candidate_id(user_id="u2", job_id="j1", source_family="history",
                                        source_pass="fact", chunk_index=0, fact_text="怕香菜")  # diff user


def test_source_ref_carries_locator_and_hash():
    cid = cp.make_candidate_id(user_id="u1", job_id="j1", source_family="history",
                               source_pass="fact", chunk_index=3, fact_text="去西湖")
    ref = cp.make_source_ref(job_id="j1", source_pass="fact", chunk_index=3, candidate_id=cid)
    assert ref.startswith("genesis:j1:fact:3:")
    assert cid.split("_")[-1][:16] in ref

"""Genesis v2 Step 3b — the foreground WORKER pass over the REAL prompts/flow.

Unit-tests `worker.build_foreground_output_from_texts`: fact_map over chunks ONCE
-> select 3-5 core -> fact_write ONLY those -> identity baseline, returning the
shared candidate set for the background to partition against. Drives the real
GenesisLLMClient with an injected completion_fn (no network, no DB).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import db  # noqa: E402
import provider_client as pc  # noqa: E402
from genesis import worker  # noqa: E402
from genesis.llm_client import GenesisLLMClient  # noqa: E402

_RUNTIME = pc.ProviderConfig(provider="openai", model="gpt-x", api_key="k", base_url="http://x")

# 8 distinct facts across 2 chunks; relationship + pet should win the core slots.
_FACTS = {
    0: [
        {"about": "relationship", "summary": "我们第一次见面在大学图书馆", "evidence": "原来是你"},
        {"about": "user", "summary": "用户养了一只比熊狗叫蛋子", "evidence": "我家蛋子"},
        {"about": "user", "summary": "用户随口说今天天气不错", "evidence": ""},
    ],
    1: [
        {"about": "user", "summary": "用户最近在控制饮食戒糖", "evidence": "我在戒糖"},
        {"about": "user", "summary": "用户怕香菜从来不吃", "evidence": "我不吃香菜"},
        {"about": "user", "summary": "用户在杭州工作", "evidence": "我在杭州"},
    ],
}


def _fake_completion_factory():
    """Returns a completion_fn that dispatches by prompt: fact_map -> the chunk's
    fact_candidates; fact_write (user msg carries "fact_digest") -> memories+identity
    built straight from whatever core was handed in (so we can assert the partition)."""
    calls = {"fact_map": 0, "fact_write": []}

    def fake(runtime, messages, **kwargs):
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if '"fact_digest"' in user:                       # fact_write call
            digest = json.loads(user)["fact_digest"]
            calls["fact_write"].append(digest)
            memories = [{"bucket": d.get("bucket") or "未分类", "summary": d["summary"],
                         "content": d["summary"], "importance": 0.7} for d in digest]
            reply = json.dumps({"memories": memories,
                                "identity": {"agent_name": "老 A", "dimensions": ["细心"]}})
        else:                                             # fact_map call (user = raw chunk text)
            idx = 0 if "图书馆" in user else 1
            calls["fact_map"] += 1
            reply = json.dumps({"fact_candidates": _FACTS[idx]})
        return {"reply": reply, "usage": {}, "stop_reason": "stop"}

    return fake, calls


def _run(monkeypatch, *, max_core=5):
    monkeypatch.setattr(db, "genesis_upsert_output", lambda *a, **k: None)
    fake, calls = _fake_completion_factory()
    llm = GenesisLLMClient(completion_fn=fake)
    out = worker.build_foreground_output_from_texts(
        user_id="u1", job_id="j1", runtime=_RUNTIME,
        chunk_texts=["第一次见面在图书馆 ...", "戒糖 怕香菜 杭州 ..."],
        source_kind="history", foreground_core_max=max_core, llm=llm,
    )
    return out, calls


def test_foreground_runs_factmap_once_per_chunk_and_writes_only_core(monkeypatch):
    out, calls = _run(monkeypatch, max_core=3)
    assert calls["fact_map"] == 2                          # one extraction per chunk, no repeats
    assert len(out["all_fact_candidates"]) == 6            # full shared set for the background
    assert len(out["core_fact_candidates"]) == 3           # capped at 3
    # fact_write was called with ONLY the core (<= cap), never the whole set
    assert calls["fact_write"] and all(len(d) <= 3 for d in calls["fact_write"])
    assert sum(len(d) for d in calls["fact_write"]) == 3


def test_foreground_core_is_relationship_then_pet(monkeypatch):
    out, _ = _run(monkeypatch, max_core=3)
    core = out["core_fact_candidates"]
    assert core[0]["about"] == "relationship"             # greeting anchor first
    assert "蛋子" in core[1]["summary"]                    # pet next
    # the throwaway "天气不错" (no evidence) must not crowd out a grounded fact
    assert all("天气" not in c["summary"] for c in core)


def test_foreground_returns_memories_and_identity_baseline(monkeypatch):
    out, _ = _run(monkeypatch, max_core=5)
    assert len(out["memories"]) == len(out["core_fact_candidates"])   # core -> memories
    assert out["identity"]["agent_name"] == "老 A"                    # baseline present
    assert out["foreground"] is True and out["source_family"] == "history"


def test_foreground_never_pads_when_signal_is_thin(monkeypatch):
    # ask for 5 but only 6 candidates, 1 ungrounded throwaway -> still capped, never invents
    out, _ = _run(monkeypatch, max_core=5)
    assert len(out["core_fact_candidates"]) == 5
    assert all(c["summary"] for c in out["core_fact_candidates"])

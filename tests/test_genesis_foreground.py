"""Genesis v2 Step 3 — foreground reducer decision logic (Codex constraints)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import checkpoint as ckpt  # noqa: E402
from genesis import foreground as fg  # noqa: E402


def _c(summary, bucket="", importance=0.5, content=""):
    return {"summary": summary, "bucket": bucket, "importance": importance, "content": content}


def test_priority_relationship_then_pet_then_health():
    cands = [
        _c("随手提一句天气", bucket="未分类", importance=0.3),
        _c("我们第一次见面在图书馆", bucket="我们的关系", importance=0.8),
        _c("我家狗叫蛋子是比熊", bucket="宠物", importance=0.7),
        _c("我最近在控制饮食戒糖", bucket="健康", importance=0.6),
    ]
    buckets = [c["bucket"] for c in fg.select_core_for_foreground(cands, max_n=5)]
    assert buckets[0] == "我们的关系"   # relationship first
    assert buckets[1] == "宠物"         # pet next
    assert "健康" in buckets


def test_core_capped_and_never_padded():
    # only 2 high-signal → return 2 (not padded)
    cands = [_c("我们关系的起点", bucket="我们的关系", importance=0.9),
             _c("我家狗叫蛋子", bucket="宠物", importance=0.8),
             _c("x", bucket="未分类", importance=0.0)]          # low signal → excluded
    assert len(fg.select_core_for_foreground(cands, max_n=5)) == 2
    # cap honoured when there's plenty
    many = [_c(f"我们关系里的事实{i}", bucket="我们的关系", importance=0.9) for i in range(10)]
    assert len(fg.select_core_for_foreground(many, max_n=5)) == 5


def test_is_low_signal_rules():
    assert fg.is_low_signal(_c("短", bucket="未分类", importance=0.1))               # too short
    assert fg.is_low_signal(_c("一句没长期意义的闲聊罢了", bucket="未分类", importance=0.1))  # low imp + no priority
    assert not fg.is_low_signal(_c("我家狗叫蛋子是比熊", bucket="宠物", importance=0.1))      # priority bucket saves it
    assert not fg.is_low_signal(_c("一段够长的durable偏好描述", bucket="偏好与边界", importance=0.5))


def test_greeting_material_is_light():
    ident = {"agent_name": "老A", "relationship_anchor_evidence": "认识两年", "category": "细心 · 稳定"}
    core = [_c("狗叫蛋子"), _c("怕香菜"), _c("喜欢下雨"), _c("第四条不应出现")]
    gm = fg.build_greeting_material(identity_baseline=ident, core_memories=core)
    assert gm["agent_name"] == "老A"
    assert gm["relationship_anchor"] == "认识两年"
    assert len(gm["signal_facts"]) == 3          # 1-3 only, no extra chain
    assert gm["persona_baseline"] == "细心 · 稳定"


def test_mark_foreground_core_sets_ready_and_dedup_anchor():
    cid = ckpt.make_candidate_id(user_id="u", job_id="j", source_family="history",
                                 source_pass="fact", chunk_index=0, fact_text="狗叫蛋子")
    ref = ckpt.make_source_ref(job_id="j", source_pass="fact", chunk_index=0, candidate_id=cid)
    cp = fg.mark_foreground_core_written(
        ckpt.new_checkpoint(now=0.0),
        [{"candidate_id": cid, "source_ref": ref, "memory_id": "mem_1"}],
    )
    assert cp["phase"] == ckpt.PHASE_FOREGROUND_READY and ckpt.greeting_allowed(cp["phase"])
    assert cid in ckpt.foreground_written_refs(cp)   # background will skip it (contract #2)
    assert "mem_1" in ckpt.all_written_memory_ids(cp)

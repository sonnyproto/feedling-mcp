"""Batch 2 A1 consumer 侧:蒸馏走共享模板、全字段、坏 JSON 重试一次、不静默。"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_API_URL", "http://fake.local")
os.environ.setdefault("FEEDLING_API_KEY", "test-key")
os.environ.setdefault("FEEDLING_DATA_DIR", tempfile.mkdtemp(prefix="feedling-rid-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import chat_resident_consumer as crc


GOOD = json.dumps({
    "agent_name": "小明", "self_introduction": "我是小明。", "category": "锐 · 实",
    "signature": ["有事直说", "别客套"], "tone_style": "短句、直接",
    "agent_role": "同事", "do_not_say": ["宝贝"], "boundaries": ["不聊政治"],
    "dimensions": [{"name": "直接", "value": 90, "description": "从不绕"}],
}, ensure_ascii=False)


def _patch(monkeypatch, replies):
    calls = {"prompts": []}
    def fake_call_agent(prompt, raw_text=True, trace_id=""):
        calls["prompts"].append(prompt)
        return replies[min(len(calls["prompts"]) - 1, len(replies) - 1)]
    monkeypatch.setattr(crc, "call_agent", fake_call_agent)
    monkeypatch.setattr(crc, "_capture_agent_reply_text", lambda x: x)
    monkeypatch.setattr(crc, "_resident_existing_identity", lambda: {})
    return calls


def test_derive_returns_full_persona_fields(monkeypatch):
    _patch(monkeypatch, [GOOD])
    out = crc._resident_derive_identity("人设材料", "job1")
    assert out["tone_style"] == "短句、直接"
    assert out["agent_role"] == "同事"
    assert out["do_not_say"] == ["宝贝"]
    assert out["boundaries"] == ["不聊政治"]
    assert out["signature"] == ["有事直说", "别客套"]


def test_prompt_comes_from_shared_template(monkeypatch):
    calls = _patch(monkeypatch, [GOOD])
    crc._resident_derive_identity("独特材料XYZ", "job2")
    p = calls["prompts"][0]
    assert "tone_style" in p and "do_not_say" in p and "boundaries" in p
    assert "独特材料XYZ" in p


def test_bad_json_retries_once_then_succeeds(monkeypatch):
    calls = _patch(monkeypatch, ["这不是 JSON", GOOD])
    out = crc._resident_derive_identity("材料", "job3")
    assert out is not None
    assert len(calls["prompts"]) == 2
    assert "ONLY the JSON" in calls["prompts"][1]  # 重试带纠偏提示


def test_bad_json_twice_returns_none(monkeypatch):
    calls = _patch(monkeypatch, ["垃圾", "还是垃圾"])
    assert crc._resident_derive_identity("材料", "job4") is None
    assert len(calls["prompts"]) == 2  # 只重试一次,不无限


def test_existing_identity_flows_into_merge_prompt(monkeypatch):
    calls = _patch(monkeypatch, [GOOD])
    monkeypatch.setattr(crc, "_resident_existing_identity",
                        lambda: {"agent_name": "老c", "tone_style": "锐"})
    crc._resident_derive_identity("材料", "job5")
    assert "EXISTING identity card" in calls["prompts"][0]
    assert "老c" in calls["prompts"][0]


def test_floor_note_below_floor(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 2})
    note = crc._resident_floor_note()
    assert "2" in note and "38" in note
    assert "绝不编造" in note


def test_floor_note_empty_at_or_above_floor(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json",
                        lambda path, **kw: {"memory_floor": 38, "memories_count": 40})
    assert crc._resident_floor_note() == ""


def test_floor_note_empty_on_error(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(crc, "_capture_get_json", boom)
    assert crc._resident_floor_note() == ""


def test_memory_snapshot_composes_terms_and_known(monkeypatch):
    def fake_get(path, **kw):
        if path == "/v1/memory/buckets":
            return {"buckets": [{"name": "工作", "count": 3}, {"name": "协作方式", "count": 2}]}
        if path == "/v1/memory/threads":
            return {"threads": [{"name": "查证不猜"}]}
        return {}
    monkeypatch.setattr(crc, "_capture_get_json", fake_get)
    monkeypatch.setattr(crc, "_resident_memory_index_summaries",
                        lambda: ["hx 是 Teleport 前端", "hx 的红线:优先成功率"])
    terms, known = crc._resident_memory_snapshot()
    assert "工作" in terms and "协作方式" in terms and "查证不猜" in terms
    assert "复用" in terms          # 引导语:先复用,别造近义/中英重复桶
    assert known == ["hx 是 Teleport 前端", "hx 的红线:优先成功率"]


def test_memory_snapshot_empty_garden_returns_empty(monkeypatch):
    monkeypatch.setattr(crc, "_capture_get_json", lambda path, **kw: {})
    monkeypatch.setattr(crc, "_resident_memory_index_summaries", lambda: [])
    terms, known = crc._resident_memory_snapshot()
    assert terms == "" and known == []


def test_memory_snapshot_error_returns_empty(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(crc, "_capture_get_json", boom)
    monkeypatch.setattr(crc, "_resident_memory_index_summaries", lambda: [])
    terms, known = crc._resident_memory_snapshot()
    assert terms == "" and known == []

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import genesis_e2e  # noqa: E402


def _fixture() -> dict:
    return {
        "persona": {
            "agent_name": "小满",
            "category": "温柔陪伴型",
            "dimensions": [
                {"name": "温柔", "value": "高"},
                {"name": "直接", "value": "中"},
            ],
            "self_introduction_keywords": ["小满", "陪你复盘"],
        },
        "relationship": {"expected_days_with_user": 17},
        "ground_truth": {
            "facts": [
                {"id": "fact_dog", "text": "用户养了一只叫蛋子的狗", "keywords": ["蛋子", "狗"]},
                {"id": "fact_city", "text": "用户住在河南焦作", "keywords": ["河南", "焦作"]},
                {"id": "fact_work", "text": "用户远程做前端开发", "keywords": ["远程", "前端"]},
            ]
        },
    }


def test_distill_acceptance_scores_recall_false_positives_duplicates_and_checks():
    memories = [
        {"id": "m1", "title": "蛋子", "description": "用户养了一只叫蛋子的狗。"},
        {"id": "m2", "title": "所在地", "description": "用户住在河南焦作。"},
        {"id": "m3", "title": "地点重复", "description": "用户住在河南焦作。"},
        {"id": "m4", "title": "误报", "description": "用户喜欢潜水。"},
    ]
    identity = {
        "agent_name": "小满",
        "category": "温柔陪伴型",
        "self_introduction": "我是小满，会陪你复盘每天的状态。",
        "dimensions": [
            {"name": "温柔", "value": "高", "description": "稳定接住情绪"},
            {"name": "直接", "value": "中", "description": "必要时给清晰建议"},
        ],
    }

    report = genesis_e2e.evaluate_distill_acceptance(
        _fixture(),
        identity=identity,
        identity_meta={"days_with_user": 17},
        memories=memories,
        validate={"passing": True},
        persona_text="小满会用温柔但直接的方式陪伴用户。",
        voice_text="语气温和、具体、不空泛。",
        greeting_messages=[{"role": "agent", "content": "我在。"}],
        job={"status": "done"},
    )

    assert report["metrics"]["ground_truth_total"] == 3
    assert report["metrics"]["recall_count"] == 2
    assert report["metrics"]["miss_count"] == 1
    assert report["metrics"]["false_positive_count"] == 1
    assert report["metrics"]["duplicate_pair_count"] == 1
    assert report["missed_facts"] == [
        {"id": "fact_work", "text": "用户远程做前端开发", "keywords": ["远程", "前端"]}
    ]
    assert [item["id"] for item in report["false_positives"]] == ["m4"]
    assert report["duplicates"] == [{"left_id": "m2", "right_id": "m3", "reason": "normalized_text"}]
    assert report["checks"]["identity_agent_name"] is True
    assert report["checks"]["relationship_days"] is True
    assert report["checks"]["greeting_non_empty"] is True
    assert report["checks"]["persona_non_empty"] is True
    assert report["checks"]["voice_non_empty"] is True
    assert report["checks"]["validate_passing"] is True
    assert report["checks"]["ground_truth_recall"] is False
    assert report["checks"]["no_duplicate_memories"] is False
    assert report["ok"] is False


def test_distill_acceptance_flags_identity_and_validate_failures():
    report = genesis_e2e.evaluate_distill_acceptance(
        _fixture(),
        identity={
            "agent_name": "别名",
            "category": "",
            "self_introduction": "",
            "dimensions": [{"name": "温柔", "value": "高"}],
        },
        identity_meta={"days_with_user": 0},
        memories=[],
        validate={"passing": False},
        persona_text="",
        voice_text="",
        greeting_messages=[],
        job={"status": "done"},
    )

    assert report["checks"]["identity_agent_name"] is False
    assert report["checks"]["identity_category"] is False
    assert report["checks"]["identity_dimensions"] is False
    assert report["checks"]["identity_self_introduction"] is False
    assert report["checks"]["relationship_days"] is False
    assert report["checks"]["greeting_non_empty"] is False
    assert report["checks"]["persona_non_empty"] is False
    assert report["checks"]["voice_non_empty"] is False
    assert report["checks"]["validate_passing"] is False
    assert report["ok"] is False


def test_chinese_report_contains_actionable_summary():
    report = {
        "ok": False,
        "metrics": {
            "ground_truth_total": 3,
            "recall_count": 2,
            "miss_count": 1,
            "recall_rate": 0.6667,
            "miss_rate": 0.3333,
            "false_positive_count": 1,
            "false_positive_rate": 0.25,
            "duplicate_pair_count": 1,
            "duplicate_rate": 0.25,
        },
        "missed_facts": [{"id": "fact_work", "text": "用户远程做前端开发"}],
        "false_positives": [{"id": "m4", "text": "用户喜欢潜水。"}],
        "duplicates": [{"left_id": "m2", "right_id": "m3", "reason": "normalized_text"}],
        "checks": {"validate_passing": True, "persona_non_empty": False},
    }

    text = genesis_e2e.render_distill_acceptance_report(report)

    assert "蒸馏验收报告" in text
    assert "召回率：66.7%" in text
    assert "漏抽：fact_work｜用户远程做前端开发" in text
    assert "误报：m4｜用户喜欢潜水。" in text
    assert "重复：m2 ↔ m3｜normalized_text" in text
    assert "persona_non_empty：FAIL" in text

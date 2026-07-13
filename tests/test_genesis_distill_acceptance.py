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
                {
                    "id": "fact_dog",
                    "text": "用户养了一只叫蛋子的狗",
                    "keywords": ["蛋子", "狗"],
                },
                {
                    "id": "fact_city",
                    "text": "用户住在河南焦作",
                    "keywords": ["河南", "焦作"],
                },
                {
                    "id": "fact_work",
                    "text": "用户远程做前端开发",
                    "keywords": ["远程", "前端"],
                },
            ]
        },
    }


def _semantic_judgment(
    fact_ids: list[str],
    evidence_sha256: str,
    **overrides: bool,
) -> dict:
    judgment = {
        "schema_version": 1,
        "judge": "qualification_agent",
        "evidence_sha256": evidence_sha256,
        "reviewed_surfaces": ["identity", "persona", "memories"],
        "reviewed_fact_ids": fact_ids,
        "persona_identity_consistent": True,
        "ground_truth_facts_supported": True,
        "contradictions_absent": True,
    }
    judgment.update(overrides)
    return judgment


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
    assert report["missed_facts"] == [{"id": "fact_work"}]
    assert [item["id"] for item in report["false_positives"]] == ["m4"]
    assert report["duplicates"] == [
        {"left_id": "m2", "right_id": "m3", "reason": "normalized_text"}
    ]
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


def test_distill_acceptance_requires_agent_semantic_judgment_for_a_green_result():
    evidence_sha256 = "a" * 64
    kwargs = {
        "identity": {
            "agent_name": "小满",
            "category": "温柔陪伴型",
            "self_introduction": "我是小满，会陪你复盘每天的状态。",
            "dimensions": [
                {"name": "温柔", "description": "稳定接住情绪"},
                {"name": "直接", "description": "必要时给清晰建议"},
            ],
        },
        "identity_meta": {"days_with_user": 17},
        "memories": [
            {"id": "m1", "description": "用户养了一只叫蛋子的狗。"},
            {"id": "m2", "description": "用户住在河南焦作。"},
            {"id": "m3", "description": "用户远程做前端开发。"},
        ],
        "validate": {"passing": True},
        "persona_text": "小满会用温柔但直接的方式陪伴用户。",
        "voice_text": "语气温和、具体、不空泛。",
        "greeting_messages": [{"role": "agent", "content": "我在。"}],
        "job": {"status": "done"},
    }

    lexical_only = genesis_e2e.evaluate_distill_acceptance(_fixture(), **kwargs)
    judged = genesis_e2e.evaluate_distill_acceptance(
        _fixture(),
        **kwargs,
        semantic_judgment=_semantic_judgment(
            ["fact_dog", "fact_city", "fact_work"], evidence_sha256
        ),
        evidence_sha256=evidence_sha256,
    )

    assert lexical_only["checks"]["ground_truth_recall"] is True
    assert lexical_only["semantic_judgment"]["provided"] is False
    assert lexical_only["ok"] is False
    assert judged["semantic_judgment"]["provided"] is True
    assert judged["ok"] is True


def test_explicitly_negated_persona_and_memory_tokens_cannot_false_green():
    evidence_sha256 = "b" * 64
    fixture = {
        "persona": {
            "agent_name": "Mira",
            "category": "warm",
            "dimensions": [{"name": "grounded"}],
            "self_introduction_keywords": ["Mira"],
        },
        "relationship": {"expected_days_with_user": 5},
        "ground_truth": {
            "facts": [
                {
                    "id": "reset-ritual",
                    "text": "Jasmine tea and a walk are the reset ritual.",
                    "keywords": ["jasmine tea", "walk"],
                }
            ]
        },
    }
    report = genesis_e2e.evaluate_distill_acceptance(
        fixture,
        identity={
            "agent_name": "Definitely-not-Mira",
            "category": "not warm",
            "self_introduction": "I am Mira.",
            "dimensions": [{"name": "grounded", "description": "Concrete and calm."}],
        },
        identity_meta={"days_with_user": 5},
        memories=[
            {
                "id": "m-negative",
                "description": "Jasmine tea and a walk do NOT form the reset ritual.",
            }
        ],
        validate={"passing": True},
        persona_text="Mira is a warm, grounded companion.",
        voice_text="Warm voice.",
        greeting_messages=[{"role": "agent", "content": "Hello."}],
        job={"status": "done"},
        semantic_judgment=_semantic_judgment(["reset-ritual"], evidence_sha256),
        evidence_sha256=evidence_sha256,
    )

    assert report["checks"]["identity_agent_name"] is False
    assert report["checks"]["identity_category"] is False
    assert report["checks"]["ground_truth_recall"] is False
    assert report["checks"]["no_explicit_contradictions"] is False
    assert report["metrics"]["explicit_contradiction_count"] == 1
    assert report["contradicted_facts"] == [
        {"id": "reset-ritual", "matched_memory_ids": ["m-negative"]}
    ]
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
        "duplicates": [
            {"left_id": "m2", "right_id": "m3", "reason": "normalized_text"}
        ],
        "checks": {"validate_passing": True, "persona_non_empty": False},
    }

    text = genesis_e2e.render_distill_acceptance_report(report)

    assert "蒸馏验收报告" in text
    assert "召回率：66.7%" in text
    assert "漏抽：fact_work" in text
    assert "误报：m4" in text
    assert "重复：m2 ↔ m3｜normalized_text" in text
    assert "persona_non_empty：FAIL" in text

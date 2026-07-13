from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from identity import card_policy  # noqa: E402


def test_is_runtime_label_matches_known_and_ignores_case():
    assert card_policy.is_runtime_label("Claude") is True
    assert card_policy.is_runtime_label(" hermes ") is True
    assert card_policy.is_runtime_label("阿锐") is False
    assert card_policy.is_runtime_label("") is False


def test_dimensions_structure_accepts_sparse_and_clustered():
    # 契约 B:2 维稀疏、全部聚集在高位,都是合法结构
    sparse = [{"name": "锐利", "value": 90, "description": "x"},
              {"name": "直接", "value": 88, "description": "y"}]
    assert card_policy.validate_dimensions_structure(sparse) == (True, "")
    clustered = [{"name": f"d{i}", "value": 85, "description": "z"} for i in range(7)]
    assert card_policy.validate_dimensions_structure(clustered) == (True, "")


def test_dimensions_structure_rejects_bad_shape():
    assert card_policy.validate_dimensions_structure("nope")[0] is False
    assert card_policy.validate_dimensions_structure(
        [{"name": "", "value": 50, "description": "x"}]) == (False, "dimension_name_empty")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": 150, "description": "x"}]) == (False, "dimension_value_out_of_range")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": "hi", "description": "x"}]) == (False, "dimension_value_not_number")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": 50, "description": "x"},
         {"name": "A", "value": 60, "description": "y"}]) == (False, "dimension_name_duplicate")
    assert card_policy.validate_dimensions_structure(
        [{"name": "a", "value": True, "description": "x"}]) == (False, "dimension_value_not_number")


def test_full_card_structure_only_lenient():
    ok_card = {"agent_name": "阿锐", "self_introduction": "hi",
               "dimensions": [{"name": "锐利", "value": 90, "description": "x"}]}
    assert card_policy.validate_full_identity_card(ok_card) == (True, "")
    # 稀疏(1 维)在契约 B 下合法
    assert card_policy.validate_full_identity_card(
        {"agent_name": "阿锐", "dimensions": []}) == (True, "")
    # 空名字放行(hx 定 0712:优先 onboarding 成功率,名字可后补;不为缺名字卡住 onboarding)
    assert card_policy.validate_full_identity_card(
        {"agent_name": "", "dimensions": []}) == (True, "")
    # 但非空名字仍不能是 runtime label
    assert card_policy.validate_full_identity_card(
        {"agent_name": "Claude", "dimensions": []}) == (False, "agent_name_is_runtime_label")


def test_profile_patch_only_checks_present_fields():
    # 只改名字:旧卡维度稀疏也不该因此被拒
    assert card_policy.validate_profile_patch({"agent_name": "阿锐"}) == (True, "")
    assert card_policy.validate_profile_patch({"tone_style": "sharp"}) == (True, "")
    assert card_policy.validate_profile_patch({"agent_name": "gpt"}) == (False, "agent_name_is_runtime_label")
    assert card_policy.validate_profile_patch(
        {"dimensions": [{"name": "a", "value": 150, "description": "x"}]}) == (False, "dimension_value_out_of_range")


def test_dimension_nudge_range_only():
    assert card_policy.validate_dimension_nudge("锐利", 70) == (True, "")
    assert card_policy.validate_dimension_nudge("锐利", 150) == (False, "dimension_value_out_of_range")
    assert card_policy.validate_dimension_nudge("", 50) == (False, "dimension_name_empty")


def test_service_runtime_labels_are_card_policy_source():
    from identity import service as identity_service
    assert identity_service._IDENTITY_RUNTIME_LABELS is card_policy.RUNTIME_LABELS
    # 既有判定不回归
    assert "claude" in identity_service._IDENTITY_RUNTIME_LABELS
    assert "hermes" in identity_service._IDENTITY_RUNTIME_LABELS
    # 之前被误删的 12 个 label 不回归(google/bard/deepseek 等错误被判定为合法名字)
    for label in ("google", "bard", "deepseek", "agent", "io", "feedling"):
        assert label in card_policy.RUNTIME_LABELS


def test_dimensions_structure_rejects_too_many_and_non_dict():
    thirteen = [{"name": f"d{i}", "value": 50, "description": "x"} for i in range(13)]
    assert card_policy.validate_dimensions_structure(thirteen) == (False, "too_many_dimensions")
    assert card_policy.validate_dimensions_structure(["not-a-dict"]) == (False, "dimension_must_be_object")


def test_runtime_labels_full_set_locked():
    # locks the full 36-label set so a future accidental drop is caught
    assert len(card_policy.RUNTIME_LABELS) == 36

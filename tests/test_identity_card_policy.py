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

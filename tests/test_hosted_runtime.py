from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import hosted_runtime as runtime  # noqa: E402


def test_coerce_runtime_action_supports_relationship_days_set():
    action = {
        "type": "identity.relationship_days_set",
        "confidence": 0.97,
        "payload": {"days_with_user": "68"},
        "reason": "User corrected the displayed relationship day count.",
    }

    coerced = runtime.coerce_runtime_action(action, [], direct_confidence=0.9)

    assert coerced is not None
    assert coerced["domain"] == "identity"
    assert coerced["requires_confirmation"] is False
    assert coerced["executor_action"] == {
        "type": "identity.relationship_days_set",
        "days_with_user": 68,
        "reason": "User corrected the displayed relationship day count.",
        "relationship_anchor_evidence": "User corrected the displayed relationship day count.",
        "source": "hosted_runtime_action",
    }


def test_coerce_runtime_action_maps_patch_with_only_days_to_relationship_days_set():
    action = {
        "type": "identity.patch",
        "confidence": 0.95,
        "payload": {"days_with_user": 68},
        "reason": "User said the relationship is 68 days, not 368.",
    }

    coerced = runtime.coerce_runtime_action(action, [], direct_confidence=0.9)

    assert coerced is not None
    assert coerced["runtime_type"] == "identity.relationship_days_set"
    assert coerced["executor_action"]["type"] == "identity.relationship_days_set"
    assert coerced["executor_action"]["days_with_user"] == 68

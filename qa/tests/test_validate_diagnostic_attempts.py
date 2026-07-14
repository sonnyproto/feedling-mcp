from __future__ import annotations

from copy import deepcopy

import pytest

from qa import validate_diagnostic_attempts as validator


def _result() -> dict:
    scenarios = []
    for scenario_id in validator.SCENARIO_IDS:
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "status": "PRODUCT_FAIL",
                "attempts": 1,
                "attempt_results": [
                    {
                        "attempt": 1,
                        "status": "PRODUCT_FAIL",
                        "failure": {
                            "failure_code": "UNCLASSIFIED_PRODUCT_FAILURE"
                        },
                    }
                ],
                "failure": {"failure_code": "UNCLASSIFIED_PRODUCT_FAILURE"},
            }
        )
    return {"scenarios": scenarios}


def test_accepts_complete_live_attempt_matrix():
    validator.validate_live_attempts(_result())


@pytest.mark.parametrize("scenario_index", range(1, 12))
def test_rejects_preflight_block_propagation_for_each_live_scenario(scenario_index):
    result = _result()
    row = result["scenarios"][scenario_index]
    row["status"] = "BLOCKED_DEPLOYMENT"
    row["failure"] = {"failure_code": "PRECONDITION_MISSING"}
    row["attempt_results"][0] = {
        "attempt": 1,
        "status": "BLOCKED_DEPLOYMENT",
        "failure": {"failure_code": "PRECONDITION_MISSING"},
    }

    with pytest.raises(validator.DiagnosticAttemptError, match=row["scenario_id"]):
        validator.validate_live_attempts(result)


def test_allows_known_local_gaps_only_at_preflight_and_parent_cleanup():
    result = _result()
    for index in (0, 12):
        row = result["scenarios"][index]
        row["status"] = "BLOCKED_DEPLOYMENT"
        row["failure"] = {"failure_code": "PRECONDITION_MISSING"}
        row["attempt_results"][0] = {
            "attempt": 1,
            "status": "BLOCKED_DEPLOYMENT",
            "failure": {"failure_code": "PRECONDITION_MISSING"},
        }

    validator.validate_live_attempts(result)


def test_rejects_missing_or_malformed_attempt_evidence():
    for mutation in ("missing_scenario", "zero_attempts", "skipped_attempt_number"):
        result = deepcopy(_result())
        if mutation == "missing_scenario":
            result["scenarios"].pop(5)
        elif mutation == "zero_attempts":
            result["scenarios"][5]["attempts"] = 0
            result["scenarios"][5]["attempt_results"] = []
        else:
            result["scenarios"][5]["attempt_results"][0]["attempt"] = 2
        with pytest.raises(validator.DiagnosticAttemptError):
            validator.validate_live_attempts(result)

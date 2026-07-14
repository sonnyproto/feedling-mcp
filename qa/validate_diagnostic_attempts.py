#!/usr/bin/env python3
"""Reject agent-authored diagnostic results that short-circuit the live SOP."""

from __future__ import annotations

from typing import Any, Mapping


SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(1, 14))
LIVE_SCENARIO_IDS = SCENARIO_IDS[1:12]


class DiagnosticAttemptError(RuntimeError):
    """The profile result does not prove that every live scenario was attempted."""


def _failure_code(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    code = value.get("failure_code")
    return str(code) if isinstance(code, str) else ""


def validate_live_attempts(profile_result: Mapping[str, Any]) -> None:
    """Require ordered per-scenario attempts, not propagated preflight blockers.

    P0-01 may lack protected deployment evidence in a local diagnostic, and P0-13
    deliberately defers account reset to the deterministic parent. P0-02 through
    P0-12 must still contain a real outcome for their own live operation. An agent
    that copies ``PRECONDITION_MISSING`` through the remaining matrix has not run
    the requested SOP and is rejected as invalid worker evidence.
    """

    scenarios = profile_result.get("scenarios")
    if not isinstance(scenarios, list):
        raise DiagnosticAttemptError("diagnostic scenario attempts are missing")
    scenario_ids = [
        row.get("scenario_id") if isinstance(row, Mapping) else None
        for row in scenarios
    ]
    if scenario_ids != list(SCENARIO_IDS):
        raise DiagnosticAttemptError("diagnostic scenario matrix is incomplete")

    for row in scenarios[1:12]:
        if not isinstance(row, Mapping):
            raise DiagnosticAttemptError("diagnostic scenario attempt is invalid")
        scenario_id = str(row.get("scenario_id") or "")
        attempts = row.get("attempts")
        attempt_results = row.get("attempt_results")
        if (
            not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts not in (1, 2)
            or not isinstance(attempt_results, list)
            or len(attempt_results) != attempts
        ):
            raise DiagnosticAttemptError(
                f"diagnostic scenario {scenario_id} was not attempted"
            )
        for index, attempt in enumerate(attempt_results, start=1):
            if (
                not isinstance(attempt, Mapping)
                or attempt.get("attempt") != index
                or attempt.get("status") not in (
                    "PASS",
                    "PRODUCT_FAIL",
                    "BLOCKED_CREDENTIAL",
                    "BLOCKED_EVIDENCE",
                    "BLOCKED_DEPLOYMENT",
                    "AGENT_ERROR",
                    "SECURITY_FAIL",
                )
                or _failure_code(attempt.get("failure")) == "PRECONDITION_MISSING"
            ):
                raise DiagnosticAttemptError(
                    f"diagnostic scenario {scenario_id} did not execute its live operation"
                )
        if _failure_code(row.get("failure")) == "PRECONDITION_MISSING":
            raise DiagnosticAttemptError(
                f"diagnostic scenario {scenario_id} propagated a preflight blocker"
            )


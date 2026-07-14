from __future__ import annotations

from xml.etree import ElementTree

import pytest

from qa import render_diagnostic_artifacts as renderer


PROFILE_ID = "official-gemini"


def _summary(cot_status: str) -> dict:
    return {
        "schema_version": 1,
        "qualification_mode": "diagnostic",
        "release_qualified": False,
        "run_id": "local-render-unit",
        "candidate_sha": "a" * 40,
        "qualification_harness": {
            "git_head": "b" * 40,
            "dirty": True,
            "source_sha256": "c" * 64,
            "worker_source_sha256": "d" * 64,
            "worker_snapshot_sha256": "d" * 64,
        },
        "status": "DIAGNOSTIC_FAIL" if cot_status != "PASS" else "DIAGNOSTIC_PASS",
        "preflight_only": False,
        "missing_strict_evidence": [],
        "cot_delivery": {
            PROFILE_ID: {
                "status": cot_status,
                "failure_code": "NONE" if cot_status == "PASS" else "TRACE_AMBIGUOUS",
                "delivery_qualified": cot_status == "PASS",
            }
        },
    }


def _agent_pass_profile() -> dict:
    return {
        "profile_id": PROFILE_ID,
        "status": "PASS",
        "scenarios": [
            {"scenario_id": scenario_id, "status": "PASS"}
            for scenario_id in renderer.SCENARIO_IDS
        ],
    }


@pytest.mark.parametrize("cot_status", ("FAIL", "UNVERIFIED", "NOT_RUN"))
def test_junit_fails_p012_when_trusted_cot_is_nonpassing(cot_status):
    root = ElementTree.fromstring(
        renderer.render_junit(
            _summary(cot_status),
            {PROFILE_ID: _agent_pass_profile()},
            (PROFILE_ID,),
        )
    )

    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "0"
    p012 = root.find(".//testcase[@name='P0-12']")
    assert p012 is not None
    failure = p012.find("failure")
    assert failure is not None
    assert failure.attrib == {
        "type": "COT_DELIVERY_FAIL",
        "message": f"trusted-cot-delivery:{cot_status}",
    }
    assert all(
        not list(testcase)
        for testcase in root.findall(".//testcase")
        if testcase.attrib["name"] != "P0-12"
    )


def test_junit_keeps_agent_p012_pass_when_trusted_cot_passes():
    root = ElementTree.fromstring(
        renderer.render_junit(
            _summary("PASS"),
            {PROFILE_ID: _agent_pass_profile()},
            (PROFILE_ID,),
        )
    )

    assert root.attrib["failures"] == "0"
    assert root.attrib["errors"] == "0"
    p012 = root.find(".//testcase[@name='P0-12']")
    assert p012 is not None
    assert list(p012) == []


def test_matrix_separates_cot_gate_failure_from_trusted_observation():
    summary = _summary("FAIL")
    summary["cot_delivery"][PROFILE_ID].update(
        {
            "failure_code": "COT_RESULT_BINDING_MISMATCH",
            "receipt_status": "UNVERIFIED",
            "receipt_failure_code": "CHAT_REQUEST_FAILED",
        }
    )

    matrix = renderer.render_matrix(
        summary,
        {PROFILE_ID: _agent_pass_profile()},
        (PROFILE_ID,),
    )

    assert "COT observation | COT observation code" in matrix
    assert "Agent P0-12" in matrix
    assert "Harness source SHA-256: `" + "c" * 64 + "`" in matrix
    assert "Worker snapshot SHA-256: `" + "d" * 64 + "`" in matrix
    assert (
        "FAIL | COT_RESULT_BINDING_MISMATCH | UNVERIFIED | CHAT_REQUEST_FAILED"
        in matrix
    )

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from qa import diagnostic_results
from qa.orchestration_contract import PROFILE_IDS


def _profile_schema() -> dict:
    document = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/codex-run-result.schema.json").read_text()
    )
    schema = dict(document["$defs"]["profileResult"])
    schema["$defs"] = document["$defs"]
    return schema


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_agent_error_profile_is_schema_valid_and_evidence_negative(profile_id):
    result = diagnostic_results.agent_error_profile(
        {
            "profile_id": profile_id,
            "configured_model": "model-safe",
            "user_id": "synthetic-user-123",
            "runtime_mode": "hosted_resident",
            "trace_enabled": True,
            "api_key": "provider-secret-must-not-escape",
            "secret_key_b64": "content-secret-must-not-escape",
        },
        profile_id=profile_id,
        expected_runtime="hosted_resident",
    )

    assert list(Draft202012Validator(_profile_schema()).iter_errors(result)) == []
    assert result["status"] == "AGENT_ERROR"
    assert result["scenarios"] == []
    assert result["turns"] == []
    assert result["diagnostic_codes"] == [
        "AGENT_EXECUTION_ERROR",
        "TRACE_PARTIAL",
        "STAGE_TIMING_UNAVAILABLE",
    ]
    assert result["reasoning"]["effective_effort"] == "unknown"
    assert result["reasoning"]["raw_private_reasoning_stored"] is False
    assert result["trace"]["correlated_event_count"] == 0
    assert result["cleanup"]["attempted"] is False
    serialized = json.dumps(result, sort_keys=True)
    assert "provider-secret-must-not-escape" not in serialized
    assert "content-secret-must-not-escape" not in serialized


def test_agent_error_profile_replaces_unsafe_optional_labels():
    result = diagnostic_results.agent_error_profile(
        {
            "profile_id": "official-gemini",
            "configured_model": "unsafe model\nsecret",
            "user_id": "unsafe user id",
            "runtime_mode": "unexpected runtime\n",
            "trace_enabled": False,
        },
        profile_id="official-gemini",
        expected_runtime="hosted_resident",
    )

    assert result["model"] == "unavailable"
    assert result["user_id"] is None
    assert result["observed_runtime"] is None
    assert result["trace"]["enabled"] is False
    assert result["trace"]["deploy_enabled"] is False


def test_agent_error_profile_preserves_bounded_unicode_relay_model():
    result = diagnostic_results.agent_error_profile(
        {
            "profile_id": "relay-kongbeiqie",
            "configured_model": "[特价纯血]claude-opus-4-6",
            "user_id": "synthetic-user-123",
            "runtime_mode": "hosted_resident",
            "trace_enabled": True,
        },
        profile_id="relay-kongbeiqie",
        expected_runtime="hosted_resident",
    )

    assert result["model"] == "[特价纯血]claude-opus-4-6"


@pytest.mark.parametrize(
    "manifest_profile,profile_id,expected_runtime",
    (
        ({"profile_id": "unknown"}, "unknown", "hosted_resident"),
        (
            {"profile_id": "official-gemini"},
            "official-openai",
            "hosted_resident",
        ),
        ({"profile_id": "official-gemini"}, "official-gemini", "resident_cli"),
    ),
)
def test_agent_error_profile_rejects_unlocked_contracts(
    manifest_profile, profile_id, expected_runtime
):
    with pytest.raises(
        diagnostic_results.DiagnosticResultError, match="diagnostic fallback"
    ):
        diagnostic_results.agent_error_profile(
            manifest_profile,
            profile_id=profile_id,
            expected_runtime=expected_runtime,
        )

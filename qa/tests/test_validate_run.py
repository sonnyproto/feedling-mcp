from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from qa import validate_run as gate


SHA = "a" * 40
STAMP = "2026-07-13T12:00:00Z"
VALID_MODELS = {
    "official-deepseek": "deepseek-v4-flash",
    "official-anthropic": "claude-sonnet-4-5",
    "official-openai": "gpt-5.4",
    "official-gemini": "gemini-2.5-flash",
    "openrouter-claude": "anthropic/claude-sonnet-4.5",
    "openrouter-openai": "openai/gpt-4.1-mini",
    "openrouter-glm": "z-ai/glm-4.5-air:free",
    "relay-kongbeiqie": "[特价纯血]claude-opus-4-6",
}


def _redaction() -> dict:
    return {
        "provider_keys_omitted": True,
        "feedling_api_keys_omitted": True,
        "content_private_keys_omitted": True,
        "raw_chat_omitted": True,
        "raw_trace_omitted": True,
        "raw_reasoning_omitted": True,
        "synthetic_users_only": True,
        "prompt_injection_detected": False,
    }


def _scenario(scenario_id: str, profile_index: int, all_turns: list[dict]) -> dict:
    contract = gate._SCENARIO_CONTRACTS[scenario_id]
    scenario_turns = [row for row in all_turns if row["scenario_id"] == scenario_id]
    if scenario_id == "P0-13":
        turn_ids = [row["turn_id"] for row in all_turns]
        trace_ids = [row["trace_id"] for row in all_turns]
    else:
        turn_ids = [row["turn_id"] for row in scenario_turns]
        trace_ids = [row["trace_id"] for row in scenario_turns]
    request_count = contract["minimum_id_counts"]["request_ids"]
    request_ids = [
        f"request-{profile_index}-{scenario_id}-{index}"
        for index in range(1, request_count + 1)
    ]
    persona_finalizer = None
    if scenario_id == "P0-06":
        persona_finalizer = {
            "fixture_id": "persona-import-v1",
            "evidence_sha256": f"{profile_index + 1:064x}",
            "request_id": request_ids[0],
            "job_id": f"genesis-job-{profile_index}-P0-06",
            "semantic_judgment_bound": True,
            "finalizer_ok": True,
            "private_evidence_deleted": True,
            "privacy_violation_count": 0,
        }
    return {
        "scenario_id": scenario_id,
        "status": "PASS",
        "started_at": STAMP,
        "finished_at": STAMP,
        "attempts": 1,
        "attempt_results": [{"attempt": 1, "status": "PASS", "failure": None}],
        "assertions": {
            assertion: True for assertion in contract["required_assertions"]
        },
        "evidence_codes": list(contract["required_evidence_codes"]),
        "request_ids": request_ids,
        "turn_ids": turn_ids,
        "trace_ids": trace_ids,
        "persona_finalizer": persona_finalizer,
        "failure": None,
    }


def _turn(scenario_id: str, turn_index: int, profile_index: int) -> dict:
    return {
        "scenario_id": scenario_id,
        "turn_index": turn_index,
        "request_id": f"request-{profile_index}-{scenario_id}-{turn_index}",
        "turn_id": f"turn-{profile_index}-{scenario_id}-{turn_index}",
        "trace_id": f"trace-{profile_index}-{scenario_id}-{turn_index}",
        "ack_latency_ms": 1,
        "reply_latency_ms": 2,
        "stage_latency_ms": {stage: 1 for stage in gate.REQUIRED_TRACE_STAGES},
        "reply_count": 1,
        "content_assertion_passed": True,
        "fallback_detected": False,
        "duplicate_detected": False,
        "out_of_order_detected": False,
    }


def _profile_turns(profile_index: int) -> list[dict]:
    counts = {"P0-08": 1, "P0-09": 10, "P0-10": 2, "P0-11": 1, "P0-12": 1}
    return [
        _turn(scenario_id, turn_index, profile_index)
        for scenario_id, count in counts.items()
        for turn_index in range(1, count + 1)
    ]


def _profile(profile_id: str, index: int) -> dict:
    route_family, model_family, provider = gate._PROFILE_METADATA[profile_id]
    turns = _profile_turns(index)
    return {
        "profile_id": profile_id,
        "route_family": route_family,
        "model_family": model_family,
        "provider": provider,
        "model": VALID_MODELS[profile_id],
        "reasoning_effort": "medium",
        "user_id": f"synthetic-user-{index}",
        "expected_runtime": "db_action_v2",
        "observed_runtime": "db_action_v2",
        "status": "PASS",
        "scenarios": [
            _scenario(item, index, turns) for item in gate.LOCKED_SCENARIO_IDS
        ],
        "turns": turns,
        "latency": {
            "sample_count": len(turns),
            "ack_p50_ms": 1,
            "reply_p50_ms": 2,
            "reply_p95_ms": 2,
            "stage_p50_ms": {stage: 1 for stage in gate.REQUIRED_TRACE_STAGES},
            "missing_stages": [],
        },
        "reasoning": {
            "expected": True,
            "capability_enabled": True,
            "requested_effort": "medium",
            "configured_effort": "medium",
            "effective_effort": "medium",
            "reasoning_event_count": 1,
            "metadata_present": True,
            "token_metadata_present": True,
            "user_visible_disclosure_present": True,
            "request_id": f"request-{index}-P0-12-1",
            "turn_id": f"turn-{index}-P0-12-1",
            "trace_id": f"trace-{index}-P0-12-1",
            "kind": "provider_reasoning_summary",
            "source": "qa",
            "model": VALID_MODELS[profile_id],
            "reasoning_token_count": 1,
            "disclosure_length": 1,
            "raw_private_reasoning_stored": False,
        },
        "trace": {
            "enabled": True,
            "deploy_enabled": True,
            "correlated_event_count": len(turns) * len(gate.REQUIRED_TRACE_STAGES),
            "observed_event_types": list(gate.REQUIRED_TRACE_STAGES),
            "missing_required_event_types": [],
            "raw_trace_stored": False,
        },
        "cleanup": {
            "attempted": True,
            "provider_config_deleted": True,
            "account_reset": True,
            "old_credential_rejected": True,
            "status": "PASS",
        },
        "diagnostic_codes": [],
        "redaction": _redaction(),
    }


def _valid_result() -> dict:
    return {
        "schema_version": "1.0",
        "suite_id": "feedling-api-key-runtime-v2-p0",
        "run_id": "unit-run-0001",
        "started_at": STAMP,
        "finished_at": STAMP,
        "target": {
            "environment": "test",
            "base_url": "https://test-api.feedling.app",
            "expected_deployment_sha": SHA,
            "observed_backend_sha": SHA,
            "observed_worker_sha": SHA,
            "expected_runtime": "db_action_v2",
        },
        "overall_status": "PASS",
        "profiles_expected": 8,
        "profiles_completed": 8,
        "orchestration": {
            "supervisor_count": 1,
            "max_configured_profile_concurrency": 3,
            "max_observed_profile_concurrency": 3,
            "profile_worker_assignments": [
                {
                    "profile_id": profile_id,
                    "worker_id": f"00000000-0000-4000-8000-{index:012d}",
                }
                for index, profile_id in enumerate(gate.LOCKED_PROFILE_IDS)
            ],
        },
        "profiles": [
            _profile(profile_id, index)
            for index, profile_id in enumerate(gate.LOCKED_PROFILE_IDS)
        ],
        "summary": {
            "pass": 8,
            "product_fail": 0,
            "blocked_credential": 0,
            "blocked_evidence": 0,
            "blocked_deployment": 0,
            "agent_error": 0,
            "security_fail": 0,
        },
        "artifacts": {
            "run_result": "run-result.json",
            "matrix_markdown": "matrix.md",
            "latency_csv": "latency.csv",
            "junit_xml": "junit.xml",
            "profiles_directory": "profiles",
        },
        "redaction": _redaction(),
    }


def _write_run(tmp_path: Path, result: dict | None = None) -> tuple[Path, Path]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "matrix.md").write_text("matrix\n")
    (artifacts / "latency.csv").write_text("profile,latency_ms\n")
    (artifacts / "junit.xml").write_text("<testsuite/>\n")
    profiles = artifacts / "profiles"
    profiles.mkdir()
    for profile_id in gate.LOCKED_PROFILE_IDS:
        (profiles / f"{profile_id}.json").write_text("{}\n")
    result_path = artifacts / "run-result.json"
    result_path.write_text(json.dumps(_valid_result() if result is None else result))
    return artifacts, result_path


def _write_receipt(
    tmp_path: Path, filename: str = "deployment-receipt.json", **updates
) -> Path:
    receipt = {
        "schema_version": 1,
        "environment": "test",
        "base_url": "https://test-api.feedling.app",
        "expected_deployment_sha": SHA,
        "observed_backend_sha": SHA,
        "observed_worker_sha": SHA,
        "live_worker_count": 2,
        "verified_at": (
            "2026-07-13T12:01:00Z"
            if filename.startswith("post-")
            else "2026-07-13T11:59:00Z"
        ),
    }
    receipt.update(updates)
    path = tmp_path / filename
    path.write_text(json.dumps(receipt))
    return path


def _write_provisioning_manifest(tmp_path: Path) -> Path:
    result = _valid_result()
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-07-13T11:58:00Z",
        "base_url": "https://test-api.feedling.app",
        "runtime_mode": "db_action_v2",
        "synthetic_account_reaper": {
            "enabled": True,
            "label_prefix": "agent-e2e-",
            "max_ttl_seconds": 14_400,
        },
        "profiles": [],
    }
    for profile in result["profiles"]:
        profile_id = profile["profile_id"]
        manifest["profiles"].append(
            {
                "profile_id": profile_id,
                "label": f"agent-e2e-{result['run_id']}-{profile_id}",
                "provider": profile["provider"],
                "route_family": profile["route_family"],
                "configured_model": profile["model"],
                "configured_base_url": gate._PROFILE_CONFIGURED_BASE_URLS[profile_id],
                "reasoning_effort": "medium",
                "provision_status": "ready",
                "provision_failure_code": "NONE",
                "user_id": profile["user_id"],
                "api_key": f"private-account-key-{profile_id}",
                "secret_key_b64": "cHJpdmF0ZS1zZWNyZXQta2V5",
                "public_key_b64": "cHJpdmF0ZS1wdWJsaWMta2V5",
                "trace_enabled": True,
                "runtime_mode": "db_action_v2",
                "registration_verified": True,
                "fresh_state_verified": True,
                "invalid_key_rejected": True,
                "invalid_key_receipt": {
                    "http_status": 400,
                    "error": "provider_test_failed",
                    "provider_status_code": 401,
                },
                "valid_key_configured": True,
                "valid_key_receipt": {
                    "status": "configured",
                    "provider": profile["provider"],
                    "model": profile["model"],
                    "base_url": gate._PROFILE_CONFIGURED_BASE_URLS[profile_id],
                    "reasoning_effort": "medium",
                },
                "runtime_mode_set_verified": True,
                "runtime_mode_readback_verified": True,
            }
        )
    path = tmp_path / "provisioning-manifest.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    return path


def _write_orchestration_receipt(
    tmp_path: Path, result: dict | None = None, **updates
) -> Path:
    source = _valid_result() if result is None else result
    workers = []
    peak = source["orchestration"]["max_observed_profile_concurrency"]
    for index, ((profile_id, agent_type), assignment) in enumerate(
        zip(
            gate.PROFILE_AGENT_TYPES,
            source["orchestration"]["profile_worker_assignments"],
            strict=True,
        )
    ):
        start_second = (index // peak) * 2
        stop_second = start_second + 1
        canonical_profile = json.dumps(
            source["profiles"][index],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        workers.append(
            {
                "profile_id": profile_id,
                "agent_type": agent_type,
                "attempt": 1,
                "process_exit_code": 0,
                "worker_id": assignment["worker_id"],
                "thread_id": assignment["worker_id"],
                "session_id": None,
                "permission_profile": f"feedling-e2e-{profile_id}",
                "started_at": f"2026-07-13T12:00:{start_second:02d}.000001Z",
                "stopped_at": f"2026-07-13T12:00:{stop_second:02d}.000001Z",
                "profile_result_sha256": hashlib.sha256(canonical_profile).hexdigest(),
                "exec_events_sha256": "c" * 64,
            }
        )
    receipt = {
        "schema_version": 2,
        "launcher_id": "run-10000000",
        "max_configured_profile_concurrency": 3,
        "max_observed_profile_concurrency": peak,
        "launch_attempts": 8,
        "workers": workers,
    }
    receipt.update(updates)
    path = tmp_path / "orchestration-receipt.json"
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    return path


def _validate(
    tmp_path: Path,
    result: dict | None = None,
    *,
    manifest_path: Path | None = None,
    receipt_result: dict | None = None,
) -> list[str]:
    artifacts, result_path = _write_run(tmp_path, result)
    qa_dir = Path(__file__).resolve().parents[1]
    return gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=(
            _write_provisioning_manifest(tmp_path)
            if manifest_path is None
            else manifest_path
        ),
        orchestration_receipt_path=_write_orchestration_receipt(
            tmp_path, receipt_result
        ),
        deployment_receipt_path=_write_receipt(tmp_path),
        post_deployment_receipt_path=_write_receipt(
            tmp_path, "post-deployment-receipt.json"
        ),
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )


def test_valid_release_artifacts_pass(tmp_path):
    assert _validate(tmp_path) == []


@pytest.mark.parametrize(
    "model",
    (
        "[relay]\nclaude-opus-4-6",
        "claude-opus-4-6\x7f",
        "claude-opus-4-6\x85",
        "claude-opus-4-6\u2028injected",
        "claude-opus-4-6\u202eabc",
        "claude-opus-4-6|injected",
        "claude-opus-4-6`injected",
        "[relay|injected]claude-opus-4-6",
        "[relay`injected]claude-opus-4-6",
    ),
)
def test_relay_model_label_rejects_controls_and_artifact_delimiters(tmp_path, model):
    result = _valid_result()
    relay = result["profiles"][-1]
    relay["model"] = model
    relay["reasoning"]["model"] = model

    errors = _validate(tmp_path, result)

    assert any("JSON Schema at $.profiles[7].model" in error for error in errors)


def test_result_target_must_be_the_exact_test_origin(tmp_path):
    result = _valid_result()
    result["target"]["base_url"] = "https://attacker.example"

    assert any(
        "JSON Schema at $.target.base_url" in error
        for error in _validate(tmp_path, result)
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda coverage: coverage["required_scenarios"].reverse(),
        lambda coverage: coverage["scenario_contracts"]["P0-08"].update(
            required_evidence_codes=[]
        ),
        lambda coverage: coverage["trace_latency_contract"].update(
            required_stages=["provider"]
        ),
        lambda coverage: coverage["profiles"][0].update(reasoning_effort="off"),
        lambda coverage: coverage["profiles"][0].update(model_env="UNLOCKED_MODEL"),
        lambda coverage: coverage["profiles"][0].update(allowed_model_regex=r"^.*$"),
        lambda coverage: coverage["target"].update(base_url_env="UNLOCKED_BASE_URL"),
        lambda coverage: coverage["reasoning_contract"].update(
            raw_private_chain_of_thought_forbidden=False
        ),
        lambda coverage: coverage["artifact_contract"].update(required=[]),
        lambda coverage: coverage["execution"].update(max_profile_concurrency=4),
        lambda coverage: coverage["execution"].update(allow_profile_skip=True),
        lambda coverage: coverage["execution"].update(max_attempts_per_scenario=99),
        lambda coverage: coverage["execution"].update(profile_timeout_seconds=99_999),
        lambda coverage: coverage["execution"].update(
            chat_reply_timeout_seconds=99_999
        ),
        lambda coverage: coverage["execution"].update(
            distillation_timeout_seconds=99_999
        ),
    ],
)
def test_coverage_lock_cannot_weaken_evidence_contract(mutate):
    qa_dir = Path(__file__).resolve().parents[1]
    coverage = json.loads((qa_dir / "coverage-lock.json").read_text())
    mutate(coverage)
    assert gate._validate_coverage(coverage, "db_action_v2")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda result: result.update(overall_status="PRODUCT_FAIL"), "overall status"),
        (
            lambda result: result["profiles"][0].update(status="PRODUCT_FAIL"),
            "profile official-deepseek status",
        ),
        (
            lambda result: result["profiles"][0]["scenarios"][4].update(
                status="PRODUCT_FAIL",
                failure={
                    "category": "PRODUCT_FAIL",
                    "stage_code": "RUNTIME_SELECTION",
                    "failure_code": "RUNTIME_MISMATCH",
                    "reproducible": True,
                },
            ),
            "scenario P0-05 status",
        ),
        (
            lambda result: result["profiles"][0].update(
                observed_runtime="resident_cli"
            ),
            "did not observe Runtime V2",
        ),
        (
            lambda result: result["target"].update(observed_worker_sha="b" * 40),
            "observed worker SHA",
        ),
        (
            lambda result: result["redaction"].update(provider_keys_omitted=False),
            "run-level redaction assertions",
        ),
        (
            lambda result: result["profiles"][0]["reasoning"].update(
                token_metadata_present=False
            ),
            "reasoning assertions are incomplete",
        ),
    ],
)
def test_nonpassing_runtime_and_sha_states_fail(tmp_path, mutate, expected):
    result = _valid_result()
    mutate(result)
    assert any(expected in error for error in _validate(tmp_path, result))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda result: result["profiles"][0].update(provider="openrouter"),
            "route metadata",
        ),
        (
            lambda result: result["profiles"][0]["reasoning"].update(
                reasoning_token_count=0
            ),
            "reasoning assertions",
        ),
        (
            lambda result: result["profiles"][0]["trace"].update(
                missing_required_event_types=["delivery"]
            ),
            "trace assertions",
        ),
        (
            lambda result: result["profiles"][0]["latency"].update(sample_count=10),
            "latency evidence",
        ),
        (
            lambda result: result["profiles"][0]["turns"][0].update(reply_count=2),
            "failed turn assertion",
        ),
        (
            lambda result: result["redaction"].update(provider_keys_omitted=False),
            "run-level redaction",
        ),
        (
            lambda result: result["profiles"][0]["turns"].pop(),
            "turns are not exact and ordered",
        ),
    ],
)
def test_semantic_evidence_is_fail_closed(tmp_path, mutate, expected):
    result = _valid_result()
    mutate(result)
    assert any(expected in error for error in _validate(tmp_path, result))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda receipt: receipt.update(fixture_id="persona-import-v2"),
        lambda receipt: receipt.update(evidence_sha256="b" * 63),
        lambda receipt: receipt.update(request_id=""),
        lambda receipt: receipt.update(job_id=None),
        lambda receipt: receipt.update(semantic_judgment_bound=False),
        lambda receipt: receipt.update(finalizer_ok=False),
        lambda receipt: receipt.update(private_evidence_deleted=False),
        lambda receipt: receipt.update(privacy_violation_count=1),
    ],
)
def test_p0_06_persona_finalizer_receipt_is_fail_closed(tmp_path, mutate):
    result = _valid_result()
    receipt = result["profiles"][0]["scenarios"][5]["persona_finalizer"]
    mutate(receipt)

    assert _validate(tmp_path, result)


def test_p0_06_persona_finalizer_request_must_match_scenario(tmp_path):
    result = _valid_result()
    result["profiles"][0]["scenarios"][5]["persona_finalizer"][
        "request_id"
    ] = "unrelated-persona-request"

    errors = _validate(tmp_path, result)

    assert any(
        "persona finalizer request does not match P0-06" in error for error in errors
    )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("job_id", "duplicate persona finalizer job IDs"),
        ("evidence_sha256", "duplicate persona finalizer evidence hashes"),
    ],
)
def test_p0_06_persona_finalizer_receipt_cannot_be_reused_across_profiles(
    tmp_path, field, expected
):
    result = _valid_result()
    first = result["profiles"][0]["scenarios"][5]["persona_finalizer"]
    second = result["profiles"][1]["scenarios"][5]["persona_finalizer"]
    second[field] = first[field]

    assert any(expected in error for error in _validate(tmp_path, result))


def test_persona_finalizer_evidence_is_for_p0_06_only(tmp_path):
    result = _valid_result()
    result["profiles"][0]["scenarios"][0]["persona_finalizer"] = deepcopy(
        result["profiles"][0]["scenarios"][5]["persona_finalizer"]
    )

    assert _validate(tmp_path, result)


@pytest.mark.parametrize("field", ["request_id", "turn_id", "trace_id"])
def test_p0_12_reasoning_evidence_must_match_exact_turn(tmp_path, field):
    result = _valid_result()
    result["profiles"][0]["reasoning"][field] = f"unrelated-{field}"

    errors = _validate(tmp_path, result)

    assert any(
        "reasoning evidence does not match P0-12 turn" in error for error in errors
    )


@pytest.mark.parametrize("field", ["request_id", "turn_id", "trace_id"])
def test_p0_12_reasoning_correlation_ids_are_required(tmp_path, field):
    result = _valid_result()
    del result["profiles"][0]["reasoning"][field]

    assert _validate(tmp_path, result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability_enabled", False),
        ("requested_effort", "off"),
        ("configured_effort", "off"),
        ("effective_effort", "off"),
        ("reasoning_event_count", 0),
    ],
)
def test_p0_12_capability_effort_and_event_evidence_fail_closed(tmp_path, field, value):
    result = _valid_result()
    result["profiles"][0]["reasoning"][field] = value

    errors = _validate(tmp_path, result, receipt_result=result)

    assert not any("JSON Schema" in error for error in errors)
    assert any(
        "reasoning assertions are incomplete" in error
        for error in gate._validate_result_semantics(result, gate.EXPECTED_RUNTIME, SHA)
    )


def test_p0_12_rejects_configured_medium_when_capability_clamps_effective_off(
    tmp_path,
):
    result = _valid_result()
    profile = result["profiles"][0]
    reasoning = profile["reasoning"]
    assert profile["reasoning_effort"] == "medium"
    reasoning.update(
        capability_enabled=False,
        requested_effort="medium",
        configured_effort="medium",
        effective_effort="off",
        reasoning_event_count=0,
    )

    errors = _validate(tmp_path, result, receipt_result=result)

    assert errors
    assert not any("JSON Schema" in error for error in errors)
    assert any(
        "reasoning assertions are incomplete" in error
        for error in gate._validate_result_semantics(result, gate.EXPECTED_RUNTIME, SHA)
    )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda scenario: scenario.update(assertions={}),
            "assertions do not match the lock",
        ),
        (
            lambda scenario: scenario["assertions"].update(target_is_test=False),
            "assertions do not match the lock",
        ),
        (
            lambda scenario: scenario.update(evidence_codes=[]),
            "evidence codes do not match the lock",
        ),
        (
            lambda scenario: scenario["evidence_codes"].pop(),
            "evidence codes do not match the lock",
        ),
    ],
)
def test_evidence_free_or_mutated_pass_scenario_fails(tmp_path, mutate, expected):
    result = _valid_result()
    mutate(result["profiles"][0]["scenarios"][0])
    assert any(expected in error for error in _validate(tmp_path, result))


def test_scenario_order_is_locked(tmp_path):
    result = _valid_result()
    result["profiles"][0]["scenarios"].reverse()
    errors = _validate(tmp_path, result)
    assert any("scenario order is not P0-01 through P0-13" in error for error in errors)


def test_turn_order_is_locked(tmp_path):
    result = _valid_result()
    result["profiles"][0]["turns"].reverse()
    errors = _validate(tmp_path, result)
    assert any("turn order does not match the lock" in error for error in errors)


@pytest.mark.parametrize(
    ("scenario_index", "field"),
    [(1, "request_ids"), (7, "turn_ids"), (7, "trace_ids"), (12, "trace_ids")],
)
def test_required_scenario_identifiers_cannot_be_omitted(
    tmp_path, scenario_index, field
):
    result = _valid_result()
    result["profiles"][0]["scenarios"][scenario_index][field] = []
    errors = _validate(tmp_path, result)
    assert any(field in error or "correlate every" in error for error in errors)


def test_valid_bounded_retry_preserves_first_observation(tmp_path):
    result = _valid_result()
    profile = result["profiles"][0]
    scenario = profile["scenarios"][7]
    scenario["attempts"] = 2
    scenario["attempt_results"] = [
        {
            "attempt": 1,
            "status": "AGENT_ERROR",
            "failure": {
                "category": "AGENT_ERROR",
                "stage_code": "BASIC_CHAT",
                "failure_code": "CHAT_TIMEOUT",
                "reproducible": False,
            },
        },
        {"attempt": 2, "status": "PASS", "failure": None},
    ]
    scenario["evidence_codes"].append("RETRY_OBSERVATION_RECORDED")
    profile["diagnostic_codes"] = ["RETRY_USED", "TRANSIENT_TRANSPORT_ERROR"]
    assert _validate(tmp_path, result, receipt_result=result) == []


def test_product_failure_cannot_be_greened_by_a_retry(tmp_path):
    result = _valid_result()
    profile = result["profiles"][0]
    scenario = profile["scenarios"][7]
    scenario["attempts"] = 2
    scenario["attempt_results"] = [
        {
            "attempt": 1,
            "status": "PRODUCT_FAIL",
            "failure": {
                "category": "PRODUCT_FAIL",
                "stage_code": "MEMORY_PERSONA",
                "failure_code": "PERSONA_DRIFT",
                "reproducible": False,
            },
        },
        {"attempt": 2, "status": "PASS", "failure": None},
    ]
    scenario["evidence_codes"].append("RETRY_OBSERVATION_RECORDED")
    profile["diagnostic_codes"] = ["RETRY_USED", "TRANSIENT_TRANSPORT_ERROR"]

    errors = _validate(tmp_path, result)

    assert any("retry is not a bounded transient retry" in error for error in errors)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda scenario, profile: scenario.update(attempt_results=[]),
        lambda scenario, profile: scenario["attempt_results"][0]["failure"].update(
            reproducible=True
        ),
        lambda scenario, profile: scenario["evidence_codes"].remove(
            "RETRY_OBSERVATION_RECORDED"
        ),
        lambda scenario, profile: profile.update(diagnostic_codes=[]),
    ],
)
def test_retry_cannot_erase_or_omit_first_observation(tmp_path, mutate):
    result = _valid_result()
    profile = result["profiles"][0]
    scenario = profile["scenarios"][7]
    scenario["attempts"] = 2
    scenario["attempt_results"] = [
        {
            "attempt": 1,
            "status": "AGENT_ERROR",
            "failure": {
                "category": "AGENT_ERROR",
                "stage_code": "BASIC_CHAT",
                "failure_code": "CHAT_TIMEOUT",
                "reproducible": False,
            },
        },
        {"attempt": 2, "status": "PASS", "failure": None},
    ]
    scenario["evidence_codes"].append("RETRY_OBSERVATION_RECORDED")
    profile["diagnostic_codes"] = ["RETRY_USED", "TRANSIENT_TRANSPORT_ERROR"]
    mutate(scenario, profile)
    assert _validate(tmp_path, result)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda profile: profile["trace"].update(
            observed_event_types=list(gate.REQUIRED_TRACE_STAGES[:-1])
        ),
        lambda profile: profile["trace"].update(correlated_event_count=5),
        lambda profile: profile["latency"]["stage_p50_ms"].update(queue=None),
        lambda profile: profile["latency"].update(missing_stages=["queue"]),
    ],
)
def test_trace_and_latency_require_all_five_numeric_stages(tmp_path, mutate):
    result = _valid_result()
    mutate(result["profiles"][0])
    assert _validate(tmp_path, result)


def test_each_passing_turn_requires_all_five_numeric_stage_latencies(tmp_path):
    result = _valid_result()
    result["profiles"][0]["turns"][0]["stage_latency_ms"]["queue"] = None

    errors = _validate(tmp_path, result)

    assert any("failed turn assertion" in error for error in errors)


@pytest.mark.parametrize("ack,reply", [(3, 2), (1, 120_001)])
def test_passing_turn_latency_must_be_ordered_and_within_timeout(tmp_path, ack, reply):
    result = _valid_result()
    profile = result["profiles"][0]
    profile["turns"][0]["ack_latency_ms"] = ack
    profile["turns"][0]["reply_latency_ms"] = reply
    if reply > 120_000:
        profile["latency"]["reply_p95_ms"] = reply

    errors = _validate(tmp_path, result)

    assert any("failed turn assertion" in error for error in errors)


def test_turn_request_trace_chain_and_global_ids_are_exact(tmp_path):
    result = _valid_result()
    first = result["profiles"][0]
    second = result["profiles"][1]
    second_turn = second["turns"][0]
    second_scenario = second["scenarios"][7]
    second_turn["request_id"] = first["turns"][0]["request_id"]
    second_turn["trace_id"] = first["turns"][0]["trace_id"]
    second_scenario["request_ids"] = [second_turn["request_id"]]
    second_scenario["trace_ids"] = [second_turn["trace_id"]]

    errors = _validate(tmp_path, result)

    assert "result contains duplicate request IDs" in errors
    assert "result contains duplicate trace IDs" in errors


def test_turn_request_id_must_match_its_scenario_receipt(tmp_path):
    result = _valid_result()
    result["profiles"][0]["turns"][0]["request_id"] = "unrelated-request"

    errors = _validate(tmp_path, result)

    assert any("request IDs do not match its turns" in error for error in errors)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda latency: latency.update(ack_p50_ms=999),
        lambda latency: latency.update(reply_p50_ms=999),
        lambda latency: latency.update(reply_p95_ms=999),
        lambda latency: latency["stage_p50_ms"].update(provider=999),
    ],
)
def test_profile_latency_summaries_must_match_turn_samples(tmp_path, mutate):
    result = _valid_result()
    mutate(result["profiles"][0]["latency"])

    errors = _validate(tmp_path, result)

    assert any("latency evidence is incomplete" in error for error in errors)


def test_reasoning_effort_must_be_medium(tmp_path):
    result = _valid_result()
    result["profiles"][0]["reasoning_effort"] = "off"
    errors = _validate(tmp_path, result)
    assert any("reasoning_effort" in error for error in errors)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda orchestration: orchestration.update(
                max_observed_profile_concurrency=4
            ),
            "max_observed_profile_concurrency",
        ),
        (
            lambda orchestration: orchestration["profile_worker_assignments"].pop(),
            "profile_worker_assignments",
        ),
        (
            lambda orchestration: orchestration["profile_worker_assignments"][1].update(
                worker_id=orchestration["profile_worker_assignments"][0]["worker_id"]
            ),
            "assignment IDs are missing or duplicated",
        ),
    ],
)
def test_orchestration_proves_eight_workers_and_concurrency_cap(
    tmp_path, mutate, expected
):
    result = _valid_result()
    mutate(result["orchestration"])
    errors = _validate(tmp_path, result)
    assert any(expected in error for error in errors)


def test_agent_worker_ids_must_match_trusted_process_receipt(tmp_path):
    result = _valid_result()
    result["orchestration"]["profile_worker_assignments"][0][
        "worker_id"
    ] = "00000000-0000-4000-8000-999999999999"
    errors = _validate(tmp_path, result)
    assert "agent worker assignments differ from trusted receipt" in errors


def test_agent_peak_must_match_trusted_process_receipt(tmp_path):
    result = _valid_result()
    result["orchestration"]["max_observed_profile_concurrency"] = 2
    errors = _validate(tmp_path, result)
    assert "agent orchestration concurrency differs from trusted receipt" in errors


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda receipt: receipt["workers"][0].update(attempt=2),
            "worker process is invalid",
        ),
        (
            lambda receipt: receipt["workers"][0].update(process_exit_code=1),
            "worker process is invalid",
        ),
        (
            lambda receipt: receipt["workers"][0].update(thread_id="different-thread"),
            "worker identity is invalid",
        ),
        (
            lambda receipt: receipt["workers"][0].update(
                profile_result_sha256="A" * 64
            ),
            "content binding is invalid",
        ),
        (
            lambda receipt: receipt.update(launch_attempts=7),
            "launch count is invalid",
        ),
        (
            lambda receipt: receipt.update(max_configured_profile_concurrency=4),
            "concurrency cap is invalid",
        ),
        (
            lambda receipt: receipt.update(max_observed_profile_concurrency=2),
            "concurrency is inconsistent",
        ),
    ),
)
def test_trusted_process_receipt_fails_closed_on_tampering(tmp_path, mutate, expected):
    result = _valid_result()
    path = _write_orchestration_receipt(tmp_path, result)
    receipt = json.loads(path.read_text())
    mutate(receipt)
    errors = gate._validate_orchestration_receipt(receipt, result)
    assert any(expected in error for error in errors)


def test_aggregator_cannot_rewrite_a_trusted_profile_result(tmp_path):
    result = _valid_result()
    result["profiles"][0]["model"] = "deepseek-rewritten"
    errors = _validate(tmp_path, result)
    assert "agent profile results differ from trusted receipt" in errors


def test_trusted_orchestration_receipt_must_be_owner_only(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    receipt = _write_orchestration_receipt(tmp_path)
    receipt.chmod(0o644)
    qa_dir = Path(__file__).resolve().parents[1]
    with pytest.raises(
        gate.GateInputError, match="orchestration receipt is unreadable"
    ):
        gate.validate_release(
            coverage_path=qa_dir / "coverage-lock.json",
            schema_path=qa_dir / "schemas" / "run-result.schema.json",
            result_path=result_path,
            artifacts_path=artifacts,
            provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
            orchestration_receipt_path=receipt,
            deployment_receipt_path=_write_receipt(tmp_path),
            post_deployment_receipt_path=_write_receipt(
                tmp_path, "post-deployment-receipt.json"
            ),
            expected_runtime="db_action_v2",
            expected_sha=SHA,
        )


def test_duplicate_profile_id_and_missing_locked_profile_fail(tmp_path):
    result = _valid_result()
    result["profiles"][-1]["profile_id"] = result["profiles"][0]["profile_id"]
    errors = _validate(tmp_path, result)
    assert any("exact eight profiles" in error for error in errors)
    assert any("duplicate profile IDs" in error for error in errors)


def test_duplicate_scenario_id_and_missing_locked_scenario_fail(tmp_path):
    result = _valid_result()
    scenarios = result["profiles"][0]["scenarios"]
    scenarios[-1]["scenario_id"] = scenarios[0]["scenario_id"]
    errors = _validate(tmp_path, result)
    assert any("exact P0-01 through P0-13" in error for error in errors)
    assert any("duplicate scenario IDs" in error for error in errors)


def test_schema_error_does_not_echo_untrusted_value(tmp_path, capsys):
    result = _valid_result()
    secret = "sk-secret-value-that-must-not-appear"
    result["unexpected"] = secret
    artifacts, result_path = _write_run(tmp_path, result)
    qa_dir = Path(__file__).resolve().parents[1]
    rc = gate.main(
        [
            "--coverage",
            str(qa_dir / "coverage-lock.json"),
            "--schema",
            str(qa_dir / "schemas" / "run-result.schema.json"),
            "--result",
            str(result_path),
            "--artifacts",
            str(artifacts),
            "--provisioning-manifest",
            str(_write_provisioning_manifest(tmp_path)),
            "--orchestration-receipt",
            str(_write_orchestration_receipt(tmp_path, result)),
            "--deployment-receipt",
            str(_write_receipt(tmp_path)),
            "--post-deployment-receipt",
            str(_write_receipt(tmp_path, "post-deployment-receipt.json")),
            "--expected-runtime",
            "db_action_v2",
            "--expected-sha",
            SHA,
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "release gate: FAIL" in captured.err
    assert secret not in captured.out + captured.err


@pytest.mark.parametrize(
    "unsafe_reference",
    [
        "../matrix.md",
        "/tmp/matrix.md",
        "nested/../../matrix.md",
        "C:/matrix.md",
        "nested\\matrix.md",
    ],
)
def test_artifact_traversal_and_absolute_paths_fail(tmp_path, unsafe_reference):
    result = _valid_result()
    result["artifacts"]["matrix_markdown"] = unsafe_reference
    errors = _validate(tmp_path, result)
    assert errors
    assert unsafe_reference not in "\n".join(errors)


def test_artifact_symlink_escape_fails(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    (artifacts / "matrix.md").unlink()
    try:
        os.symlink(outside, artifacts / "matrix.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    qa_dir = Path(__file__).resolve().parents[1]
    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
        deployment_receipt_path=_write_receipt(tmp_path),
        post_deployment_receipt_path=_write_receipt(
            tmp_path, "post-deployment-receipt.json"
        ),
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )
    assert any("escapes the artifact root" in error for error in errors)


def test_artifact_root_symlink_fails(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    linked_root = tmp_path / "linked-artifacts"
    try:
        os.symlink(artifacts, linked_root)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    qa_dir = Path(__file__).resolve().parents[1]
    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=linked_root,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
        deployment_receipt_path=_write_receipt(tmp_path),
        post_deployment_receipt_path=_write_receipt(
            tmp_path, "post-deployment-receipt.json"
        ),
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )
    assert errors == ["artifact root is missing or unreadable"]


def test_run_result_reference_must_identify_validated_result(tmp_path):
    result = _valid_result()
    result["artifacts"]["run_result"] = "nested/run-result.json"
    artifacts, result_path = _write_run(tmp_path, result)
    (artifacts / "nested").mkdir()
    (artifacts / "nested" / "run-result.json").write_text("{}\n")
    qa_dir = Path(__file__).resolve().parents[1]
    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path, result),
        deployment_receipt_path=_write_receipt(tmp_path),
        post_deployment_receipt_path=_write_receipt(
            tmp_path, "post-deployment-receipt.json"
        ),
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )
    assert any(
        "$.artifacts.run_result" in error and "rule=const" in error for error in errors
    )


def test_every_locked_profile_checkpoint_must_exist(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    (artifacts / "profiles" / "official-deepseek.json").unlink()
    qa_dir = Path(__file__).resolve().parents[1]
    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
        deployment_receipt_path=_write_receipt(tmp_path),
        post_deployment_receipt_path=_write_receipt(
            tmp_path, "post-deployment-receipt.json"
        ),
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )
    assert "profile artifact official-deepseek is missing or unsafe" in errors


def test_abbreviated_candidate_sha_is_rejected(tmp_path):
    errors = gate.validate_release(
        coverage_path=tmp_path / "unused-coverage.json",
        schema_path=tmp_path / "unused-schema.json",
        result_path=tmp_path / "unused-result.json",
        artifacts_path=tmp_path,
        provisioning_manifest_path=tmp_path / "unused-provisioning-manifest.json",
        orchestration_receipt_path=tmp_path / "unused-orchestration-receipt.json",
        deployment_receipt_path=tmp_path / "unused-receipt.json",
        post_deployment_receipt_path=tmp_path / "unused-post-receipt.json",
        expected_runtime="db_action_v2",
        expected_sha="abc1234",
    )
    assert errors == ["expected deployment SHA is malformed"]


def test_trusted_deployment_receipt_must_match_candidate_and_result(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    receipt = _write_receipt(tmp_path, observed_worker_sha="b" * 40)
    qa_dir = Path(__file__).resolve().parents[1]
    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
        deployment_receipt_path=receipt,
        post_deployment_receipt_path=_write_receipt(
            tmp_path, "post-deployment-receipt.json"
        ),
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )
    assert "pre-run trusted deployment receipt does not match the candidate" in errors
    assert "agent result deployment identity differs from the pre-run receipt" in errors


def test_trusted_provisioning_manifest_binds_model_user_and_reasoning_route(tmp_path):
    result = _valid_result()
    profile = result["profiles"][0]
    profile["model"] = "relabeled-model"
    profile["user_id"] = "relabeled-user"
    profile["reasoning"]["model"] = "relabeled-model"

    errors = _validate(tmp_path, result)

    assert "profile official-deepseek is not bound to its provisioned account" in errors


def test_trusted_provisioning_manifest_blocked_profile_cannot_release(tmp_path):
    manifest_path = _write_provisioning_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["profiles"][0]["provision_status"] = "blocked"
    manifest["profiles"][0]["provision_failure_code"] = "VALID_KEY_REJECTED"
    manifest_path.write_text(json.dumps(manifest))
    manifest_path.chmod(0o600)

    errors = _validate(tmp_path, _valid_result(), manifest_path=manifest_path)

    assert (
        "profile official-deepseek trusted provisioning receipt is incomplete" in errors
    )


def test_trusted_provisioning_manifest_rejects_swapped_openrouter_model_families(
    tmp_path,
):
    result = _valid_result()
    manifest = json.loads(_write_provisioning_manifest(tmp_path).read_text())
    manifest_by_id = {row["profile_id"]: row for row in manifest["profiles"]}
    result_by_id = {row["profile_id"]: row for row in result["profiles"]}
    claude = manifest_by_id["openrouter-claude"]
    openai = manifest_by_id["openrouter-openai"]
    claude["configured_model"], openai["configured_model"] = (
        openai["configured_model"],
        claude["configured_model"],
    )
    for profile_id in ("openrouter-claude", "openrouter-openai"):
        entry = manifest_by_id[profile_id]
        model = entry["configured_model"]
        entry["valid_key_receipt"]["model"] = model
        result_by_id[profile_id]["model"] = model
        result_by_id[profile_id]["reasoning"]["model"] = model

    errors = gate._validate_provisioning_manifest(manifest, result, "db_action_v2")

    assert (
        "profile openrouter-claude trusted provisioning receipt is incomplete" in errors
    )
    assert (
        "profile openrouter-openai trusted provisioning receipt is incomplete" in errors
    )


@pytest.mark.parametrize("tamper_receipt", (False, True))
def test_trusted_provisioning_manifest_binds_kongbeiqie_base_url(
    tmp_path, tamper_receipt
):
    result = _valid_result()
    manifest = json.loads(_write_provisioning_manifest(tmp_path).read_text())
    relay = next(
        row for row in manifest["profiles"] if row["profile_id"] == "relay-kongbeiqie"
    )
    field = relay["valid_key_receipt"] if tamper_receipt else relay
    key = "base_url" if tamper_receipt else "configured_base_url"
    field[key] = "https://attacker.example/v1"

    errors = gate._validate_provisioning_manifest(manifest, result, "db_action_v2")

    assert (
        "profile relay-kongbeiqie trusted provisioning receipt is incomplete" in errors
    )


def test_trusted_provisioning_manifest_must_be_owner_only_regular_file(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    manifest = _write_provisioning_manifest(tmp_path)
    manifest.chmod(0o644)
    qa_dir = Path(__file__).resolve().parents[1]

    with pytest.raises(
        gate.GateInputError, match="provisioning manifest is unreadable"
    ):
        gate.validate_release(
            coverage_path=qa_dir / "coverage-lock.json",
            schema_path=qa_dir / "schemas" / "run-result.schema.json",
            result_path=result_path,
            artifacts_path=artifacts,
            provisioning_manifest_path=manifest,
            orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
            deployment_receipt_path=_write_receipt(tmp_path),
            post_deployment_receipt_path=_write_receipt(
                tmp_path, "post-deployment-receipt.json"
            ),
            expected_runtime="db_action_v2",
            expected_sha=SHA,
        )


def test_post_run_receipt_must_match_pre_run_identity(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    pre_receipt = _write_receipt(tmp_path)
    post_receipt = _write_receipt(
        tmp_path,
        "post-deployment-receipt.json",
        observed_worker_sha="b" * 40,
    )
    qa_dir = Path(__file__).resolve().parents[1]
    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
        deployment_receipt_path=pre_receipt,
        post_deployment_receipt_path=post_receipt,
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )
    assert "post-run trusted deployment receipt does not match the candidate" in errors
    assert "deployment identity changed during the qualification run" in errors


def test_pre_and_post_receipts_must_strictly_bracket_the_run(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    pre_receipt = _write_receipt(tmp_path, verified_at="2026-07-13T12:01:00Z")
    post_receipt = _write_receipt(
        tmp_path,
        "post-deployment-receipt.json",
        verified_at="2026-07-13T12:01:00Z",
    )
    qa_dir = Path(__file__).resolve().parents[1]

    errors = gate.validate_release(
        coverage_path=qa_dir / "coverage-lock.json",
        schema_path=qa_dir / "schemas" / "run-result.schema.json",
        result_path=result_path,
        artifacts_path=artifacts,
        provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
        orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
        deployment_receipt_path=pre_receipt,
        post_deployment_receipt_path=post_receipt,
        expected_runtime="db_action_v2",
        expected_sha=SHA,
    )

    assert (
        "deployment receipt timestamps do not bracket the qualification run" in errors
    )


def test_trusted_deployment_receipt_symlink_fails(tmp_path):
    artifacts, result_path = _write_run(tmp_path, _valid_result())
    receipt = _write_receipt(tmp_path)
    linked_receipt = tmp_path / "linked-receipt.json"
    try:
        os.symlink(receipt, linked_receipt)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    qa_dir = Path(__file__).resolve().parents[1]
    with pytest.raises(gate.GateInputError, match="receipt is unreadable"):
        gate.validate_release(
            coverage_path=qa_dir / "coverage-lock.json",
            schema_path=qa_dir / "schemas" / "run-result.schema.json",
            result_path=result_path,
            artifacts_path=artifacts,
            provisioning_manifest_path=_write_provisioning_manifest(tmp_path),
            orchestration_receipt_path=_write_orchestration_receipt(tmp_path),
            deployment_receipt_path=linked_receipt,
            post_deployment_receipt_path=_write_receipt(
                tmp_path, "post-deployment-receipt.json"
            ),
            expected_runtime="db_action_v2",
            expected_sha=SHA,
        )


def test_duplicate_synthetic_and_turn_ids_fail(tmp_path):
    result = _valid_result()
    result["profiles"][1]["user_id"] = result["profiles"][0]["user_id"]
    turn = {
        "scenario_id": "P0-09",
        "turn_index": 1,
        "request_id": "duplicate-request",
        "turn_id": "duplicate-turn",
        "trace_id": None,
        "ack_latency_ms": 1,
        "reply_latency_ms": 2,
        "stage_latency_ms": {stage: 1 for stage in gate.REQUIRED_TRACE_STAGES},
        "reply_count": 1,
        "content_assertion_passed": True,
        "fallback_detected": False,
        "duplicate_detected": False,
        "out_of_order_detected": False,
    }
    result["profiles"][0]["turns"] = [deepcopy(turn)]
    result["profiles"][1]["turns"] = [deepcopy(turn)]
    errors = _validate(tmp_path, result)
    assert "result contains duplicate synthetic user IDs" in errors
    assert "result contains duplicate turn IDs" in errors

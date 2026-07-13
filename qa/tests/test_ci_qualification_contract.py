from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PG_DEPLOY = (ROOT / ".github" / "workflows" / "pg-deploy.yml").read_text(
    encoding="utf-8"
)
E2E = (ROOT / ".github" / "workflows" / "api-key-e2e.yml").read_text(encoding="utf-8")


def _job(name: str, next_name: str) -> str:
    start = CI.index(f"  {name}:\n")
    end = CI.index(f"  {next_name}:\n", start)
    return CI[start:end]


def test_deterministic_qualification_contracts_gate_test_and_production_deploys():
    qa_job = _job("qa-contract-tests", "docker-build")
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert "python -m pytest -q" in qa_job
    assert "qa/tests" in qa_job
    assert "tools/provider_smoke/tests" in qa_job
    assert "tests/test_genesis_distill_acceptance.py" in qa_job
    assert "python -m pip install --require-hashes -r qa/requirements.lock" in qa_job
    assert "qa-contract-tests" in test_deploy.split("steps:", 1)[0]
    assert "qa-contract-tests" in production_deploy.split("steps:", 1)[0]


def test_test_admin_credential_is_distinct_from_the_production_secret():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert "secrets.TEST_FEEDLING_ADMIN_TOKEN" in test_deploy
    assert "secrets.FEEDLING_ADMIN_TOKEN" not in test_deploy
    assert "secrets.FEEDLING_ADMIN_TOKEN" in production_deploy
    assert "secrets.TEST_FEEDLING_ADMIN_TOKEN" not in production_deploy


def test_every_test_environment_mutator_uses_the_qualification_lock():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    test_runner_deploy = _job("deploy-test-runner-cvm", "deploy-prod-runner-cvm")

    for source in (test_deploy, test_runner_deploy, PG_DEPLOY, E2E):
        assert "feedling-test-environment" in source
    assert "options: [test]" in PG_DEPLOY


def test_manual_qualification_binds_agent_output_to_trusted_inputs():
    assert "on:\n  workflow_dispatch:" in E2E
    assert '--provisioning-manifest "${{ steps.context.outputs.manifest }}"' in E2E
    assert "per-turn five-stage latency" in E2E
    assert "qualification-agent semantic judgment" in E2E
    assert "steps.deployment_pre.outcome" in E2E
    assert "steps.deployment_post.outcome" in E2E

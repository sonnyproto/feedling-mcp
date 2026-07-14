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
    assert '--basetemp "${RUNNER_TEMP}/feedling-qa-pytest-' in qa_job
    assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in qa_job
    assert "qa/tests" in qa_job
    assert "tools/provider_smoke/tests" in qa_job
    assert "tests/test_genesis_distill_acceptance.py" in qa_job
    assert "python -m pip install --require-hashes -r qa/requirements.lock" in qa_job
    assert "qa-contract-tests" in test_deploy.split("steps:", 1)[0]
    assert "qa-contract-tests" in production_deploy.split("steps:", 1)[0]


def test_existing_default_branch_ci_dispatches_exact_test_ref_e2e_workflow():
    manual_job = _job("api-key-e2e-manual", "forge-test")

    assert "resolve-test-deployment-sha:" not in CI
    assert "github.event_name == 'workflow_dispatch'" in manual_job
    assert "github.ref == 'refs/heads/test'" in manual_job
    assert "uses: ./.github/workflows/api-key-e2e.yml" in manual_job
    assert "expected_deployment_sha:" not in manual_job
    assert "runtime_target: deployed_current" in manual_job
    assert "secrets: inherit" in manual_job
    assert "workflow_call:" in E2E
    assert "group: ci-${{ github.event_name }}-${{ github.ref }}" in CI
    assert "cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}" in CI


def test_e2e_resolves_current_compose_pin_inside_the_environment_lock():
    trigger = E2E[E2E.index("on:\n") : E2E.index("permissions:\n")]

    assert "expected_deployment_sha:" not in trigger
    assert "group: feedling-test-environment" in E2E
    assert "ref: test" in E2E
    assert "fetch-depth: 0" in E2E
    assert "Resolve current serialized test deployment target" in E2E
    assert 'git show "origin/test:deploy/docker-compose.phala.test.yaml"' in E2E
    assert 'if [ "${#images[@]}" -ne 2 ]' in E2E
    assert "GITHUB_REPOSITORY_OWNER: ${{ github.repository_owner }}" in E2E
    assert '[[ ! "$tag" =~ ^[a-f0-9]{7,64}$ ]]' in E2E
    assert 'git rev-parse --verify "origin/test^{commit}"' in E2E
    assert "git merge-base --is-ancestor" in E2E
    assert "steps.deployment_target.outputs.sha" in E2E


def test_backend_qualification_regressions_run_with_postgres_dependencies():
    backend_job = _job("python-tests", "qa-contract-tests")

    assert "tests/test_memory_contract_backend.py" in backend_job
    assert "tests/test_qa_build_identity.py" in backend_job
    assert "tests/test_qa_synthetic_accounts.py" in backend_job
    assert "FEEDLING_TEST_PG" in backend_job
    assert "backend/requirements.lock" in backend_job


def test_test_admin_credential_is_distinct_from_the_production_secret():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert "secrets.TEST_FEEDLING_ADMIN_TOKEN" in test_deploy
    assert "secrets.FEEDLING_ADMIN_TOKEN" not in test_deploy
    assert "secrets.FEEDLING_ADMIN_TOKEN" in production_deploy
    assert "secrets.TEST_FEEDLING_ADMIN_TOKEN" not in production_deploy


def test_test_deploy_enables_bounded_synthetic_account_reaper():
    test_deploy = _job("deploy-test-cvm", "deploy-test-runner-cvm")
    production_deploy = _job("deploy-cvm", "deploy-test-cvm")

    assert 'FEEDLING_QA_SYNTHETIC_ACCOUNTS_ENABLED: "true"' in test_deploy
    assert 'FEEDLING_QA_SYNTHETIC_ACCOUNT_MAX_TTL_SECONDS: "14400"' in test_deploy
    assert 'FEEDLING_QA_SYNTHETIC_REAPER_INTERVAL_SECONDS: "60"' in test_deploy
    for variable in (
        "FEEDLING_QA_SYNTHETIC_ACCOUNTS_ENABLED",
        "FEEDLING_QA_SYNTHETIC_ACCOUNT_MAX_TTL_SECONDS",
        "FEEDLING_QA_SYNTHETIC_REAPER_INTERVAL_SECONDS",
    ):
        assert f'-e "{variable}=${variable}"' in test_deploy
        assert variable not in production_deploy
    assert 'if [ -z "$FEEDLING_ADMIN_TOKEN" ]' in test_deploy
    assert "TEST_FEEDLING_ADMIN_TOKEN secret is not set" in test_deploy


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

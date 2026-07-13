from __future__ import annotations

from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "api-key-e2e.yml"
).read_text(encoding="utf-8")


def _step(name: str, next_name: str) -> str:
    start = WORKFLOW.index(f"      - name: {name}")
    end = WORKFLOW.index(f"      - name: {next_name}", start)
    return WORKFLOW[start:end]


def test_workflow_is_manual_only_and_uses_protected_ephemeral_runner():
    trigger = WORKFLOW[WORKFLOW.index("on:\n") : WORKFLOW.index("permissions:\n")]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "pull_request:" not in trigger
    assert "schedule:" not in trigger
    assert "  deployment:" not in trigger
    assert "validate-dispatch:" in WORKFLOW
    assert 'if [ "$DISPATCH_REF" != "refs/heads/test" ]' in WORKFLOW
    assert "needs: validate-dispatch" in WORKFLOW
    assert "environment: feedling-e2e-test" in WORKFLOW
    assert "runs-on: [self-hosted, linux, x64, feedling-e2e]" in WORKFLOW
    assert "timeout-minutes: 240" in WORKFLOW
    assert "group: feedling-test-environment" in WORKFLOW
    assert "persist-credentials: false" in WORKFLOW
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in WORKFLOW
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in WORKFLOW
    assert (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in WORKFLOW
    )


def test_codex_preflight_installs_oauth_and_real_top_level_profile_config():
    preflight = _step(
        "Install and verify isolated headless Codex runtime",
        "Provision eight isolated API-key profiles",
    )
    assert "qa/install_codex_auth.py" in preflight
    assert "qa/write_codex_config.py" in preflight
    assert "--full-manifest" in preflight
    assert "--worker-output-root" in preflight
    assert "--aggregation-input-root" in preflight
    assert "--orchestration-receipt" in preflight
    assert "--runtime-read-root" in preflight
    assert "codex-cli 0.144.3" in preflight
    assert "persistent Codex auth is forbidden" in preflight
    assert "must run as an unprivileged user" in preflight
    assert "secrets.QA_CODEX_AUTH_JSON_B64" in preflight
    assert "vars.QA_CODEX_MODEL" in preflight
    assert "unset QA_CODEX_AUTH_JSON_B64" in preflight
    assert "mcp list --json" in preflight
    assert "sandbox -P feedling-e2e-official-deepseek" in preflight
    assert "https://test-api.feedling.app/" in preflight
    assert "https://example.com/" in preflight
    assert "https://1.1.1.1/" in preflight
    assert "--noproxy" in preflight
    assert "-p profile_official_deepseek" in preflight
    assert "--strict-config" in preflight
    assert "--output-schema" in preflight
    assert "parse_exec_events" in preflight
    assert "spawn_agent" not in preflight
    assert "record_codex_subagent_hook" not in preflight
    assert "dangerously-bypass-hook-trust" not in preflight


def test_provider_admin_and_oauth_secrets_have_fixed_trust_boundaries():
    provision = _step(
        "Provision eight isolated API-key profiles",
        "Split credentials into isolated one-profile manifests",
    )
    workers = _step(
        "Run eight independent headless Codex profile agents",
        "Verify independent Codex worker lifecycle and canonical inputs",
    )
    supervisor = _step(
        "Run intelligent Codex qualification aggregator",
        "Publish canonical result without following agent-created links",
    )
    scan = _step(
        "Scan public artifacts for secrets and raw evidence",
        "Cleanup every synthetic account",
    )
    for secret_name in (
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
    ):
        assert f"secrets.{secret_name}" in provision
        assert f"secrets.{secret_name}" in scan
        assert WORKFLOW.count(f"secrets.{secret_name}") == 2
        assert secret_name not in workers
        assert secret_name not in supervisor
    assert WORKFLOW.count("secrets.QA_TEST_ADMIN_TOKEN") == 5
    assert WORKFLOW.count("secrets.QA_CODEX_AUTH_JSON_B64") == 2
    assert "QA_TEST_ADMIN_TOKEN" not in workers
    assert "QA_TEST_ADMIN_TOKEN" not in supervisor
    assert "QA_CODEX_AUTH_JSON_B64" not in workers
    assert "QA_CODEX_AUTH_JSON_B64" not in supervisor
    assert "env -i" in supervisor
    for variable_name in (
        "QA_GEMINI_MODEL",
        "QA_KONGBEIQIE_MODEL",
        "QA_KONGBEIQIE_BASE_URL",
    ):
        assert f"vars.{variable_name}" in provision
        assert variable_name not in workers
        assert variable_name not in supervisor


def test_manifest_isolation_is_probed_for_all_eight_profiles():
    split = _step(
        "Split credentials into isolated one-profile manifests",
        "Verify every profile manifest permission boundary",
    )
    isolation = _step(
        "Verify every profile manifest permission boundary",
        "Run eight independent headless Codex profile agents",
    )
    assert "qa/split_profile_manifests.py" in split
    assert "profiles=(" in isolation
    assert "agent_types=(" in isolation
    assert 'own_manifest="${QA_PROFILE_MANIFEST_DIR}/${profile_id}.json"' in isolation
    assert 'sandbox -P "feedling-e2e-${profile_id}"' in isolation
    assert "stat -c" in isolation
    assert "os.O_WRONLY | os.O_APPEND" in isolation
    assert "denied_paths=(" in isolation
    assert "QA_PRIVATE_MANIFEST" in isolation
    assert "QA_WORKER_OUTPUT_ROOT" in isolation
    assert "QA_AGGREGATION_INPUT_ROOT" in isolation
    assert "QA_ORCHESTRATION_RECEIPT" in isolation
    assert "source-write-must-fail" in isolation
    for profile_id, agent_type in (
        ("official-deepseek", "profile_official_deepseek"),
        ("official-anthropic", "profile_official_anthropic"),
        ("official-openai", "profile_official_openai"),
        ("official-gemini", "profile_official_gemini"),
        ("openrouter-claude", "profile_openrouter_claude"),
        ("openrouter-openai", "profile_openrouter_openai"),
        ("openrouter-glm", "profile_openrouter_glm"),
        ("relay-kongbeiqie", "profile_relay_kongbeiqie"),
    ):
        assert f"            {profile_id}\n" in isolation
        assert f"            {agent_type}\n" in isolation


def test_deterministic_launcher_runs_exact_independent_profile_matrix():
    workers = _step(
        "Run eight independent headless Codex profile agents",
        "Verify independent Codex worker lifecycle and canonical inputs",
    )
    assert "qa/run_codex_profile_workers.py" in workers
    assert '--codex-home "$QA_CODEX_HOME"' in workers
    assert '--artifact-root "$QA_ARTIFACT_DIR"' in workers
    assert '--profile-manifest-dir "$QA_PROFILE_MANIFEST_DIR"' in workers
    assert '--worker-output-root "$QA_WORKER_OUTPUT_ROOT"' in workers
    assert '--aggregation-input-root "$QA_AGGREGATION_INPUT_ROOT"' in workers
    assert "qa/schemas/codex-run-result.schema.json" in workers
    assert '--receipt "$QA_ORCHESTRATION_RECEIPT"' in workers
    assert "--timeout-seconds 2400" in workers
    assert "timeout-minutes: 140" in workers
    assert "spawn_agent" not in workers
    assert "followup_task" not in workers
    assert "hook" not in workers.lower()


def test_real_codex_preflight_binds_the_locked_permission_profile():
    preflight = _step(
        "Install and verify isolated headless Codex runtime",
        "Provision eight isolated API-key profiles",
    )
    assert "-p profile_official_deepseek" in preflight
    assert "-c 'default_permissions=\"feedling-e2e-official-deepseek\"'" in preflight


def test_raw_worker_output_is_verified_but_not_exposed_to_aggregator():
    orchestration = _step(
        "Verify independent Codex worker lifecycle and canonical inputs",
        "Verify deployment identity remained stable after profile testing",
    )
    supervisor = _step(
        "Run intelligent Codex qualification aggregator",
        "Publish canonical result without following agent-created links",
    )
    assert "qa/verify_codex_orchestration.py" in orchestration
    assert "--receipt" in orchestration
    assert "--worker-output-root" in orchestration
    assert "--aggregation-input-root" in orchestration
    assert "QA_WORKER_OUTPUT_ROOT" not in supervisor
    assert "raw worker events/stderr" in supervisor
    assert "QA_AGGREGATION_INPUT_ROOT" in supervisor
    assert "QA_ORCHESTRATION_RECEIPT" in supervisor
    assert "--disable multi_agent" in supervisor
    assert "--disable network_proxy" in supervisor
    assert "launch another agent" in supervisor


def test_aggregator_preserves_semantic_and_cot_evidence_and_writes_privately():
    supervisor = _step(
        "Run intelligent Codex qualification aggregator",
        "Publish canonical result without following agent-created links",
    )
    for secret_name in (
        "QA_TEST_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
    ):
        assert secret_name not in supervisor
    assert "persona" in supervisor
    assert "reasoning/COT evidence" in supervisor
    assert "trace correlation" in supervisor
    assert "Copy all eight profile objects exactly" in supervisor
    assert "three fixed" in supervisor
    assert "batches (3+3+2)" in supervisor
    assert "profiles_expected and profiles_completed are both 8" in supervisor
    assert "must sum to eight" in supervisor
    assert "summary counts" in supervisor
    assert "--strict-config" in supervisor
    assert (
        '--output-schema "$GITHUB_WORKSPACE/qa/schemas/codex-run-result.schema.json"'
        in supervisor
    )
    assert '--output-last-message "$QA_PRIVATE_RESULT"' in supervisor
    assert "run-result.json" not in supervisor


def test_deployment_identity_is_checked_before_and_after_live_profile_agents():
    deployment_pre = _step(
        "Verify deployed backend and worker build identity before qualification",
        "Install and verify isolated headless Codex runtime",
    )
    deployment_post = _step(
        "Verify deployment identity remained stable after profile testing",
        "Run intelligent Codex qualification aggregator",
    )
    validate = _step(
        "Validate complete release result",
        "Scan public artifacts for secrets and raw evidence",
    )
    for deployment in (deployment_pre, deployment_post):
        assert "qa/verify_deployment.py" in deployment
        assert "secrets.QA_TEST_ADMIN_TOKEN" in deployment
        assert "deployment_receipt" in deployment
    assert "steps.orchestration.outcome == 'success'" in deployment_post
    assert "--deployment-receipt" in validate
    assert "--post-deployment-receipt" in validate
    assert "--orchestration-receipt" in validate


def test_agent_result_is_published_and_rendered_only_by_trusted_code():
    publish = _step(
        "Publish canonical result without following agent-created links",
        "Render trusted derived artifacts",
    )
    render = _step(
        "Render trusted derived artifacts",
        "Validate complete release result",
    )
    assert "qa/publish_agent_result.py" in publish
    assert '--source "${{ steps.context.outputs.private_result }}"' in publish
    assert (
        '--destination "${{ steps.context.outputs.artifact_dir }}/run-result.json"'
        in publish
    )
    assert "qa/render_artifacts.py" in render
    assert '--result "$QA_ARTIFACT_DIR/run-result.json"' in render
    assert "--schema qa/schemas/run-result.schema.json" in render


def test_secret_scan_includes_credentials_oauth_and_persona_privacy_fixture():
    scan = _step(
        "Scan public artifacts for secrets and raw evidence",
        "Cleanup every synthetic account",
    )
    assert "qa/scan_artifacts.py" in scan
    assert "--manifest" in scan
    assert "--codex-auth" in scan
    assert "--fixture qa/fixtures/persona-import-v1.json" in scan
    for secret_name in (
        "QA_TEST_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_CODEX_AUTH_JSON_B64",
    ):
        assert f"secrets.{secret_name}" in scan


def test_cleanup_diagnostic_upload_and_final_gate_are_fail_closed():
    cleanup = _step(
        "Cleanup every synthetic account",
        "Upload sanitized public qualification artifacts",
    )
    upload = _step(
        "Upload sanitized public qualification artifacts",
        "Remove public scratch after upload decision",
    )
    assert "if: always()" in cleanup
    assert "qa/provision_profiles.py cleanup" in cleanup
    assert "steps.secret_scan.outcome == 'success'" in upload
    assert "steps.cleanup.outcome == 'success'" in upload
    assert "steps.validate.outcome" not in upload
    assert "include-hidden-files: false" in upload
    assert "retention-days: 14" in upload
    assert "Enforce fail-closed qualification outcome" in WORKFLOW
    assert '"profile-workers:$PROFILE_WORKERS"' in WORKFLOW
    assert '"orchestration:$ORCHESTRATION"' in WORKFLOW
    assert '"validate:$VALIDATE"' in WORKFLOW
    assert '"secret-scan:$SECRET_SCAN"' in WORKFLOW
    assert "release qualification: PASS" in WORKFLOW

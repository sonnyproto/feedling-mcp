from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa import run_persona_memory_arm as supervisor


BUILD_SHA = "a" * 40


def _private_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _args(tmp_path: Path):
    private = _private_dir(tmp_path, "private")
    work = _private_dir(tmp_path, "work")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return SimpleNamespace(
        target_label="candidate",
        target_id="candidate-under-test",
        build_sha=BUILD_SHA,
        profile="official-openai",
        repetitions=3,
        concurrency=3,
        private_root=private,
        work_dir=work,
        artifact_dir=artifacts,
        judge_provider="openai",
        judge_model="judge-model",
        judge_base_url="https://api.openai.com/v1",
        judge_api_key_env="QA_EVAL_JUDGE_API_KEY",
        judge_id="persona-memory-judge-v1",
        judge_configuration_id="rubric-v1",
        allow_private_judge_egress=True,
    )


def _fake_runner(
    commands,
    *,
    run_code: int,
    provision_code: int = 0,
    write_provision_manifest: bool = True,
    write_blocked_result: bool = False,
    result_status: str | None = None,
    finalize_code: int = 0,
):
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(command, *, env):
        command = list(command)
        seen.append((command, dict(env)))
        script = Path(command[1]).name
        subcommand = command[2] if len(command) > 2 else ""

        def value(flag: str) -> Path:
            return Path(command[command.index(flag) + 1])

        if script == "provision_profiles.py" and subcommand == "provision-pool":
            if write_provision_manifest:
                value("--manifest").write_text("pool", encoding="utf-8")
            return provision_code
        elif script == "provision_profiles.py" and subcommand == "cleanup":
            value("--manifest").unlink()
        elif script == "verify_deployment.py":
            value("--receipt").write_text("deployment", encoding="utf-8")
        elif script == "prepare_persona_memory_accounts.py" and subcommand == "prepare":
            value("--post-deployment-receipt").write_text(
                "import-post", encoding="utf-8"
            )
            value("--readiness-receipt").write_text("ready", encoding="utf-8")
        elif script == "run_persona_memory_regression.py" and subcommand == "run-live":
            if run_code in {0, 1} or write_blocked_result:
                status = result_status or {
                    0: "PASS",
                    1: "FAIL",
                    2: "BLOCKED_EVIDENCE",
                }[run_code]
                output = value("--output")
                output.write_text(json.dumps({"status": status}), encoding="utf-8")
                output.chmod(0o600)
            return run_code
        elif script == "prepare_persona_memory_accounts.py" and subcommand == "cleanup":
            value("--account-pool").unlink()
            value("--receipt").write_text("cleanup", encoding="utf-8")
        elif script == "run_persona_memory_regression.py" and subcommand == "finalize-arm":
            status = result_status or {
                0: "PASS",
                1: "FAIL",
                2: "BLOCKED_EVIDENCE",
            }[run_code]
            output = value("--output")
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "persona_memory_arm_run",
                        "result_status": status,
                    }
                ),
                encoding="utf-8",
            )
            output.chmod(0o600)
            return finalize_code
        return 0

    return run, seen


def test_product_regression_still_post_verifies_cleans_and_finalizes(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=1)
    env = {
        "QA_TEST_ADMIN_TOKEN": "admin-secret",
        "QA_OPENAI_PROVIDER_API_KEY": "provider-secret",
        "QA_EVAL_JUDGE_API_KEY": "judge-secret",
    }

    code = supervisor.run_arm(args, env=env, step_runner=runner)

    assert code == 1
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    run_index = scripts.index(("run_persona_memory_regression.py", "run-live"))
    assert scripts[run_index + 1] == ("verify_deployment.py", "--expected-sha")
    assert scripts[run_index + 2] == (
        "prepare_persona_memory_accounts.py",
        "cleanup",
    )
    assert scripts[-1] == ("run_persona_memory_regression.py", "finalize-arm")
    run_env = seen[run_index][1]
    assert "QA_TEST_ADMIN_TOKEN" not in run_env
    assert "QA_OPENAI_PROVIDER_API_KEY" not in run_env
    assert run_env["QA_EVAL_JUDGE_API_KEY"] == "judge-secret"
    assert (args.private_root / "account-pool.json").exists() is False
    assert (args.private_root / "arm-receipt.json").is_file()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "FAIL"
    assert summary["arm_finalized"] is True


def test_operational_run_failure_still_executes_post_and_cleanup(tmp_path, capsys):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=2)

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("verify_deployment.py", "--expected-sha") in scripts
    assert (
        "prepare_persona_memory_accounts.py",
        "cleanup",
    ) in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") not in scripts
    assert (args.private_root / "account-pool.json").exists() is False
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_blocked_evidence_result_is_cleaned_and_finalized_but_returns_two(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=2, write_blocked_result=True)

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") in scripts
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is True


def test_failed_provisioning_retries_partial_pool_cleanup(tmp_path, capsys):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=0, provision_code=2)

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert scripts == [
        ("provision_profiles.py", "provision-pool"),
        ("provision_profiles.py", "cleanup"),
    ]
    assert not (args.private_root / "account-pool.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["provision_cleanup_complete"] is True
    assert summary["provision_reaper_pending"] is False


def test_failed_provision_without_manifest_never_claims_immediate_cleanup(
    tmp_path, capsys
):
    args = _args(tmp_path)
    runner, seen = _fake_runner(
        [],
        run_code=0,
        provision_code=2,
        write_provision_manifest=False,
    )

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert scripts == [("provision_profiles.py", "provision-pool")]
    summary = json.loads(capsys.readouterr().out)
    assert summary["provision_cleanup_complete"] is False
    assert summary["provision_reaper_pending"] is True


@pytest.mark.parametrize(
    ("run_code", "result_status"),
    [(0, "FAIL"), (2, "PASS")],
)
def test_run_exit_must_match_private_result_before_finalization(
    tmp_path, capsys, run_code, result_status
):
    args = _args(tmp_path)
    runner, seen = _fake_runner(
        [],
        run_code=run_code,
        write_blocked_result=True,
        result_status=result_status,
    )

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=runner,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    assert ("run_persona_memory_regression.py", "finalize-arm") not in scripts
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_post_verify_launch_error_does_not_bypass_cleanup(tmp_path, capsys):
    args = _args(tmp_path)
    runner, seen = _fake_runner([], run_code=0)
    verify_calls = 0

    def fail_run_post(command, *, env):
        nonlocal verify_calls
        if Path(command[1]).name == "verify_deployment.py":
            verify_calls += 1
            if verify_calls == 3:
                raise supervisor.ArmSupervisorError("could not launch post verify")
        return runner(command, env=env)

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=fail_run_post,
    )

    assert code == 2
    scripts = [(Path(command[1]).name, command[2]) for command, _env in seen]
    assert ("prepare_persona_memory_accounts.py", "cleanup") in scripts
    assert not (args.private_root / "account-pool.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_nonzero_finalize_discards_written_arm_receipt(tmp_path, capsys):
    args = _args(tmp_path)
    runner, _seen = _fake_runner([], run_code=0, finalize_code=2)

    code = supervisor.run_arm(
        args,
        env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
        step_runner=runner,
    )

    assert code == 2
    assert not (args.private_root / "arm-receipt.json").exists()
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "BLOCKED"
    assert summary["arm_finalized"] is False


def test_supervisor_rejects_nonempty_private_root_before_subprocess(tmp_path):
    args = _args(tmp_path)
    (args.private_root / "occupied").write_text("do not overwrite", encoding="utf-8")
    calls = []

    def must_not_run(command, *, env):
        calls.append(command)
        return 0

    try:
        supervisor.run_arm(
            args,
            env={"QA_EVAL_JUDGE_API_KEY": "judge-secret"},
            step_runner=must_not_run,
        )
    except supervisor.ArmSupervisorError as exc:
        assert "start empty" in str(exc)
    else:
        raise AssertionError("unsafe private root was accepted")
    assert calls == []


def test_supervisor_rejects_reused_judge_and_admin_secret_values(tmp_path):
    args = _args(tmp_path)
    calls = []

    def must_not_run(command, *, env):
        calls.append(command)
        return 0

    with pytest.raises(supervisor.ArmSupervisorError, match="secret values"):
        supervisor.run_arm(
            args,
            env={
                "QA_EVAL_JUDGE_API_KEY": "same-secret",
                "QA_TEST_ADMIN_TOKEN": "same-secret",
            },
            step_runner=must_not_run,
        )

    assert calls == []

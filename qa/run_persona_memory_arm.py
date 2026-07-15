#!/usr/bin/env python3
"""Run one formal live persona-memory arm with unconditional cleanup.

This trusted supervisor keeps provisioning/admin credentials out of the
conversation-runner subprocess, preserves a product-regression exit code, and
still performs post-deployment verification, recoverable pool cleanup, and arm
finalization when the live evaluation returns non-zero.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.regression.live_accounts import (  # noqa: E402
    LiveAccountContractError,
    read_private_json,
)


_QA_ROOT = _REPO_ROOT / "qa"
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROVIDER_SECRET_ENVS = {
    "QA_ANTHROPIC_API_KEY",
    "QA_DEEPSEEK_API_KEY",
    "QA_GEMINI_API_KEY",
    "QA_KONGBEIQIE_API_KEY",
    "QA_OPENAI_PROVIDER_API_KEY",
    "QA_OPENROUTER_API_KEY",
}


class ArmSupervisorError(RuntimeError):
    """A bounded local orchestration error safe to print."""


@dataclass(frozen=True, slots=True)
class ArmPaths:
    pool: Path
    import_pre: Path
    import_post: Path
    readiness: Path
    run_pre: Path
    result: Path
    run_post: Path
    cleanup: Path
    arm: Path


def _owner_directory(path: Path, *, empty: bool) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ArmSupervisorError("private directories must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise ArmSupervisorError("private directory is unavailable") from None
    if (
        resolved != candidate
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ArmSupervisorError("private directory must be owner-controlled mode 0700")
    if empty:
        try:
            if any(resolved.iterdir()):
                raise ArmSupervisorError("private directory must start empty")
        except OSError:
            raise ArmSupervisorError("private directory is unreadable") from None
    return resolved


def _artifact_directory(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ArmSupervisorError("artifact scratch directory must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ArmSupervisorError("artifact scratch directory is unavailable") from None
    if resolved != candidate or not resolved.is_dir():
        raise ArmSupervisorError("artifact scratch directory is invalid")
    return resolved


def _paths(root: Path) -> ArmPaths:
    return ArmPaths(
        pool=root / "account-pool.json",
        import_pre=root / "import-deployment-pre.json",
        import_post=root / "import-deployment-post.json",
        readiness=root / "account-readiness.json",
        run_pre=root / "deployment-pre.json",
        result=root / "result.json",
        run_post=root / "deployment-post.json",
        cleanup=root / "account-cleanup.json",
        arm=root / "arm-receipt.json",
    )


def _run_step(command: Sequence[str], *, env: Mapping[str, str]) -> int:
    try:
        completed = subprocess.run(
            list(command),
            cwd=_REPO_ROOT,
            env=dict(env),
            check=False,
        )
    except OSError:
        raise ArmSupervisorError("an orchestration subprocess could not start") from None
    return int(completed.returncode)


def _attempt_step(
    step_runner: Callable[..., int],
    command: Sequence[str],
    *,
    env: Mapping[str, str],
) -> int | None:
    """Keep one subprocess-launch failure from bypassing later cleanup steps."""
    try:
        return int(step_runner(command, env=env))
    except Exception:
        return None


def _private_result_status(path: Path) -> str:
    try:
        document, _digest = read_private_json(
            path,
            label="private experiment result",
            max_bytes=64 * 1024 * 1024,
        )
    except LiveAccountContractError:
        raise ArmSupervisorError("private experiment result is invalid") from None
    status = document.get("status")
    if status not in {"PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR"}:
        raise ArmSupervisorError("private experiment result status is invalid")
    return str(status)


def _private_arm_status(path: Path) -> str:
    try:
        document, _digest = read_private_json(path, label="arm run receipt")
    except LiveAccountContractError:
        raise ArmSupervisorError("arm run receipt is invalid") from None
    status = document.get("result_status")
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "persona_memory_arm_run"
        or status not in {"PASS", "FAIL", "BLOCKED_EVIDENCE", "INFRA_ERROR"}
    ):
        raise ArmSupervisorError("arm run receipt status is invalid")
    return str(status)


def _discard_arm_receipt(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _result_exit_code(status: str) -> int:
    if status == "PASS":
        return 0
    if status == "FAIL":
        return 1
    return 2


def _script(name: str, *arguments: str) -> list[str]:
    return [sys.executable, str(_QA_ROOT / name), *arguments]


def _runner_environment(env: Mapping[str, str]) -> dict[str, str]:
    sanitized = dict(env)
    sanitized.pop("QA_TEST_ADMIN_TOKEN", None)
    for name in _PROVIDER_SECRET_ENVS:
        sanitized.pop(name, None)
    return sanitized


def run_arm(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str] | None = None,
    step_runner=_run_step,
) -> int:
    active_env = dict(os.environ if env is None else env)
    if _BUILD_SHA_RE.fullmatch(str(args.build_sha or "")) is None:
        raise ArmSupervisorError("build SHA must be a full lowercase digest")
    if not args.allow_private_judge_egress:
        raise ArmSupervisorError("semantic judge plaintext egress was not authorized")
    if args.judge_api_key_env in _PROVIDER_SECRET_ENVS | {"QA_TEST_ADMIN_TOKEN"}:
        raise ArmSupervisorError(
            "judge credentials must be distinct from provisioning/admin credentials"
        )
    judge_secret = str(active_env.get(args.judge_api_key_env) or "").strip()
    if not judge_secret:
        raise ArmSupervisorError("semantic judge API key environment is empty")
    protected_secret_names = _PROVIDER_SECRET_ENVS | {"QA_TEST_ADMIN_TOKEN"}
    if any(
        hmac.compare_digest(judge_secret, str(active_env.get(name) or ""))
        for name in protected_secret_names
        if str(active_env.get(name) or "")
    ):
        raise ArmSupervisorError(
            "judge credentials must not reuse provisioning/admin secret values"
        )
    private_root = _owner_directory(args.private_root, empty=True)
    work_dir = _owner_directory(args.work_dir, empty=True)
    artifact_dir = _artifact_directory(args.artifact_dir)
    if private_root == work_dir:
        raise ArmSupervisorError("private result and import work directories must differ")
    try:
        if any(
            os.path.commonpath([str(path), str(artifact_dir)]) == str(artifact_dir)
            for path in (private_root, work_dir)
        ):
            raise ArmSupervisorError(
                "private result and work directories must be outside artifacts"
            )
    except ValueError:
        raise ArmSupervisorError("private/artifact path boundary is invalid") from None
    paths = _paths(private_root)
    target_id = args.target_id or f"{args.target_label}-{args.build_sha}"
    account_count = args.repetitions * 8
    common_verify = [
        "--expected-sha",
        args.build_sha,
        "--expected-runtime",
        "hosted_resident",
    ]

    pool_created = False
    preparation_started = False
    run_code = 2
    result_status: str | None = None
    run_exit_consistent = False
    arm_finalized = False
    operational_failure = False

    provision = _script(
        "provision_profiles.py",
        "provision-pool",
        "--profile",
        args.profile,
        "--count",
        str(account_count),
        "--require-runtime-v2",
        "--manifest",
        str(paths.pool),
    )
    provision_code = _attempt_step(step_runner, provision, env=active_env)
    if provision_code != 0 or not paths.pool.is_file():
        recovery_ok = False
        if paths.pool.exists():
            recover_partial_pool = _script(
                "provision_profiles.py",
                "cleanup",
                "--manifest",
                str(paths.pool),
            )
            recovery_ok = (
                _attempt_step(step_runner, recover_partial_pool, env=active_env) == 0
                and not paths.pool.exists()
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "BLOCKED",
                    "run_exit_code": 2,
                    "arm_finalized": False,
                    "provision_cleanup_complete": recovery_ok,
                    "provision_reaper_pending": not recovery_ok,
                    "private_root": str(private_root),
                    "result": str(paths.result),
                    "arm_receipt": str(paths.arm),
                },
                sort_keys=True,
            )
        )
        return 2
    pool_created = True

    try:
        import_pre = _script(
            "verify_deployment.py",
            *common_verify,
            "--receipt",
            str(paths.import_pre),
        )
        if (
            _attempt_step(step_runner, import_pre, env=active_env) != 0
            or not paths.import_pre.is_file()
        ):
            operational_failure = True
        else:
            preparation_started = True
            prepare = _script(
                "prepare_persona_memory_accounts.py",
                "prepare",
                "--account-pool",
                str(paths.pool),
                "--build-sha",
                args.build_sha,
                "--deployment-receipt",
                str(paths.import_pre),
                "--post-deployment-receipt",
                str(paths.import_post),
                "--work-dir",
                str(work_dir),
                "--artifact-dir",
                str(artifact_dir),
                "--readiness-receipt",
                str(paths.readiness),
                "--concurrency",
                str(args.concurrency),
            )
            if (
                _attempt_step(step_runner, prepare, env=active_env) != 0
                or not paths.readiness.is_file()
                or not paths.import_post.is_file()
                or not paths.pool.is_file()
            ):
                operational_failure = True

        if not operational_failure:
            run_pre = _script(
                "verify_deployment.py",
                *common_verify,
                "--receipt",
                str(paths.run_pre),
            )
            if (
                _attempt_step(step_runner, run_pre, env=active_env) != 0
                or not paths.run_pre.is_file()
            ):
                operational_failure = True

        if not operational_failure:
            run = _script(
                "run_persona_memory_regression.py",
                "run-live",
                "--target-id",
                target_id,
                "--target-label",
                args.target_label,
                "--build-sha",
                args.build_sha,
                "--deployment-receipt",
                str(paths.run_pre),
                "--account-pool",
                str(paths.pool),
                "--readiness-receipt",
                str(paths.readiness),
                "--external-cleanup-guaranteed",
                "--repetitions",
                str(args.repetitions),
                "--concurrency",
                str(args.concurrency),
                "--judge-provider",
                args.judge_provider,
                "--judge-model",
                args.judge_model,
                "--judge-base-url",
                args.judge_base_url,
                "--judge-api-key-env",
                args.judge_api_key_env,
                "--judge-id",
                args.judge_id,
                "--judge-configuration-id",
                args.judge_configuration_id,
                "--allow-private-judge-egress",
                "--output",
                str(paths.result),
            )
            observed_run_code = _attempt_step(
                step_runner, run, env=_runner_environment(active_env)
            )
            run_code = observed_run_code if observed_run_code is not None else 2
            if not paths.result.is_file():
                operational_failure = True
            else:
                try:
                    result_status = _private_result_status(paths.result)
                except ArmSupervisorError:
                    operational_failure = True
                else:
                    run_exit_consistent = (
                        run_code in {0, 1, 2}
                        and _result_exit_code(result_status) == run_code
                    )
                    if not run_exit_consistent or run_code == 2:
                        operational_failure = True
    finally:
        post_ok = False
        cleanup_ok = False
        if pool_created and preparation_started:
            run_post = _script(
                "verify_deployment.py",
                *common_verify,
                "--receipt",
                str(paths.run_post),
            )
            post_ok = (
                _attempt_step(step_runner, run_post, env=active_env) == 0
                and paths.run_post.is_file()
            )
            if not post_ok:
                operational_failure = True

        if pool_created and paths.pool.exists():
            cleanup = _script(
                "prepare_persona_memory_accounts.py",
                "cleanup",
                "--account-pool",
                str(paths.pool),
                "--receipt",
                str(paths.cleanup),
            )
            cleanup_ok = (
                _attempt_step(step_runner, cleanup, env=active_env) == 0
                and paths.cleanup.is_file()
                and not paths.pool.exists()
            )
            if not cleanup_ok:
                operational_failure = True
        elif paths.cleanup.is_file():
            cleanup_ok = True
        elif pool_created:
            operational_failure = True

        required = (
            paths.result,
            paths.readiness,
            paths.import_pre,
            paths.import_post,
            paths.run_pre,
            paths.run_post,
            paths.cleanup,
        )
        if (
            run_exit_consistent
            and post_ok
            and cleanup_ok
            and all(path.is_file() for path in required)
        ):
            finalize = _script(
                "run_persona_memory_regression.py",
                "finalize-arm",
                "--result",
                str(paths.result),
                "--readiness-receipt",
                str(paths.readiness),
                "--import-pre-deployment-receipt",
                str(paths.import_pre),
                "--import-post-deployment-receipt",
                str(paths.import_post),
                "--pre-deployment-receipt",
                str(paths.run_pre),
                "--post-deployment-receipt",
                str(paths.run_post),
                "--cleanup-receipt",
                str(paths.cleanup),
                "--output",
                str(paths.arm),
            )
            finalize_code = _attempt_step(step_runner, finalize, env=active_env)
            if finalize_code != 0 or not paths.arm.is_file():
                operational_failure = True
                _discard_arm_receipt(paths.arm)
            else:
                try:
                    arm_status = _private_arm_status(paths.arm)
                except ArmSupervisorError:
                    operational_failure = True
                    _discard_arm_receipt(paths.arm)
                else:
                    arm_finalized = arm_status == result_status
                    if not arm_finalized:
                        operational_failure = True
                        _discard_arm_receipt(paths.arm)
        elif paths.result.exists():
            operational_failure = True

    status = "BLOCKED" if operational_failure else ("PASS" if run_code == 0 else "FAIL")
    print(
        json.dumps(
            {
                "ok": status == "PASS",
                "status": status,
                "run_exit_code": run_code,
                "arm_finalized": arm_finalized,
                "private_root": str(private_root),
                "result": str(paths.result),
                "arm_receipt": str(paths.arm),
            },
            sort_keys=True,
        )
    )
    return 2 if operational_failure else run_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-label", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--target-id", default="")
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--profile", default="official-openai")
    parser.add_argument("--repetitions", type=int, choices=(1, 3), default=3)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--judge-provider", required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--judge-base-url", required=True)
    parser.add_argument("--judge-api-key-env", required=True)
    parser.add_argument("--judge-id", required=True)
    parser.add_argument("--judge-configuration-id", required=True)
    parser.add_argument(
        "--allow-private-judge-egress",
        action="store_true",
        required=True,
        help="authorize plaintext trajectory egress to the configured semantic judge",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.concurrency < 1:
            raise ArmSupervisorError("concurrency must be positive")
        return run_arm(args)
    except ArmSupervisorError as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

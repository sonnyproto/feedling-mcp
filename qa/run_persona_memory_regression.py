#!/usr/bin/env python3
"""Validate, run, and compare Feedling persona/memory regression experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.atomic_private_file import AtomicPrivateFileError, create_private_file  # noqa: E402
from qa.live_scenario_probe import (  # noqa: E402
    LOCKED_BASE_URL,
    LiveScenarioProbeError,
    load_profile,
)
from qa.regression.compare import ComparisonError, compare_results  # noqa: E402
from qa.regression.contracts import (  # noqa: E402
    ContractError,
    ExperimentTarget,
    canonical_json_sha256,
)
from qa.regression.engine import run_experiment, validate_suite_contract  # noqa: E402
from qa.regression.judge import (  # noqa: E402
    HttpJsonJudge,
    JudgeError,
    ProviderClientJudge,
)
from qa.regression.report import ReportError, write_reports  # noqa: E402
from qa.regression.scenario_loader import (  # noqa: E402
    LoaderError,
    RegressionSuite,
    load_experiment_result,
    load_suite,
    load_suite_directory,
    read_contract_json,
    verify_source_fixture,
)
from qa.regression.target import FeedlingTarget, TargetContext  # noqa: E402


DEFAULT_PERSONA = Path("qa/regression/fixtures/golden-persona-mira-v1.json")
DEFAULT_SCENARIO_ROOT = Path("qa/regression/scenarios")
DEFAULT_SOURCE_FIXTURE = Path("qa/fixtures/persona-import-v1.json")
_BUILD_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class CommandError(RuntimeError):
    """A bounded operator error that contains no account credentials."""


class _SessionPool:
    def __init__(self, rows: list[tuple[dict[str, Any], Any]]) -> None:
        self._rows = list(rows)
        self._lock = threading.Lock()

    def checkout(self, _context: TargetContext) -> Any:
        with self._lock:
            if not self._rows:
                raise CommandError("isolated session pool is exhausted")
            _profile, session = self._rows.pop(0)
            return session


def _private_output_parent(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    parent = candidate.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = parent.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise CommandError("private output parent is unavailable") from None
    if (
        resolved != parent
        or metadata.st_uid != os.geteuid()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CommandError("private output parent must be owner-controlled mode 0700")
    return resolved / candidate.name


def _manifest_spec(value: str) -> tuple[str, Path]:
    profile_id, separator, raw_path = value.partition("=")
    if not separator or not profile_id or not raw_path:
        raise argparse.ArgumentTypeError("expected PROFILE_ID=/absolute/manifest.json")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("profile manifest path must be absolute")
    return profile_id, path


def _load_manifest_sessions(
    specs: Sequence[tuple[str, Path]],
) -> list[tuple[dict[str, Any], Any]]:
    rows = [load_profile(path, profile_id) for profile_id, path in specs]
    users = [session.user_id for _profile, session in rows]
    if len(users) != len(set(users)):
        raise CommandError("profile manifests reuse a synthetic account")
    routes = {
        (
            str(profile.get("provider") or ""),
            str(profile.get("configured_model") or ""),
            str(profile.get("configured_base_url") or ""),
            str(profile.get("runtime_mode") or ""),
            str(profile.get("runtime_version") or ""),
            str(profile.get("reasoning_effort") or ""),
            str(profile.get("trace_enabled") or ""),
        )
        for profile, _session in rows
    }
    if len(routes) != 1 or any(not value for value in next(iter(routes), ())):
        raise CommandError("profile manifests must use one fully identified target route")
    return rows


def _deployment_receipt(
    path: Path, *, expected_sha: str, expected_runtime: str
) -> tuple[dict[str, Any], str]:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise CommandError("deployment receipt path must be an absolute regular file")
    try:
        metadata = candidate.stat(follow_symlinks=False)
    except OSError:
        raise CommandError("deployment receipt is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
        or metadata.st_nlink != 1
    ):
        raise CommandError("deployment receipt is not owner-controlled")
    receipt = read_contract_json(candidate, max_bytes=64 * 1024)
    if not isinstance(receipt, dict):
        raise CommandError("deployment receipt is invalid")
    sha = expected_sha.lower()
    worker_sha = receipt.get("observed_worker_sha")
    try:
        verified_at = datetime.fromisoformat(
            str(receipt.get("verified_at") or "").replace("Z", "+00:00")
        )
        receipt_age = (datetime.now(timezone.utc) - verified_at).total_seconds()
    except (TypeError, ValueError):
        receipt_age = -1
    if (
        _BUILD_SHA_RE.fullmatch(sha) is None
        or receipt.get("schema_version") != 1
        or receipt.get("environment") != "test"
        or receipt.get("base_url") != LOCKED_BASE_URL
        or receipt.get("expected_runtime") != expected_runtime
        or receipt.get("expected_deployment_sha") != sha
        or receipt.get("observed_backend_sha") != sha
        or receipt.get("observed_deployment_sha") != sha
        or receipt.get("liveness_verified") is not True
        or receipt.get("deployment_identity_verified") is not True
        or not 0 <= receipt_age <= 1800
        or (
            expected_runtime == "hosted_resident"
            and (
                worker_sha != sha
                or type(receipt.get("live_worker_count")) is not int
                or receipt["live_worker_count"] < 1
            )
        )
        or (
            expected_runtime != "hosted_resident"
            and (
                worker_sha is not None
                or receipt.get("live_worker_count") is not None
            )
        )
    ):
        raise CommandError("deployment receipt does not prove the requested build")
    return receipt, canonical_json_sha256(receipt)


def _required_sessions(suite: RegressionSuite, repetitions: int) -> int:
    per_repeat = 0
    for scenario in suite.scenarios:
        # Strong scenarios block before mutation when the live target has no
        # runtime rotation evidence provider, so they consume no account.
        if "persistent_memory_strong" in scenario.requirements:
            continue
        per_repeat += len({turn.session_key for turn in scenario.turns})
    return per_repeat * repetitions


def _selected_suite(args: argparse.Namespace) -> RegressionSuite:
    if args.scenario:
        suite = load_suite(args.persona, list(args.scenario))
    else:
        all_scenarios = load_suite_directory(args.persona, args.scenario_root)
        selected = tuple(
            scenario
            for scenario in all_scenarios.scenarios
            if args.include_nightly or scenario.metadata.get("lane") == "release"
        )
        if not selected:
            raise CommandError("no scenarios selected")
        suite = RegressionSuite(persona=all_scenarios.persona, scenarios=selected)
    verify_source_fixture(suite.persona, args.source_fixture)
    validate_suite_contract(suite)
    return suite


def _judge(args: argparse.Namespace) -> HttpJsonJudge | ProviderClientJudge | None:
    if args.judge_endpoint and args.judge_provider:
        raise CommandError("choose either a generic judge endpoint or a provider judge")
    if not args.judge_endpoint and not args.judge_provider:
        return None
    if not args.allow_private_judge_egress:
        raise CommandError(
            "external judge requires --allow-private-judge-egress because requests contain plaintext trajectories"
        )
    if not args.judge_id or not args.judge_configuration_id:
        raise CommandError(
            "external judge requires --judge-id and --judge-configuration-id"
        )
    if args.judge_provider:
        if not args.judge_model or not args.judge_base_url or not args.judge_api_key_env:
            raise CommandError(
                "provider judge requires model, base URL, and API key environment name"
            )
        api_key = os.environ.get(args.judge_api_key_env, "")
        if not api_key:
            raise CommandError("provider judge API key environment is empty")
        return ProviderClientJudge(
            judge_id=args.judge_id,
            provider=args.judge_provider,
            model=args.judge_model,
            base_url=args.judge_base_url,
            api_key=api_key,
            timeout_seconds=args.judge_timeout,
            max_tokens=args.judge_max_tokens,
            configuration_id=args.judge_configuration_id,
        )
    token = os.environ.get(args.judge_token_env, "") if args.judge_token_env else ""
    return HttpJsonJudge(
        judge_id=args.judge_id,
        endpoint=args.judge_endpoint,
        bearer_token=token,
        timeout_seconds=args.judge_timeout,
        allow_insecure_localhost=args.allow_insecure_local_judge,
        configuration_id=args.judge_configuration_id,
    )


def _cmd_validate(args: argparse.Namespace) -> int:
    suite = _selected_suite(args)
    print(
        json.dumps(
            {
                "ok": True,
                "persona_id": suite.persona.persona_id,
                "persona_version": suite.persona.persona_version,
                "persona_fixture_sha256": suite.persona.fixture_sha256,
                "rubric_sha256": suite.rubric_sha256,
                "evaluation_contract_sha256": suite.evaluation_contract_sha256,
                "scenario_fingerprints": suite.scenario_fingerprints,
                "sessions_per_repetition": _required_sessions(suite, 1),
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.accounts_ready:
        raise CommandError("--accounts-ready is required for live mutation")
    suite = _selected_suite(args)
    if any("persistent_memory_strong" in item.requirements for item in suite.scenarios):
        raise CommandError(
            "run-live does not yet have a deployment-specific runtime session rotator; strong nightly scenarios cannot run"
        )
    semantic_judge = _judge(args)
    sessions = _load_manifest_sessions(args.manifest_profile)
    required = _required_sessions(suite, args.repetitions)
    if len(sessions) < required:
        raise CommandError(
            f"isolated session pool is too small: required={required} available={len(sessions)}"
        )
    if args.cleanup_account and len(sessions) != required:
        raise CommandError(
            "--cleanup-account requires exactly the accounts consumed by this run"
        )
    first_profile = sessions[0][0]
    account_fingerprints = sorted(
        hashlib.sha256(str(session.user_id).encode("utf-8")).hexdigest()
        for _profile, session in sessions[:required]
    )
    expected_runtime = str(first_profile.get("runtime_mode") or "")
    _receipt, receipt_sha256 = _deployment_receipt(
        args.deployment_receipt,
        expected_sha=args.build_sha,
        expected_runtime=expected_runtime,
    )
    pool = _SessionPool(sessions)

    def cleanup(client: Any, session: Any) -> None:
        client.reset_account(session)

    target_adapter = FeedlingTarget(
        target_id=args.target_id,
        base_url=LOCKED_BASE_URL,
        session_factory=pool.checkout,
        session_closer=cleanup if args.cleanup_account else None,
        external_cleanup_guaranteed=args.external_cleanup_guaranteed,
    )
    target = ExperimentTarget(
        target_id=args.target_id,
        label=args.target_label,
        base_url=LOCKED_BASE_URL,
        build_sha=args.build_sha,
        runtime_mode=str(first_profile.get("runtime_mode") or "unknown"),
        provider=str(first_profile.get("provider") or "unknown"),
        model=str(first_profile.get("configured_model") or "unknown"),
        configuration={
            "configured_base_url": str(
                first_profile.get("configured_base_url") or ""
            ),
            "reasoning_effort": str(first_profile.get("reasoning_effort") or ""),
            "runtime_version": first_profile.get("runtime_version"),
            "trace_enabled": first_profile.get("trace_enabled") is True,
        },
    )
    experiment_id = args.experiment_id or f"persona-memory-{uuid.uuid4().hex}"
    result = run_experiment(
        suite=suite,
        target_adapter=target_adapter,
        target=target,
        experiment_id=experiment_id,
        repetitions=args.repetitions,
        concurrency=args.concurrency,
        judge=semantic_judge,
        metadata={
            "deployment_receipt_sha256": receipt_sha256,
            "account_readiness": "operator_attested",
            "account_fingerprints": account_fingerprints,
            "source_bundle_sha256": suite.persona.source_fixture_sha256,
        },
    )
    output = _private_output_parent(args.output)
    content = (
        json.dumps(
            result.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    create_private_file(output, content)
    print(
        json.dumps(
            {
                "ok": result.status == "PASS",
                "status": result.status,
                "experiment_id": result.experiment_id,
                "trajectory_count": len(result.trajectories),
                "metric_count": len(result.metric_results),
                "private_result": str(output),
            },
            sort_keys=True,
        )
    )
    return 0 if result.status == "PASS" else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline = load_experiment_result(args.baseline)
    candidate = load_experiment_result(args.candidate)
    comparison = compare_results(
        baseline,
        candidate,
        baseline_target_id=args.baseline_target_id,
        candidate_target_id=args.candidate_target_id,
        required_pass_rate=args.required_pass_rate,
        max_score_drop=args.max_score_drop,
    )
    write_reports(comparison, args.output_dir)
    print(
        json.dumps(
            {
                "ok": comparison["status"] == "PASS",
                "status": comparison["status"],
                "summary": comparison["summary"],
                "artifact_dir": str(args.output_dir),
            },
            sort_keys=True,
        )
    )
    return 0 if comparison["status"] == "PASS" else 1


def _suite_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--persona", type=Path, default=DEFAULT_PERSONA)
    parser.add_argument("--source-fixture", type=Path, default=DEFAULT_SOURCE_FIXTURE)
    parser.add_argument("--scenario-root", type=Path, default=DEFAULT_SCENARIO_ROOT)
    parser.add_argument("--scenario", type=Path, action="append")
    parser.add_argument(
        "--include-nightly",
        action="store_true",
        help="include strong-boundary/nightly scenarios when --scenario is omitted",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate and fingerprint fixtures")
    _suite_arguments(validate)
    validate.set_defaults(handler=_cmd_validate)

    run = commands.add_parser(
        "run-live",
        help="run against already-provisioned, persona-imported one-profile manifests",
    )
    _suite_arguments(run)
    run.add_argument(
        "--manifest-profile",
        action="append",
        type=_manifest_spec,
        required=True,
        metavar="PROFILE_ID=/ABSOLUTE/MANIFEST.json",
    )
    run.add_argument("--target-id", required=True)
    run.add_argument("--target-label", choices=("baseline", "candidate"), required=True)
    run.add_argument("--build-sha", required=True)
    run.add_argument("--deployment-receipt", type=Path, required=True)
    run.add_argument("--experiment-id", default="")
    run.add_argument("--repetitions", type=int, default=3)
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--accounts-ready",
        action="store_true",
        help="confirm persona import, hosting, and chat gate already succeeded",
    )
    cleanup = run.add_mutually_exclusive_group(required=True)
    cleanup.add_argument("--cleanup-account", action="store_true")
    cleanup.add_argument("--external-cleanup-guaranteed", action="store_true")
    run.add_argument("--judge-endpoint", default="")
    run.add_argument("--judge-provider", default="")
    run.add_argument("--judge-model", default="")
    run.add_argument("--judge-base-url", default="")
    run.add_argument("--judge-api-key-env", default="")
    run.add_argument("--judge-id", default="")
    run.add_argument("--judge-configuration-id", default="")
    run.add_argument("--judge-token-env", default="")
    run.add_argument("--judge-timeout", type=float, default=120.0)
    run.add_argument("--judge-max-tokens", type=int, default=2400)
    run.add_argument("--allow-private-judge-egress", action="store_true")
    run.add_argument("--allow-insecure-local-judge", action="store_true")
    run.set_defaults(handler=_cmd_run)

    compare = commands.add_parser(
        "compare", help="compare no-overwrite private baseline and candidate results"
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--baseline-target-id")
    compare.add_argument("--candidate-target-id")
    compare.add_argument("--required-pass-rate", type=float, default=1.0)
    compare.add_argument("--max-score-drop", type=float, default=0.0)
    compare.add_argument("--output-dir", type=Path, required=True)
    compare.set_defaults(handler=_cmd_compare)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "repetitions", 1) < 1 or getattr(args, "concurrency", 1) < 1:
            raise CommandError("repetitions and concurrency must be positive")
        return int(args.handler(args))
    except (
        AtomicPrivateFileError,
        CommandError,
        ComparisonError,
        ContractError,
        JudgeError,
        LiveScenarioProbeError,
        LoaderError,
        ReportError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:500],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

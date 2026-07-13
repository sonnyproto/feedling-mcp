#!/usr/bin/env python3
"""Launch exactly six isolated top-level Codex qualification processes.

The launcher is intentionally deterministic and not intelligent.  Intelligence
lives inside each selected headless Codex profile.  The launcher owns process
count, concurrency, environment isolation, structured-output validation,
canonical aggregation inputs, and the trusted orchestration receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

try:
    from qa.codex_output_schema import validate_authoring_schema
    from qa.orchestration_contract import PROFILE_AGENT_TYPES
    from qa.verify_codex_orchestration import (
        MAX_CONFIGURED_CONCURRENCY,
        RECEIPT_SCHEMA_VERSION,
        OrchestrationError,
        canonical_json_sha256,
        file_sha256,
        load_private_json,
        open_owned_regular,
        owned_directory,
        parse_exec_events,
        verify,
        write_receipt,
    )
    from qa.write_codex_config import worker_permission_profile
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from codex_output_schema import validate_authoring_schema
    from orchestration_contract import PROFILE_AGENT_TYPES
    from verify_codex_orchestration import (
        MAX_CONFIGURED_CONCURRENCY,
        RECEIPT_SCHEMA_VERSION,
        OrchestrationError,
        canonical_json_sha256,
        file_sha256,
        load_private_json,
        open_owned_regular,
        owned_directory,
        parse_exec_events,
        verify,
        write_receipt,
    )
    from write_codex_config import worker_permission_profile


PINNED_CODEX_VERSION = "codex-cli 0.144.3"
LOCKED_BASE_URL = "https://test-api.feedling.app"
LOCKED_RUNTIME = "db_action_v2"
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_SCHEMA_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_AMBIENT_READ_ROOTS = tuple(
    dict.fromkeys(Path(value).resolve() for value in ("/tmp", "/var/tmp", "/dev/shm"))
)
_EXPECTED_OUTPUT_FILES = frozenset(
    ("events.jsonl", "result.json", "schema.json", "stderr.log")
)
_PROFILE_PROMPT = """\
You are one independent intelligent qualification agent in the Feedling API-key
runtime-v2 P0 suite. Read $QA_SOURCE_ROOT/qa/SOP.md,
$QA_SOURCE_ROOT/qa/coverage-lock.json, and
$QA_SOURCE_ROOT/qa/scenarios/api-key-journey.md before acting. QA_PRIVATE_MANIFEST is an
owner-only one-row manifest for exactly your assigned profile. Test only that
profile against QA_FEEDLING_BASE_URL and execute all locked scenarios in order.
Drive the live user journey, inspect correlated traces and latency stages,
adapt the next probe when evidence is ambiguous, and make semantic judgments
for chat, reasoning disclosure, memory, persona import, identity, and cleanup.
Never seek provider/admin credentials, the full provisioning manifest, another
profile manifest, public artifacts, raw output from another process, or nested
agents. Always attempt cleanup. Return exactly one profileResult JSON object
matching the supplied output schema; include only sanitized structured evidence.
"""


class WorkerLaunchError(RuntimeError):
    """Sanitized fixed failure from the deterministic process boundary."""


@dataclass(frozen=True)
class WorkerSpec:
    profile_id: str
    agent_type: str
    command: tuple[str, ...]
    environment: Mapping[str, str]
    work: Path
    output_dir: Path
    schema_path: Path
    result_path: Path
    events_path: Path
    stderr_path: Path
    prompt: str


@dataclass(frozen=True)
class WorkerAttempt:
    spec: WorkerSpec
    exit_code: int
    started_at: str
    stopped_at: str
    invocation_failed: bool


ProcessRunner = Callable[[WorkerSpec, int], int]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _private_file(path: Path, label: str, *, max_bytes: int) -> None:
    with open_owned_regular(path, label, max_bytes=max_bytes):
        pass


def _source_file(path: Path, label: str, *, max_bytes: int) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise WorkerLaunchError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerLaunchError(f"{label} is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size > max_bytes
    ):
        raise WorkerLaunchError(f"{label} is unsafe")
    return resolved


def _source_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise WorkerLaunchError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerLaunchError(f"{label} is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise WorkerLaunchError(f"{label} is unsafe")
    return resolved


def _trusted_executable(path: Path) -> Path:
    if not path.is_absolute():
        raise WorkerLaunchError("Codex executable must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise WorkerLaunchError("Codex executable is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise WorkerLaunchError("Codex executable is unsafe")
    return resolved


def verify_codex_version(codex_bin: Path) -> None:
    executable = _trusted_executable(codex_bin)
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError):
        raise WorkerLaunchError("unable to verify Codex version") from None
    if result.returncode != 0 or result.stdout.strip() != PINNED_CODEX_VERSION:
        raise WorkerLaunchError("Codex version does not match the qualification pin")


def _create_private_file(path: Path, content: bytes = b"") -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise WorkerLaunchError("unable to create private worker evidence") from None


def _copy_private_file(source: Path, destination: Path, *, max_bytes: int) -> None:
    try:
        with open_owned_regular(
            source, "validated worker result", max_bytes=max_bytes
        ) as handle:
            content = handle.read()
    except OrchestrationError as exc:
        raise WorkerLaunchError(str(exc)) from None
    _create_private_file(destination, content)


def _load_authoring_schema(path: Path) -> dict[str, Any]:
    resolved = _source_file(path, "Codex authoring schema", max_bytes=_MAX_SCHEMA_BYTES)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise WorkerLaunchError("Codex authoring schema is invalid") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("$defs"), dict):
        raise WorkerLaunchError("Codex authoring schema is invalid")
    if not isinstance(payload["$defs"].get("profileResult"), dict):
        raise WorkerLaunchError("Codex authoring schema is missing profileResult")
    return payload


def _referenced_definitions(
    root: Mapping[str, Any], definitions: Mapping[str, Any]
) -> set[str]:
    found: set[str] = set()
    pending: list[Any] = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.removeprefix("#/$defs/")
                if name not in definitions:
                    raise WorkerLaunchError(
                        "profile schema contains an unresolved reference"
                    )
                if name not in found:
                    found.add(name)
                    pending.append(definitions[name])
            pending.extend(value for key, value in node.items() if key != "$ref")
        elif isinstance(node, list):
            pending.extend(node)
    return found


def build_profile_schema(
    authoring: Mapping[str, Any], profile_id: str
) -> dict[str, Any]:
    definitions = authoring.get("$defs")
    if not isinstance(definitions, dict):
        raise WorkerLaunchError("Codex authoring schema is invalid")
    root = deepcopy(definitions.get("profileResult"))
    try:
        root["properties"]["profile_id"] = {
            "type": "string",
            "enum": [profile_id],
        }
    except (KeyError, TypeError):
        raise WorkerLaunchError("profileResult schema is invalid") from None
    names = _referenced_definitions(root, definitions)
    root["$defs"] = {name: deepcopy(definitions[name]) for name in sorted(names)}
    errors = validate_authoring_schema(root)
    if errors:
        raise WorkerLaunchError(
            "derived profile schema is not strict-output compatible"
        )
    try:
        Draft202012Validator.check_schema(root)
    except Exception:
        raise WorkerLaunchError("derived profile schema is invalid") from None
    return root


def _manifest_profile(path: Path, expected_profile: str) -> None:
    try:
        payload = load_private_json(
            path, "isolated profile manifest", max_bytes=_MAX_MANIFEST_BYTES
        )
    except OrchestrationError as exc:
        raise WorkerLaunchError(str(exc)) from None
    profiles = payload.get("profiles")
    if (
        payload.get("schema_version") != 1
        or not isinstance(profiles, list)
        or len(profiles) != 1
        or not isinstance(profiles[0], dict)
        or profiles[0].get("profile_id") != expected_profile
    ):
        raise WorkerLaunchError("isolated profile manifest matrix is invalid")


def _validate_config_profiles(codex_home: Path) -> None:
    main = codex_home / "config.toml"
    try:
        _private_file(main, "base Codex config", max_bytes=_MAX_SCHEMA_BYTES)
        base = tomllib.loads(main.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, OrchestrationError):
        raise WorkerLaunchError("base Codex config is invalid") from None
    features = base.get("features")
    if (
        "agents" in base
        or not isinstance(features, dict)
        or features.get("multi_agent") is not False
        or features.get("hooks") is not False
    ):
        raise WorkerLaunchError("base Codex config enables unsafe orchestration")
    permissions = base.get("permissions")
    if not isinstance(permissions, dict):
        raise WorkerLaunchError("base Codex permissions are missing")
    for profile_id, agent_type in PROFILE_AGENT_TYPES:
        profile_path = codex_home / f"{agent_type}.config.toml"
        try:
            _private_file(
                profile_path, "Codex worker profile", max_bytes=_MAX_SCHEMA_BYTES
            )
            profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, OrchestrationError):
            raise WorkerLaunchError("Codex worker profile is invalid") from None
        expected_permission = worker_permission_profile(profile_id)
        if (
            profile.get("default_permissions") != expected_permission
            or expected_permission not in permissions
            or "agents" in profile
            or "permissions" in profile
        ):
            raise WorkerLaunchError("Codex worker profile binding is invalid")


def _worker_environment(
    *,
    codex_home: Path,
    source_root: Path,
    artifact_root: Path,
    manifest: Path,
    profile_id: str,
    agent_type: str,
    home: Path,
    temporary: Path,
    work: Path,
    run_id: str,
    expected_sha: str,
    base_url: str,
) -> dict[str, str]:
    # This allowlist is constructed from scratch. Provider/admin secrets in the
    # launcher's parent environment are deliberately not inherited.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "NO_COLOR": "1",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "CODEX_HOME": str(codex_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(source_root),
        "QA_SOURCE_ROOT": str(source_root),
        "QA_ARTIFACT_DIR": str(artifact_root),
        "QA_RUN_ID": run_id,
        "QA_PRIVATE_MANIFEST": str(manifest),
        "QA_PROFILE_ID": profile_id,
        "QA_AGENT_TYPE": agent_type,
        "QA_WORK_ROOT": str(work),
        "QA_EXPECTED_DEPLOYMENT_SHA": expected_sha,
        "QA_EXPECTED_RUNTIME": LOCKED_RUNTIME,
        "QA_FEEDLING_BASE_URL": base_url,
    }


def _run_process(spec: WorkerSpec, timeout_seconds: int) -> int:
    try:
        with spec.events_path.open(
            "wb", buffering=0
        ) as stdout_handle, spec.stderr_path.open("wb", buffering=0) as stderr_handle:
            process = subprocess.Popen(
                list(spec.command),
                cwd=spec.work,
                env=dict(spec.environment),
                stdin=subprocess.PIPE,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
            try:
                process.communicate(spec.prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:  # pragma: no cover - qualification runners are Linux.
                    process.kill()
                process.communicate()
                return 124
            return int(process.returncode)
    except OSError:
        return 126


def _prepare_specs(
    *,
    codex_bin: Path,
    codex_home: Path,
    source_root: Path,
    artifact_root: Path,
    profile_manifest_dir: Path,
    worker_root: Path,
    worker_output_root: Path,
    authoring_schema: Mapping[str, Any],
    run_id: str,
    base_url: str,
    expected_sha: str,
) -> list[WorkerSpec]:
    specs: list[WorkerSpec] = []
    for profile_id, agent_type in PROFILE_AGENT_TYPES:
        manifest = profile_manifest_dir / f"{profile_id}.json"
        _manifest_profile(manifest, profile_id)
        agent_root = owned_directory(
            worker_root / agent_type, f"{profile_id} worker root"
        )
        home = owned_directory(agent_root / "home", f"{profile_id} home", empty=True)
        temporary = owned_directory(
            agent_root / "tmp", f"{profile_id} temp", empty=True
        )
        work = owned_directory(agent_root / "work", f"{profile_id} work", empty=True)
        output_dir = worker_output_root / profile_id
        try:
            output_dir.mkdir(mode=0o700)
            output_dir.chmod(0o700)
        except OSError:
            raise WorkerLaunchError("unable to create private worker output") from None
        schema_path = output_dir / "schema.json"
        result_path = output_dir / "result.json"
        events_path = output_dir / "events.jsonl"
        stderr_path = output_dir / "stderr.log"
        profile_schema = build_profile_schema(authoring_schema, profile_id)
        _create_private_file(
            schema_path,
            (
                json.dumps(profile_schema, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8"),
        )
        for path in (result_path, events_path, stderr_path):
            _create_private_file(path)
        command = (
            str(codex_bin),
            "exec",
            "-p",
            agent_type,
            "-c",
            f'default_permissions="{worker_permission_profile(profile_id)}"',
            "--ignore-rules",
            "--strict-config",
            "--enable",
            "network_proxy",
            "--skip-git-repo-check",
            "--cd",
            str(work),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "--color",
            "never",
            "--json",
            "-",
        )
        environment = _worker_environment(
            codex_home=codex_home,
            source_root=source_root,
            artifact_root=artifact_root,
            manifest=manifest,
            profile_id=profile_id,
            agent_type=agent_type,
            home=home,
            temporary=temporary,
            work=work,
            run_id=run_id,
            expected_sha=expected_sha,
            base_url=base_url,
        )
        specs.append(
            WorkerSpec(
                profile_id=profile_id,
                agent_type=agent_type,
                command=command,
                environment=environment,
                work=work,
                output_dir=output_dir,
                schema_path=schema_path,
                result_path=result_path,
                events_path=events_path,
                stderr_path=stderr_path,
                prompt=_PROFILE_PROMPT
                + f"\nLocked assignment: {profile_id} ({agent_type}).\n",
            )
        )
    return specs


def _peak_from_attempts(attempts: Sequence[WorkerAttempt]) -> int:
    points: list[tuple[datetime, int]] = []
    for attempt in attempts:
        start = datetime.fromisoformat(attempt.started_at.replace("Z", "+00:00"))
        stop = datetime.fromisoformat(attempt.stopped_at.replace("Z", "+00:00"))
        points.extend(((start, 1), (stop, -1)))
    points.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    for _, delta in points:
        active += delta
        peak = max(peak, active)
    return peak


def _validate_result(
    spec: WorkerSpec,
) -> tuple[str, str | None, dict[str, Any]]:
    try:
        schema = load_private_json(
            spec.schema_path, "Codex worker schema", max_bytes=_MAX_SCHEMA_BYTES
        )
        result = load_private_json(
            spec.result_path, "Codex worker result", max_bytes=_MAX_RESULT_BYTES
        )
        errors = list(Draft202012Validator(schema).iter_errors(result))
        if errors or result.get("profile_id") != spec.profile_id:
            raise WorkerLaunchError("Codex worker structured result is invalid")
        thread_id, session_id = parse_exec_events(spec.events_path)
        return thread_id, session_id, result
    except OrchestrationError as exc:
        raise WorkerLaunchError(str(exc)) from None


def launch(
    *,
    codex_bin: Path,
    codex_home: Path,
    source_root: Path,
    artifact_root: Path,
    profile_manifest_dir: Path,
    worker_root: Path,
    worker_output_root: Path,
    aggregation_input_root: Path,
    authoring_schema_path: Path,
    receipt_path: Path,
    run_id: str,
    base_url: str,
    expected_sha: str,
    timeout_seconds: int,
    process_runner: ProcessRunner = _run_process,
) -> dict[str, Any]:
    executable = _trusted_executable(codex_bin)
    codex_home = owned_directory(codex_home, "run-scoped CODEX_HOME")
    source_root = _source_directory(source_root, "source checkout")
    artifact_root = _source_directory(artifact_root, "public artifact root")
    manifests = owned_directory(profile_manifest_dir, "profile manifest directory")
    worker_root = owned_directory(worker_root, "worker root")
    outputs = owned_directory(worker_output_root, "worker output root", empty=True)
    aggregation = owned_directory(
        aggregation_input_root, "aggregation input root", empty=True
    )
    if (
        not _SAFE_TOKEN_RE.fullmatch(run_id)
        or base_url != LOCKED_BASE_URL
        or not _SHA_RE.fullmatch(expected_sha)
        or not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 60 <= timeout_seconds <= 14_400
    ):
        raise WorkerLaunchError("worker launch contract is invalid")
    if (
        not receipt_path.is_absolute()
        or receipt_path.is_symlink()
        or receipt_path.exists()
    ):
        raise WorkerLaunchError("orchestration receipt path is unsafe")
    receipt_parent = owned_directory(
        receipt_path.parent, "orchestration receipt parent"
    )
    if any(
        left == right or left in right.parents or right in left.parents
        for index, left in enumerate(
            (codex_home, source_root, manifests, worker_root, outputs, aggregation)
        )
        for right in (
            codex_home,
            source_root,
            manifests,
            worker_root,
            outputs,
            aggregation,
        )[index + 1 :]
        if left != source_root and right != source_root
    ):
        raise WorkerLaunchError("private worker roots are not isolated")
    if any(
        source_root == private
        or source_root in private.parents
        or private in source_root.parents
        for private in (
            codex_home,
            manifests,
            worker_root,
            outputs,
            aggregation,
            receipt_path,
        )
    ):
        raise WorkerLaunchError("private worker roots overlap the source checkout")
    if artifact_root != source_root and source_root not in artifact_root.parents:
        raise WorkerLaunchError("public artifact root is outside the source checkout")
    if any(
        private == ambient or ambient in private.parents
        for private in (
            codex_home,
            manifests,
            worker_root,
            outputs,
            aggregation,
            receipt_path,
            artifact_root,
        )
        for ambient in _AMBIENT_READ_ROOTS
    ):
        raise WorkerLaunchError("private worker roots are ambient-readable")
    if any(
        root in receipt_path.parents
        for root in (manifests, worker_root, outputs, aggregation)
    ):
        raise WorkerLaunchError("orchestration receipt path is not isolated")
    if receipt_path.parent.resolve() != receipt_parent:
        raise WorkerLaunchError("orchestration receipt parent is unsafe")
    _validate_config_profiles(codex_home)
    authoring = _load_authoring_schema(authoring_schema_path)
    specs = _prepare_specs(
        codex_bin=executable,
        codex_home=codex_home,
        source_root=source_root,
        artifact_root=artifact_root,
        profile_manifest_dir=manifests,
        worker_root=worker_root,
        worker_output_root=outputs,
        authoring_schema=authoring,
        run_id=run_id,
        base_url=base_url,
        expected_sha=expected_sha,
    )

    attempts: list[WorkerAttempt] = []
    active = 0
    observed_peak = 0
    lock = threading.Lock()

    def invoke(spec: WorkerSpec) -> WorkerAttempt:
        nonlocal active, observed_peak
        with lock:
            active += 1
            observed_peak = max(observed_peak, active)
            if active > MAX_CONFIGURED_CONCURRENCY:
                raise WorkerLaunchError("worker concurrency exceeded the fixed cap")
            started_at = _utc_now()
        failed = False
        try:
            exit_code = process_runner(spec, timeout_seconds)
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                failed = True
                exit_code = 125
        except Exception:
            failed = True
            exit_code = 125
        finally:
            with lock:
                stopped_at = _utc_now()
                active -= 1
        return WorkerAttempt(
            spec=spec,
            exit_code=exit_code,
            started_at=started_at,
            stopped_at=stopped_at,
            invocation_failed=failed,
        )

    # Two fixed batches guarantee at most three simultaneous processes.  Every
    # locked profile is attempted exactly once even when an earlier worker fails.
    with ThreadPoolExecutor(max_workers=MAX_CONFIGURED_CONCURRENCY) as executor:
        for offset in range(0, len(specs), MAX_CONFIGURED_CONCURRENCY):
            batch = specs[offset : offset + MAX_CONFIGURED_CONCURRENCY]
            futures = [executor.submit(invoke, spec) for spec in batch]
            attempts.extend(future.result() for future in futures)

    if (
        len(attempts) != len(PROFILE_AGENT_TYPES)
        or any(
            attempt.invocation_failed or attempt.exit_code != 0 for attempt in attempts
        )
        or not 1 <= observed_peak <= MAX_CONFIGURED_CONCURRENCY
    ):
        raise WorkerLaunchError("one or more independent Codex workers failed")

    identities: set[str] = set()
    workers: list[dict[str, Any]] = []
    for (expected_profile, expected_agent), attempt in zip(
        PROFILE_AGENT_TYPES, attempts, strict=True
    ):
        spec = attempt.spec
        if (spec.profile_id, spec.agent_type) != (expected_profile, expected_agent):
            raise WorkerLaunchError(
                "worker launch order differs from the locked matrix"
            )
        try:
            names = {entry.name for entry in spec.output_dir.iterdir()}
        except OSError:
            raise WorkerLaunchError("worker output is unreadable") from None
        if names != _EXPECTED_OUTPUT_FILES:
            raise WorkerLaunchError("worker output contains missing or extra files")
        thread_id, session_id, profile_result = _validate_result(spec)
        if thread_id in identities:
            raise WorkerLaunchError("independent Codex worker identity is duplicated")
        identities.add(thread_id)
        canonical = aggregation / f"{spec.profile_id}.json"
        _copy_private_file(spec.result_path, canonical, max_bytes=_MAX_RESULT_BYTES)
        workers.append(
            {
                "profile_id": spec.profile_id,
                "agent_type": spec.agent_type,
                "attempt": 1,
                "process_exit_code": attempt.exit_code,
                "worker_id": thread_id,
                "thread_id": thread_id,
                "session_id": session_id,
                "permission_profile": worker_permission_profile(spec.profile_id),
                "started_at": attempt.started_at,
                "stopped_at": attempt.stopped_at,
                "profile_result_sha256": canonical_json_sha256(profile_result),
                "exec_events_sha256": file_sha256(
                    spec.events_path,
                    "Codex worker event stream",
                    max_bytes=_MAX_EVENTS_BYTES,
                ),
            }
        )
    if {entry.name for entry in aggregation.iterdir()} != {
        f"{profile_id}.json" for profile_id, _ in PROFILE_AGENT_TYPES
    }:
        raise WorkerLaunchError("canonical aggregation input matrix is incomplete")

    peak = _peak_from_attempts(attempts)
    if peak != observed_peak or not 1 <= peak <= MAX_CONFIGURED_CONCURRENCY:
        raise WorkerLaunchError("worker concurrency evidence is inconsistent")
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "launcher_id": run_id,
        "max_configured_profile_concurrency": MAX_CONFIGURED_CONCURRENCY,
        "max_observed_profile_concurrency": peak,
        "launch_attempts": len(attempts),
        "workers": workers,
    }
    try:
        write_receipt(receipt_path, receipt)
        verify(receipt_path, outputs, aggregation)
    except (OrchestrationError, OSError) as exc:
        try:
            receipt_path.unlink()
        except OSError:
            pass
        raise WorkerLaunchError(str(exc)) from None
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run six isolated headless Codex API-key qualification workers"
    )
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--profile-manifest-dir", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, required=True)
    parser.add_argument("--worker-output-root", type=Path, required=True)
    parser.add_argument("--aggregation-input-root", type=Path, required=True)
    parser.add_argument("--authoring-schema", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        verify_codex_version(args.codex_bin)
        launch(
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            source_root=args.source_root,
            artifact_root=args.artifact_root,
            profile_manifest_dir=args.profile_manifest_dir,
            worker_root=args.worker_root,
            worker_output_root=args.worker_output_root,
            aggregation_input_root=args.aggregation_input_root,
            authoring_schema_path=args.authoring_schema,
            receipt_path=args.receipt,
            run_id=args.run_id,
            base_url=args.base_url,
            expected_sha=args.expected_sha,
            timeout_seconds=args.timeout_seconds,
        )
    except WorkerLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: independent Codex worker launcher encountered an internal error",
            file=sys.stderr,
        )
        return 1
    finally:
        os.umask(previous_umask)
    print("six independent Codex qualification workers completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

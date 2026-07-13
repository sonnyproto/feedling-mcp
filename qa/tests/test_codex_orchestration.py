from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from qa import run_codex_profile_workers as launcher
from qa import verify_codex_orchestration as verifier
from qa import write_codex_config as writer
from qa.orchestration_contract import PROFILE_AGENT_TYPES


def _private(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _setup(tmp_path: Path) -> dict[str, Any]:
    source = tmp_path / "checkout"
    artifacts = source / "artifacts" / "run"
    artifacts.mkdir(parents=True)
    private = _private(tmp_path / "private")
    paths = {
        "source": source,
        "artifacts": artifacts,
        "private": private,
        "codex_home": _private(private / "codex-home"),
        "manifests": _private(private / "manifests"),
        "worker_root": _private(private / "workers"),
        "raw": _private(private / "raw"),
        "aggregation": _private(private / "aggregation"),
        "supervisor_home": _private(private / "supervisor-home"),
        "supervisor_tmp": _private(private / "supervisor-tmp"),
        "supervisor_work": _private(private / "supervisor-work"),
        "receipt": private / "receipt.json",
        "full_manifest": private / "full-manifest.json",
        "schema": Path(__file__).resolve().parents[1]
        / "schemas"
        / "codex-run-result.schema.json",
        "codex_bin": Path("/usr/bin/true"),
    }
    for _, agent_type in PROFILE_AGENT_TYPES:
        agent_root = _private(paths["worker_root"] / agent_type)
        for leaf in ("home", "tmp", "work"):
            _private(agent_root / leaf)
    config_values = {
        "output": paths["codex_home"] / "config.toml",
        "source_root": source,
        "artifact_root": artifacts,
        "full_manifest": paths["full_manifest"],
        "profile_manifest_dir": paths["manifests"],
        "supervisor_home": paths["supervisor_home"],
        "supervisor_tmp": paths["supervisor_tmp"],
        "supervisor_work": paths["supervisor_work"],
        "worker_root": paths["worker_root"],
        "worker_output_root": paths["raw"],
        "aggregation_input_root": paths["aggregation"],
        "orchestration_receipt": paths["receipt"],
        "codex_model": "gpt-5.4",
        "allowed_host": "test-api.feedling.app",
    }
    writer.write_bundle(
        config_values["output"], writer.build_config_bundle(**config_values)
    )
    for profile_id, _ in PROFILE_AGENT_TYPES:
        manifest = paths["manifests"] / f"{profile_id}.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": [{"profile_id": profile_id}],
                }
            )
            + "\n"
        )
        manifest.chmod(0o600)
    return paths


def _instance(node: dict[str, Any], definitions: dict[str, Any]) -> Any:
    if "$ref" in node:
        return _instance(
            definitions[node["$ref"].removeprefix("#/$defs/")], definitions
        )
    if "enum" in node:
        return node["enum"][0]
    if "anyOf" in node:
        return _instance(node["anyOf"][0], definitions)
    node_type = node.get("type")
    if isinstance(node_type, list):
        node_type = next(value for value in node_type if value != "null")
    if node_type == "object":
        return {
            name: _instance(child, definitions)
            for name, child in node.get("properties", {}).items()
        }
    if node_type == "array":
        return []
    if node_type == "string":
        return "safe"
    if node_type in ("integer", "number"):
        return 0
    if node_type == "boolean":
        return True
    if node_type == "null":
        return None
    raise AssertionError(node)


def _successful_runner(
    captured: list[launcher.WorkerSpec],
    *,
    duplicate_thread: bool = False,
    invalid_result: bool = False,
    extra_file: bool = False,
) -> launcher.ProcessRunner:
    lock = threading.Lock()
    cap = verifier.MAX_CONFIGURED_CONCURRENCY
    barriers = {
        offset // cap: threading.Barrier(min(cap, len(PROFILE_AGENT_TYPES) - offset))
        for offset in range(0, len(PROFILE_AGENT_TYPES), cap)
    }

    def run(spec: launcher.WorkerSpec, timeout: int) -> int:
        assert timeout == 600
        with lock:
            captured.append(spec)
            index = PROFILE_AGENT_TYPES.index((spec.profile_id, spec.agent_type))
        barrier = barriers[index // cap]
        barrier.wait(timeout=5)
        schema = json.loads(spec.schema_path.read_text())
        result = _instance(schema, schema["$defs"])
        if invalid_result and index == 0:
            result.pop("profile_id")
        spec.result_path.write_text(json.dumps(result) + "\n")
        thread_index = 0 if duplicate_thread else index
        thread_id = f"30000000-0000-4000-8000-{thread_index:012d}"
        rows = [
            {
                "type": "thread.started",
                "thread_id": thread_id,
                "session_id": f"40000000-0000-4000-8000-{thread_index:012d}",
            },
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {}},
        ]
        spec.events_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        if extra_file and index == 0:
            extra = spec.output_dir / "extra.txt"
            extra.write_text("unexpected\n")
            extra.chmod(0o600)
        barrier.wait(timeout=5)
        return 0

    return run


def _launch(paths: dict[str, Any], runner: launcher.ProcessRunner) -> dict[str, Any]:
    return launcher.launch(
        codex_bin=paths["codex_bin"],
        codex_home=paths["codex_home"],
        source_root=paths["source"],
        artifact_root=paths["artifacts"],
        profile_manifest_dir=paths["manifests"],
        worker_root=paths["worker_root"],
        worker_output_root=paths["raw"],
        aggregation_input_root=paths["aggregation"],
        authoring_schema_path=paths["schema"],
        receipt_path=paths["receipt"],
        run_id="run-123",
        base_url="https://test-api.feedling.app",
        expected_sha="a" * 40,
        timeout_seconds=600,
        process_runner=runner,
    )


def test_launcher_runs_exact_matrix_at_peak_three_without_secrets(
    tmp_path, monkeypatch
):
    assert PROFILE_AGENT_TYPES == (
        ("official-deepseek", "profile_official_deepseek"),
        ("official-anthropic", "profile_official_anthropic"),
        ("official-openai", "profile_official_openai"),
        ("official-gemini", "profile_official_gemini"),
        ("openrouter-claude", "profile_openrouter_claude"),
        ("openrouter-openai", "profile_openrouter_openai"),
        ("openrouter-glm", "profile_openrouter_glm"),
        ("relay-kongbeiqie", "profile_relay_kongbeiqie"),
    )
    paths = _setup(tmp_path)
    for name in (
        "QA_TEST_ADMIN_TOKEN",
        "FEEDLING_ADMIN_TOKEN",
        "QA_DEEPSEEK_API_KEY",
        "QA_ANTHROPIC_API_KEY",
        "QA_OPENAI_PROVIDER_API_KEY",
        "QA_OPENROUTER_API_KEY",
        "QA_GEMINI_API_KEY",
        "QA_KONGBEIQIE_API_KEY",
        "QA_GEMINI_MODEL",
        "QA_KONGBEIQIE_MODEL",
        "QA_KONGBEIQIE_BASE_URL",
        "QA_CODEX_AUTH_JSON_B64",
    ):
        monkeypatch.setenv(name, "must-not-cross-boundary")
    captured: list[launcher.WorkerSpec] = []
    receipt = _launch(paths, _successful_runner(captured))

    assert receipt["schema_version"] == 2
    assert receipt["launch_attempts"] == len(PROFILE_AGENT_TYPES)
    assert receipt["max_configured_profile_concurrency"] == 3
    assert receipt["max_observed_profile_concurrency"] == 3
    assert [
        (row["profile_id"], row["agent_type"]) for row in receipt["workers"]
    ] == list(PROFILE_AGENT_TYPES)
    assert len({row["thread_id"] for row in receipt["workers"]}) == len(
        PROFILE_AGENT_TYPES
    )
    assert [row["permission_profile"] for row in receipt["workers"]] == [
        f"feedling-e2e-{profile_id}" for profile_id, _ in PROFILE_AGENT_TYPES
    ]
    assert (
        verifier.verify(paths["receipt"], paths["raw"], paths["aggregation"]) == receipt
    )
    assert {path.name for path in paths["aggregation"].iterdir()} == {
        f"{profile_id}.json" for profile_id, _ in PROFILE_AGENT_TYPES
    }
    assert len(captured) == len(PROFILE_AGENT_TYPES)
    for spec in captured:
        assert spec.command[1:6] == (
            "exec",
            "-p",
            spec.agent_type,
            "-c",
            f'default_permissions="feedling-e2e-{spec.profile_id}"',
        )
        assert "--ephemeral" not in spec.command
        assert "spawn_agent" not in spec.prompt
        assert spec.environment["QA_PRIVATE_MANIFEST"].endswith(
            f"/{spec.profile_id}.json"
        )
        assert spec.environment["HOME"].endswith(f"/{spec.agent_type}/home")
        assert spec.environment["TMPDIR"].endswith(f"/{spec.agent_type}/tmp")
        assert spec.environment["QA_WORK_ROOT"].endswith(f"/{spec.agent_type}/work")
        assert spec.environment["QA_ARTIFACT_DIR"] == str(paths["artifacts"].resolve())
        assert not any(
            name in spec.environment
            for name in (
                "QA_TEST_ADMIN_TOKEN",
                "FEEDLING_ADMIN_TOKEN",
                "QA_DEEPSEEK_API_KEY",
                "QA_ANTHROPIC_API_KEY",
                "QA_OPENAI_PROVIDER_API_KEY",
                "QA_OPENROUTER_API_KEY",
                "QA_GEMINI_API_KEY",
                "QA_KONGBEIQIE_API_KEY",
                "QA_GEMINI_MODEL",
                "QA_KONGBEIQIE_MODEL",
                "QA_KONGBEIQIE_BASE_URL",
                "QA_CODEX_AUTH_JSON_B64",
            )
        )


def test_nonzero_exit_attempts_all_eight_once_and_writes_no_receipt(tmp_path):
    paths = _setup(tmp_path)
    captured: list[launcher.WorkerSpec] = []
    successful = _successful_runner(captured)

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        code = successful(spec, timeout)
        return 1 if spec.profile_id == PROFILE_AGENT_TYPES[0][0] else code

    with pytest.raises(launcher.WorkerLaunchError, match="workers failed"):
        _launch(paths, runner)
    assert len(captured) == len(PROFILE_AGENT_TYPES)
    assert len({spec.profile_id for spec in captured}) == len(PROFILE_AGENT_TYPES)
    assert not paths["receipt"].exists()
    assert list(paths["aggregation"].iterdir()) == []


def test_launcher_rejects_ambient_readable_private_roots(tmp_path, monkeypatch):
    paths = _setup(tmp_path)
    monkeypatch.setattr(launcher, "_AMBIENT_READ_ROOTS", (tmp_path.resolve(),))
    invoked = False

    def runner(spec: launcher.WorkerSpec, timeout: int) -> int:
        nonlocal invoked
        invoked = True
        return 0

    with pytest.raises(launcher.WorkerLaunchError, match="ambient-readable"):
        _launch(paths, runner)
    assert invoked is False
    assert not paths["receipt"].exists()


@pytest.mark.parametrize(
    "runner_kwargs",
    (
        {"duplicate_thread": True},
        {"invalid_result": True},
        {"extra_file": True},
    ),
)
def test_launcher_fails_closed_on_invalid_worker_evidence(tmp_path, runner_kwargs):
    paths = _setup(tmp_path)
    captured: list[launcher.WorkerSpec] = []
    with pytest.raises(launcher.WorkerLaunchError):
        _launch(paths, _successful_runner(captured, **runner_kwargs))
    assert len(captured) == len(PROFILE_AGENT_TYPES)
    assert not paths["receipt"].exists()


def test_verifier_rejects_tampered_canonical_input(tmp_path):
    paths = _setup(tmp_path)
    _launch(paths, _successful_runner([]))
    canonical = paths["aggregation"] / f"{PROFILE_AGENT_TYPES[0][0]}.json"
    canonical.write_text("{}\n")
    with pytest.raises(verifier.OrchestrationError, match="canonical aggregation"):
        verifier.verify(paths["receipt"], paths["raw"], paths["aggregation"])


def test_parse_events_rejects_nested_agents(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {
                    "type": "item.started",
                    "item": {"type": "collab_tool_call", "tool": "spawn_agent"},
                },
                {"type": "turn.completed"},
            )
        )
    )
    path.chmod(0o600)
    with pytest.raises(verifier.OrchestrationError, match="nested orchestration"):
        verifier.parse_exec_events(path.resolve())


@pytest.mark.skipif(os.name != "posix", reason="qualification runner is POSIX")
def test_timeout_kills_the_entire_codex_process_group(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    stderr = tmp_path / "stderr.log"
    events.write_bytes(b"")
    stderr.write_bytes(b"")
    spec = launcher.WorkerSpec(
        profile_id="official-deepseek",
        agent_type="profile_official_deepseek",
        command=("/trusted/codex", "exec"),
        environment={},
        work=tmp_path,
        output_dir=tmp_path,
        schema_path=tmp_path / "schema.json",
        result_path=tmp_path / "result.json",
        events_path=events,
        stderr_path=stderr,
        prompt="test",
    )

    class TimedOutProcess:
        pid = 4321
        returncode = None

        def __init__(self):
            self.communications = 0
            self.parent_killed = False

        def communicate(self, _input=None, timeout=None):
            self.communications += 1
            if self.communications == 1:
                raise subprocess.TimeoutExpired("codex", timeout)
            self.returncode = -signal.SIGKILL
            return (None, None)

        def kill(self):
            self.parent_killed = True

    process = TimedOutProcess()
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: process)
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        launcher.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    assert launcher._run_process(spec, 60) == 124
    assert killed == [(process.pid, signal.SIGKILL)]
    assert process.parent_killed is False
    assert process.communications == 2

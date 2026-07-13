#!/usr/bin/env python3
"""Verify trusted receipts from eight independent top-level Codex processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Sequence

try:
    from qa.orchestration_contract import PROFILE_AGENT_TYPES
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from orchestration_contract import PROFILE_AGENT_TYPES


RECEIPT_SCHEMA_VERSION = 2
MAX_CONFIGURED_CONCURRENCY = 3
WORKER_FILES = frozenset(("events.jsonl", "result.json", "schema.json", "stderr.log"))
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_RESULT_BYTES = 32 * 1024 * 1024
_MAX_SCHEMA_BYTES = 8 * 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024 * 1024
_MAX_JSON_LINE_BYTES = 16 * 1024 * 1024


class OrchestrationError(RuntimeError):
    """Sanitized deterministic-verifier failure."""


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def owned_directory(path: Path, label: str, *, empty: bool = False) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise OrchestrationError(f"{label} is unsafe")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        entries = list(path.iterdir()) if empty else None
    except (OSError, RuntimeError):
        raise OrchestrationError(f"{label} is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (empty and entries)
    ):
        raise OrchestrationError(f"{label} is unsafe")
    return resolved


def open_owned_regular(
    path: Path, label: str, *, max_bytes: int, require_mode: int = 0o600
) -> BinaryIO:
    if not path.is_absolute() or path.is_symlink():
        raise OrchestrationError(f"{label} is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise OrchestrationError(f"{label} is unreadable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != require_mode
            or metadata.st_size > max_bytes
        ):
            raise OrchestrationError(f"{label} is unsafe")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def file_sha256(path: Path, label: str, *, max_bytes: int) -> str:
    digest = hashlib.sha256()
    with open_owned_regular(path, label, max_bytes=max_bytes) as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    """Hash a JSON object independently of whitespace and object key order."""

    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise OrchestrationError("canonical worker result is invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def load_private_json(path: Path, label: str, *, max_bytes: int) -> dict[str, Any]:
    try:
        with open_owned_regular(path, label, max_bytes=max_bytes) as handle:
            payload = json.load(handle)
    except OrchestrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise OrchestrationError(f"{label} is invalid") from None
    if not isinstance(payload, dict):
        raise OrchestrationError(f"{label} is invalid")
    return payload


def _json_lines(path: Path, label: str) -> Iterable[dict[str, Any]]:
    try:
        with open_owned_regular(path, label, max_bytes=_MAX_EVENTS_BYTES) as handle:
            for raw in handle:
                if len(raw) > _MAX_JSON_LINE_BYTES:
                    raise OrchestrationError(f"{label} contains an oversized row")
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError, RecursionError):
                    raise OrchestrationError(f"{label} is invalid") from None
                if not isinstance(row, dict):
                    raise OrchestrationError(f"{label} is invalid")
                yield row
    except OrchestrationError:
        raise
    except OSError:
        raise OrchestrationError(f"{label} is unreadable") from None


def parse_exec_events(path: Path) -> tuple[str, str | None]:
    """Return the single completed root thread/session identity."""

    threads: list[str] = []
    sessions: list[str] = []
    turn_started = 0
    turn_completed = 0
    failed = False
    for row in _json_lines(path, "Codex worker event stream"):
        row_type = row.get("type")
        if row_type == "thread.started":
            thread_id = row.get("thread_id")
            if not _valid_identifier(thread_id):
                raise OrchestrationError("Codex worker event identity is invalid")
            threads.append(thread_id)
            session_id = row.get("session_id")
            if session_id is not None:
                if not _valid_identifier(session_id):
                    raise OrchestrationError("Codex worker session identity is invalid")
                sessions.append(session_id)
        elif row_type == "turn.started":
            turn_started += 1
        elif row_type == "turn.completed":
            turn_completed += 1
        elif row_type in ("turn.failed", "error"):
            failed = True
        if row_type in ("item.started", "item.completed"):
            item = row.get("item")
            if isinstance(item, dict) and item.get("type") == "collab_tool_call":
                raise OrchestrationError("Codex worker attempted nested orchestration")
    if (
        failed
        or len(threads) != 1
        or turn_started != 1
        or turn_completed != 1
        or len(set(sessions)) > 1
    ):
        raise OrchestrationError("Codex worker execution is incomplete")
    return threads[0], (sessions[0] if sessions else None)


def write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise OrchestrationError("orchestration receipt path is unsafe")
    parent = owned_directory(path.parent, "orchestration receipt parent")
    if path.parent.resolve() != parent:
        raise OrchestrationError("orchestration receipt parent is unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise OrchestrationError("unable to create orchestration receipt") from None
    with open_owned_regular(
        path, "orchestration receipt", max_bytes=_MAX_RECEIPT_BYTES
    ):
        pass


def _peak_concurrency(workers: Sequence[Mapping[str, Any]]) -> int:
    points: list[tuple[datetime, int]] = []
    for worker in workers:
        start = _parse_timestamp(worker.get("started_at"))
        stop = _parse_timestamp(worker.get("stopped_at"))
        if start is None or stop is None or stop < start:
            raise OrchestrationError("Codex worker lifecycle timestamps are invalid")
        points.extend(((start, 1), (stop, -1)))
    # A stop and a start at the exact same instant are not concurrent.
    points.sort(key=lambda point: (point[0], point[1]))
    active = 0
    peak = 0
    for _, delta in points:
        active += delta
        if active < 0:
            raise OrchestrationError("Codex worker lifecycle is inconsistent")
        peak = max(peak, active)
    if active != 0:
        raise OrchestrationError("Codex worker lifecycle is incomplete")
    return peak


def _validate_receipt_shape(receipt: Any) -> list[dict[str, Any]]:
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "launcher_id",
        "max_configured_profile_concurrency",
        "max_observed_profile_concurrency",
        "launch_attempts",
        "workers",
    }:
        raise OrchestrationError("orchestration receipt shape is invalid")
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or not _valid_identifier(receipt.get("launcher_id"))
        or receipt.get("max_configured_profile_concurrency")
        != MAX_CONFIGURED_CONCURRENCY
        or receipt.get("launch_attempts") != len(PROFILE_AGENT_TYPES)
    ):
        raise OrchestrationError("orchestration receipt contract is invalid")
    workers = receipt.get("workers")
    if not isinstance(workers, list) or len(workers) != len(PROFILE_AGENT_TYPES):
        raise OrchestrationError("orchestration receipt worker count is invalid")
    expected_keys = {
        "profile_id",
        "agent_type",
        "attempt",
        "process_exit_code",
        "worker_id",
        "thread_id",
        "session_id",
        "permission_profile",
        "started_at",
        "stopped_at",
        "profile_result_sha256",
        "exec_events_sha256",
    }
    identities: list[str] = []
    for index, row in enumerate(workers):
        if not isinstance(row, dict) or set(row) != expected_keys:
            raise OrchestrationError("orchestration receipt worker shape is invalid")
        expected_profile, expected_agent = PROFILE_AGENT_TYPES[index]
        if (
            row.get("profile_id") != expected_profile
            or row.get("agent_type") != expected_agent
            or row.get("attempt") != 1
            or row.get("process_exit_code") != 0
            or row.get("permission_profile") != f"feedling-e2e-{expected_profile}"
            or not _valid_identifier(row.get("worker_id"))
            or row.get("thread_id") != row.get("worker_id")
            or (
                row.get("session_id") is not None
                and not _valid_identifier(row.get("session_id"))
            )
            or not isinstance(row.get("profile_result_sha256"), str)
            or not _SHA256_RE.fullmatch(row["profile_result_sha256"])
            or not isinstance(row.get("exec_events_sha256"), str)
            or not _SHA256_RE.fullmatch(row["exec_events_sha256"])
        ):
            raise OrchestrationError("orchestration receipt worker contract is invalid")
        # Also validates ordering and timezone-awareness.
        start = _parse_timestamp(row.get("started_at"))
        stop = _parse_timestamp(row.get("stopped_at"))
        if start is None or stop is None or stop < start:
            raise OrchestrationError("orchestration receipt timestamps are invalid")
        identities.append(row["worker_id"])
    if len(set(identities)) != len(PROFILE_AGENT_TYPES):
        raise OrchestrationError(
            "orchestration receipt worker identities are duplicated"
        )
    peak = _peak_concurrency(workers)
    if (
        not 1 <= peak <= MAX_CONFIGURED_CONCURRENCY
        or receipt.get("max_observed_profile_concurrency") != peak
    ):
        raise OrchestrationError("orchestration receipt concurrency is invalid")
    return workers


def verify(
    receipt_path: Path,
    worker_output_root: Path,
    aggregation_input_root: Path,
) -> dict[str, Any]:
    """Verify receipt identity, exact output set, lifecycle, and content hashes."""

    root = owned_directory(worker_output_root, "worker output root")
    aggregation = owned_directory(aggregation_input_root, "aggregation input root")
    receipt = load_private_json(
        receipt_path, "orchestration receipt", max_bytes=_MAX_RECEIPT_BYTES
    )
    workers = _validate_receipt_shape(receipt)
    try:
        entries = list(root.iterdir())
    except OSError:
        raise OrchestrationError("worker output root is unreadable") from None
    if {entry.name for entry in entries} != {
        profile_id for profile_id, _ in PROFILE_AGENT_TYPES
    } or any(entry.is_symlink() for entry in entries):
        raise OrchestrationError(
            "worker output matrix is incomplete or contains extras"
        )
    try:
        aggregation_entries = list(aggregation.iterdir())
    except OSError:
        raise OrchestrationError("aggregation input root is unreadable") from None
    if {entry.name for entry in aggregation_entries} != {
        f"{profile_id}.json" for profile_id, _ in PROFILE_AGENT_TYPES
    } or any(entry.is_symlink() for entry in aggregation_entries):
        raise OrchestrationError(
            "aggregation input matrix is incomplete or contains extras"
        )

    for row in workers:
        profile_id = row["profile_id"]
        directory = owned_directory(root / profile_id, f"{profile_id} output directory")
        try:
            names = {entry.name for entry in directory.iterdir()}
        except OSError:
            raise OrchestrationError("worker output directory is unreadable") from None
        if names != WORKER_FILES:
            raise OrchestrationError(
                "worker output file set is incomplete or contains extras"
            )
        events = directory / "events.jsonl"
        result = directory / "result.json"
        thread_id, session_id = parse_exec_events(events)
        if (
            thread_id != row["thread_id"]
            or session_id != row["session_id"]
            or file_sha256(
                events, "Codex worker event stream", max_bytes=_MAX_EVENTS_BYTES
            )
            != row["exec_events_sha256"]
        ):
            raise OrchestrationError("worker output does not match trusted receipt")
        canonical = aggregation / f"{profile_id}.json"
        result_payload = load_private_json(
            result, "Codex worker result", max_bytes=_MAX_RESULT_BYTES
        )
        canonical_payload = load_private_json(
            canonical, "canonical aggregation input", max_bytes=_MAX_RESULT_BYTES
        )
        if (
            canonical_json_sha256(result_payload) != row["profile_result_sha256"]
            or canonical_json_sha256(canonical_payload) != row["profile_result_sha256"]
        ):
            raise OrchestrationError(
                "canonical aggregation input does not match receipt"
            )
        if result_payload.get("profile_id") != profile_id:
            raise OrchestrationError("Codex worker result profile is invalid")
        # Validate ownership and modes even when their contents are intentionally ignored.
        with open_owned_regular(
            directory / "schema.json",
            "Codex worker schema",
            max_bytes=_MAX_SCHEMA_BYTES,
        ):
            pass
        with open_owned_regular(
            directory / "stderr.log", "Codex worker stderr", max_bytes=_MAX_STDERR_BYTES
        ):
            pass
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify independent-process Codex orchestration evidence"
    )
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--worker-output-root", type=Path, required=True)
    parser.add_argument("--aggregation-input-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify(args.receipt, args.worker_output_root, args.aggregation_input_root)
    except OrchestrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: orchestration verifier encountered an internal error",
            file=sys.stderr,
        )
        return 1
    print("trusted independent Codex orchestration verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

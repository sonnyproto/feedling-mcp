#!/usr/bin/env python3
"""Compute bounded, secret-free provenance for the qualification harness."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterator


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_FILE_COUNT = 10_000
WORKER_SOURCE_PATHS = (
    Path("backend/content_encryption.py"),
    Path("qa"),
    Path("tools/genesis_e2e.py"),
    Path("tools/provider_smoke"),
)
WORKER_SNAPSHOT_EXCLUDED_NAMES = frozenset(
    (
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "qualification-artifacts",
        "venv",
    )
)
_PATHS = (
    ".github/workflows/api-key-e2e.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/pg-deploy.yml",
    ".gitignore",
    "backend/content_encryption.py",
    "qa",
    "tests/test_genesis_distill_acceptance.py",
    "tools/genesis_e2e.py",
    "tools/provider_smoke",
)
_WORKER_PATHS = tuple(path.as_posix() for path in WORKER_SOURCE_PATHS)
_EXCLUDED_PARTS = WORKER_SNAPSHOT_EXCLUDED_NAMES
_EXCLUDED_SUFFIXES = frozenset((".pyc", ".pyo"))


class HarnessProvenanceError(RuntimeError):
    """A fixed provenance failure safe to show to an operator."""


def _excluded(relative: Path) -> bool:
    return (
        any(part in _EXCLUDED_PARTS or part.startswith(".env") for part in relative.parts)
        or relative.suffix in _EXCLUDED_SUFFIXES
    )


def _walk(source_root: Path, directory: Path) -> Iterator[tuple[Path, os.stat_result]]:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        raise HarnessProvenanceError(
            "qualification harness source is unreadable"
        ) from None
    for entry in entries:
        path = Path(entry.path)
        relative = path.relative_to(source_root)
        if _excluded(relative):
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            raise HarnessProvenanceError(
                "qualification harness source is unreadable"
            ) from None
        if entry.is_symlink():
            raise HarnessProvenanceError("qualification harness source is unsafe")
        if stat.S_ISDIR(metadata.st_mode):
            yield from _walk(source_root, path)
        elif stat.S_ISREG(metadata.st_mode):
            yield relative, metadata
        else:
            raise HarnessProvenanceError("qualification harness source is unsafe")


def _files(
    source_root: Path, paths: tuple[str, ...], *, require_all: bool
) -> Iterator[tuple[Path, os.stat_result]]:
    for raw in paths:
        candidate = source_root / raw
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if require_all:
                raise HarnessProvenanceError(
                    "qualification worker source is unavailable"
                ) from None
            continue
        except OSError:
            raise HarnessProvenanceError(
                "qualification harness source is unreadable"
            ) from None
        if candidate.is_symlink():
            raise HarnessProvenanceError("qualification harness source is unsafe")
        relative = candidate.relative_to(source_root)
        if _excluded(relative):
            continue
        if stat.S_ISDIR(metadata.st_mode):
            yield from _walk(source_root, candidate)
        elif stat.S_ISREG(metadata.st_mode):
            yield relative, metadata
        else:
            raise HarnessProvenanceError("qualification harness source is unsafe")


def _stable_file_bytes(path: Path, expected: os.stat_result) -> Iterator[bytes]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise HarnessProvenanceError(
            "qualification harness source is unreadable"
        ) from None
    try:
        before = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
        )
        if (
            identity != expected_identity
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise HarnessProvenanceError("qualification harness source changed")
        while chunk := os.read(descriptor, 1024 * 1024):
            yield chunk
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != identity
        ):
            raise HarnessProvenanceError("qualification harness source changed")
    finally:
        os.close(descriptor)


def _source_digest(
    source_root: Path, paths: tuple[str, ...], *, require_all: bool
) -> str:
    digest = hashlib.sha256()
    total = 0
    count = 0
    for relative, metadata in _files(source_root, paths, require_all=require_all):
        count += 1
        if count > _MAX_FILE_COUNT:
            raise HarnessProvenanceError(
                "qualification harness contains too many files"
            )
        path = source_root / relative
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 0
            or metadata.st_size > _MAX_FILE_BYTES
            or metadata.st_nlink != 1
        ):
            raise HarnessProvenanceError("qualification harness source is unsafe")
        total += metadata.st_size
        if total > _MAX_TOTAL_BYTES:
            raise HarnessProvenanceError("qualification harness source is too large")
        name = relative.as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(metadata.st_size.to_bytes(8, "big"))
        for chunk in _stable_file_bytes(path, metadata):
            digest.update(chunk)
    if count == 0:
        raise HarnessProvenanceError("qualification harness source is unavailable")
    return digest.hexdigest()


def _git(source_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("git", "-C", str(source_root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
            },
        )
    except (OSError, subprocess.SubprocessError):
        raise HarnessProvenanceError(
            "qualification harness Git provenance is unavailable"
        ) from None


def collect(source_root: Path) -> dict[str, Any]:
    """Return harness HEAD, dirty state, and a digest of the actual QA sources."""

    try:
        root = source_root.resolve(strict=True)
        metadata = root.stat()
    except (OSError, RuntimeError):
        raise HarnessProvenanceError(
            "qualification harness source root is unavailable"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise HarnessProvenanceError("qualification harness source root is unsafe")

    def head() -> str:
        result = _git(root, "rev-parse", "HEAD")
        value = result.stdout.strip()
        if result.returncode != 0 or not _SHA_RE.fullmatch(value):
            raise HarnessProvenanceError(
                "qualification harness Git HEAD is unavailable"
            )
        return value

    def status_output() -> str:
        result = _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *_PATHS,
        )
        if result.returncode != 0:
            raise HarnessProvenanceError(
                "qualification harness Git status is unavailable"
            )
        return result.stdout

    head_before = head()
    status_before = status_output()
    digest_before = _source_digest(root, _PATHS, require_all=False)
    worker_digest_before = _source_digest(root, _WORKER_PATHS, require_all=True)
    digest_after = _source_digest(root, _PATHS, require_all=False)
    worker_digest_after = _source_digest(root, _WORKER_PATHS, require_all=True)
    status_after = status_output()
    head_after = head()
    if (
        head_before != head_after
        or status_before != status_after
        or digest_before != digest_after
        or worker_digest_before != worker_digest_after
    ):
        raise HarnessProvenanceError(
            "qualification harness changed during provenance capture"
        )
    return {
        "git_head": head_after,
        "dirty": bool(status_after),
        "source_sha256": digest_after,
        "worker_source_sha256": worker_digest_after,
    }


def snapshot_digest(snapshot_root: Path) -> str:
    """Return a stable digest of the exact allowlisted bytes workers execute."""

    try:
        root = snapshot_root.resolve(strict=True)
        metadata = root.stat()
    except (OSError, RuntimeError):
        raise HarnessProvenanceError(
            "qualification worker snapshot is unavailable"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise HarnessProvenanceError("qualification worker snapshot is unsafe")
    before = _source_digest(root, _WORKER_PATHS, require_all=True)
    after = _source_digest(root, _WORKER_PATHS, require_all=True)
    if before != after:
        raise HarnessProvenanceError(
            "qualification worker snapshot changed during provenance capture"
        )
    return after

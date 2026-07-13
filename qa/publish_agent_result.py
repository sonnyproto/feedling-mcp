#!/usr/bin/env python3
"""Install Codex's private final message as the public canonical result safely.

The Codex parent writes ``--output-last-message`` outside every model-writable
directory.  This trusted helper then publishes that file with no symlink or
overwrite semantics.  It never prints the result body.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path


MAX_RESULT_BYTES = 8 * 1024 * 1024


class PublishError(RuntimeError):
    """A fixed, non-sensitive publication failure."""


def _read_private_regular(path: Path) -> bytes:
    if not path.is_absolute():
        raise PublishError("private agent result path must be absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise PublishError("private agent result is missing or unsafe") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > MAX_RESULT_BYTES
        ):
            raise PublishError("private agent result metadata is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise PublishError("private agent result changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise PublishError("private agent result changed while reading")
        raw = b"".join(chunks)
    finally:
        os.close(fd)

    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublishError("private agent result is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise PublishError("private agent result must be one JSON object")
    return raw


def publish(source: Path, destination: Path) -> None:
    raw = _read_private_regular(source)
    if not destination.is_absolute() or destination.name != "run-result.json":
        raise PublishError("public result destination is unsafe")
    try:
        parent = destination.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PublishError("public result directory is missing") from None
    if not parent.is_dir() or parent == Path(parent.anchor):
        raise PublishError("public result directory is unsafe")
    if source.parent.resolve() == parent:
        raise PublishError("private and public result directories must differ")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        fd = os.open(destination, flags, 0o600)
        created = True
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        metadata = destination.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise PublishError("published result metadata is unsafe")
    except PublishError:
        if created:
            destination.unlink(missing_ok=True)
        raise
    except OSError:
        if created:
            destination.unlink(missing_ok=True)
        raise PublishError("unable to publish canonical result exclusively") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        publish(args.source, args.destination)
    except PublishError as exc:
        print(f"agent result publication error: {exc}", file=sys.stderr)
        return 1
    print("canonical agent result published safely")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

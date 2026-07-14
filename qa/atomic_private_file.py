"""No-overwrite atomic publication for owner-controlled QA files."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class AtomicPrivateFileError(RuntimeError):
    """A private file could not be published safely."""


def _write_and_sync(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("private file write made no progress")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _same_inode(metadata: os.stat_result, identity: tuple[int, int]) -> bool:
    return (metadata.st_dev, metadata.st_ino) == identity


def _safe_private_inode(
    metadata: os.stat_result,
    identity: tuple[int, int],
    *,
    links: int,
    size: int,
) -> bool:
    return bool(
        _same_inode(metadata, identity)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_nlink == links
        and metadata.st_size == size
    )


def _descriptor_content_matches(descriptor: int, expected: bytes) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    observed = bytearray()
    while len(observed) <= len(expected):
        chunk = os.read(descriptor, min(1024 * 1024, len(expected) + 1 - len(observed)))
        if not chunk:
            break
        observed.extend(chunk)
    return bytes(observed) == expected


def _unlink_matching(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        if _same_inode(path.stat(follow_symlinks=False), identity):
            path.unlink()
    except OSError:
        pass


def create_private_file(path: Path, content: bytes = b"") -> None:
    """Publish complete mode-0600 content without replacing an existing path.

    The temporary inode is written and synced before the final pathname exists.
    A hard-link publication retains ``O_EXCL``-style no-overwrite semantics; the
    temporary name is then removed so readers require exactly one link.
    """

    if not isinstance(content, bytes) or not path.is_absolute():
        raise AtomicPrivateFileError("private file publication is invalid")
    try:
        parent = path.parent.resolve(strict=True)
        parent_metadata = parent.stat()
    except (OSError, RuntimeError):
        raise AtomicPrivateFileError("private file parent is unavailable") from None
    if (
        parent != path.parent
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise AtomicPrivateFileError("private file parent is unsafe")

    descriptor = -1
    temporary: Path | None = None
    published = False
    identity: tuple[int, int] | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(parent)
        )
        temporary = Path(raw_temporary)
        initial_metadata = os.fstat(descriptor)
        identity = (initial_metadata.st_dev, initial_metadata.st_ino)
        os.fchmod(descriptor, 0o600)
        _write_and_sync(descriptor, content)
        descriptor_metadata = os.fstat(descriptor)
        if (
            not _safe_private_inode(
                descriptor_metadata, identity, links=1, size=len(content)
            )
            or not _safe_private_inode(
                temporary.stat(follow_symlinks=False),
                identity,
                links=1,
                size=len(content),
            )
            or not _descriptor_content_matches(descriptor, content)
        ):
            raise OSError("private temporary file changed before publication")
        os.link(temporary, path, follow_symlinks=False)
        published = True
        if (
            not _safe_private_inode(
                os.fstat(descriptor), identity, links=2, size=len(content)
            )
            or not _safe_private_inode(
                path.stat(follow_symlinks=False),
                identity,
                links=2,
                size=len(content),
            )
            or not _descriptor_content_matches(descriptor, content)
        ):
            raise OSError("private file changed during publication")
        temporary.unlink()
        temporary = None
        if (
            not _safe_private_inode(
                os.fstat(descriptor), identity, links=1, size=len(content)
            )
            or not _safe_private_inode(
                path.stat(follow_symlinks=False),
                identity,
                links=1,
                size=len(content),
            )
            or not _descriptor_content_matches(descriptor, content)
        ):
            raise OSError("published private file is unsafe")
    except (OSError, RuntimeError, ValueError):
        if published:
            _unlink_matching(path, identity)
        raise AtomicPrivateFileError("unable to publish private file") from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            _unlink_matching(temporary, identity)

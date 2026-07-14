from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from qa import atomic_private_file as atomic


def test_private_file_path_stays_hidden_until_content_is_synced(
    tmp_path: Path, monkeypatch
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "facts.json"
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original = atomic._write_and_sync

    def delayed_write(descriptor: int, content: bytes) -> None:
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("atomic publication test did not release writer")
        original(descriptor, content)

    monkeypatch.setattr(atomic, "_write_and_sync", delayed_write)

    def publish() -> None:
        try:
            atomic.create_private_file(target, b'{"status":"PASS"}\n')
        except BaseException as exc:  # surfaced in the main test thread
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    assert entered.wait(timeout=5)
    assert not target.exists()
    assert any(private.iterdir())
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert target.read_bytes() == b'{"status":"PASS"}\n'
    metadata = target.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.geteuid()
    assert metadata.st_nlink == 1
    assert list(private.iterdir()) == [target]


def test_private_file_publication_never_replaces_an_existing_path(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "facts.json"
    target.write_bytes(b"original\n")
    target.chmod(0o600)

    with pytest.raises(atomic.AtomicPrivateFileError):
        atomic.create_private_file(target, b"replacement\n")

    assert target.read_bytes() == b"original\n"
    assert list(private.iterdir()) == [target]


def test_private_file_publication_supports_empty_content(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "events.jsonl"

    atomic.create_private_file(target)

    metadata = target.stat()
    assert target.read_bytes() == b""
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1


def test_private_file_publication_preserves_an_existing_symlink(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    victim = private / "victim"
    victim.write_bytes(b"victim\n")
    victim.chmod(0o600)
    target = private / "facts.json"
    target.symlink_to(victim)

    with pytest.raises(atomic.AtomicPrivateFileError):
        atomic.create_private_file(target, b"replacement\n")

    assert target.is_symlink()
    assert victim.read_bytes() == b"victim\n"


def test_private_file_publication_detects_a_hardlinked_temporary_inode(
    tmp_path: Path, monkeypatch
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "facts.json"
    stolen = private / "stolen"
    original = atomic._write_and_sync

    def hardlink_after_write(descriptor: int, content: bytes) -> None:
        original(descriptor, content)
        temporary = next(
            path for path in private.iterdir() if path.name.startswith(".facts.json.")
        )
        os.link(temporary, stolen)

    monkeypatch.setattr(atomic, "_write_and_sync", hardlink_after_write)

    with pytest.raises(atomic.AtomicPrivateFileError):
        atomic.create_private_file(target, b"private\n")

    assert not target.exists()
    assert stolen.read_bytes() == b"private\n"
    assert list(private.iterdir()) == [stolen]


def test_private_file_publication_rolls_back_after_temporary_unlink_failure(
    tmp_path: Path, monkeypatch
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "facts.json"
    original = Path.unlink
    failed_once = False

    def fail_first_temporary_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal failed_once
        if path.name.startswith(".facts.json.") and not failed_once:
            failed_once = True
            raise OSError("injected unlink failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_temporary_unlink)

    with pytest.raises(atomic.AtomicPrivateFileError):
        atomic.create_private_file(target, b"private\n")

    assert failed_once is True
    assert list(private.iterdir()) == []


def test_private_file_link_window_settles_from_two_links_to_one(
    tmp_path: Path, monkeypatch
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "request.json"
    linked = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original = Path.unlink
    delayed_once = False

    def delay_temporary_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal delayed_once
        if path.name.startswith(".request.json.") and not delayed_once:
            delayed_once = True
            linked.set()
            if not release.wait(timeout=5):
                raise OSError("atomic link-window test did not release writer")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", delay_temporary_unlink)

    def publish() -> None:
        try:
            atomic.create_private_file(target, b"request\n")
        except BaseException as exc:  # surfaced in the main test thread
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    assert linked.wait(timeout=5)
    assert target.stat().st_nlink == 2
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert target.read_bytes() == b"request\n"
    assert target.stat().st_nlink == 1
    assert list(private.iterdir()) == [target]


def test_concurrent_private_publishers_allow_exactly_one_winner(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "facts.json"
    barrier = threading.Barrier(8)
    successes: list[bytes] = []
    failures: list[atomic.AtomicPrivateFileError] = []
    lock = threading.Lock()

    def publish(index: int) -> None:
        content = f"publisher-{index}\n".encode()
        barrier.wait(timeout=5)
        try:
            atomic.create_private_file(target, content)
        except atomic.AtomicPrivateFileError as exc:
            with lock:
                failures.append(exc)
        else:
            with lock:
                successes.append(content)

    workers = [threading.Thread(target=publish, args=(index,)) for index in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert len(successes) == 1
    assert len(failures) == 7
    assert target.read_bytes() == successes[0]
    metadata = target.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    assert list(private.iterdir()) == [target]

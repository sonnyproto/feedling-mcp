from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from qa import harness_provenance


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "qa").mkdir(parents=True)
    (root / "backend").mkdir(parents=True)
    (root / "tools" / "provider_smoke").mkdir(parents=True)
    (root / "qa" / "runner.py").write_text("print('one')\n", encoding="utf-8")
    (root / "tools" / "provider_smoke" / "client.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (root / "tools" / "genesis_e2e.py").write_text(
        "from content_encryption import build_envelope\n", encoding="utf-8"
    )
    (root / "backend" / "content_encryption.py").write_text(
        "def build_envelope():\n    return {}\n", encoding="utf-8"
    )
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    subprocess.run(("git", "-C", str(root), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=QA",
            "-c",
            "user.email=qa@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ),
        check=True,
    )
    return root


def test_collect_binds_head_dirty_state_and_actual_source(tmp_path):
    root = _repo(tmp_path)

    clean = harness_provenance.collect(root)
    assert clean["dirty"] is False
    assert len(clean["git_head"]) == 40
    assert len(clean["source_sha256"]) == 64
    assert len(clean["worker_source_sha256"]) == 64

    (root / "qa" / "runner.py").write_text("print('two')\n", encoding="utf-8")
    dirty = harness_provenance.collect(root)
    assert dirty["git_head"] == clean["git_head"]
    assert dirty["dirty"] is True
    assert dirty["source_sha256"] != clean["source_sha256"]


def test_collect_binds_executed_backend_dependency_and_snapshot(tmp_path):
    root = _repo(tmp_path)
    before = harness_provenance.collect(root)
    snapshot = tmp_path / "snapshot"
    for relative in harness_provenance.WORKER_SOURCE_PATHS:
        source = root / relative
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)

    assert (
        harness_provenance.snapshot_digest(snapshot)
        == before["worker_source_sha256"]
    )

    dependency = root / "backend" / "content_encryption.py"
    dependency.write_text("def build_envelope():\n    return {'changed': True}\n")
    after = harness_provenance.collect(root)
    assert after["source_sha256"] != before["source_sha256"]
    assert after["worker_source_sha256"] != before["worker_source_sha256"]


def test_collect_ignores_dotenv_and_cache_content(tmp_path):
    root = _repo(tmp_path)
    before = harness_provenance.collect(root)
    (root / "qa" / ".env.test").write_text("SECRET=do-not-read\n", encoding="utf-8")
    cache = root / "qa" / "__pycache__"
    cache.mkdir()
    (cache / "runner.pyc").write_bytes(b"cache")

    after = harness_provenance.collect(root)
    assert after["source_sha256"] == before["source_sha256"]


def test_collect_rejects_symlinked_harness_files(tmp_path):
    root = _repo(tmp_path)
    target = tmp_path / "outside.py"
    target.write_text("outside\n", encoding="utf-8")
    (root / "qa" / "linked.py").symlink_to(target)

    with pytest.raises(
        harness_provenance.HarnessProvenanceError, match="source is unsafe"
    ):
        harness_provenance.collect(root)


def test_collect_rejects_symlinked_top_level_harness_directory(tmp_path):
    root = _repo(tmp_path)
    subprocess.run(("git", "-C", str(root), "rm", "-qr", "qa"), check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    (root / "qa").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        harness_provenance.HarnessProvenanceError, match="source is unsafe"
    ):
        harness_provenance.collect(root)


def test_collect_caps_file_count(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (root / "qa" / "second.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(harness_provenance, "_MAX_FILE_COUNT", 1)

    with pytest.raises(
        harness_provenance.HarnessProvenanceError, match="too many files"
    ):
        harness_provenance.collect(root)

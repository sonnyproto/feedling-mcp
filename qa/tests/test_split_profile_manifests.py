from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from qa import split_profile_manifests as splitter
from qa.orchestration_contract import PROFILE_IDS


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _manifest(tmp_path: Path) -> Path:
    private = _private_dir(tmp_path / "private")
    payload = {
        "schema_version": 1,
        "base_url": "https://test-api.feedling.app",
        "runtime_mode": "db_action_v2",
        "profiles": [
            {
                "profile_id": profile_id,
                "api_key": f"account-key-{profile_id}",
                "secret_key_b64": f"content-key-{profile_id}",
            }
            for profile_id in PROFILE_IDS
        ],
    }
    path = private / "provisioning.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


def test_splitter_creates_exact_eight_owner_only_one_row_manifests(tmp_path):
    manifest = _manifest(tmp_path)
    output = _private_dir(manifest.parent / "profiles")

    created = splitter.split_manifest(manifest, output)

    assert [path.name for path in created] == [
        f"{profile_id}.json" for profile_id in PROFILE_IDS
    ]
    for profile_id, path in zip(PROFILE_IDS, created, strict=True):
        payload = json.loads(path.read_text())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert payload["base_url"] == "https://test-api.feedling.app"
        assert payload["profiles"] == [
            {
                "profile_id": profile_id,
                "api_key": f"account-key-{profile_id}",
                "secret_key_b64": f"content-key-{profile_id}",
            }
        ]
        for other_profile_id in PROFILE_IDS:
            if other_profile_id != profile_id:
                assert f"account-key-{other_profile_id}" not in path.read_text()


def test_splitter_rejects_non_private_source_and_nonempty_destination(tmp_path):
    manifest = _manifest(tmp_path)
    output = _private_dir(manifest.parent / "profiles")
    manifest.chmod(0o644)
    with pytest.raises(splitter.ManifestSplitError, match="unsafe"):
        splitter.split_manifest(manifest, output)

    manifest.chmod(0o600)
    (output / "stale.json").write_text("{}")
    with pytest.raises(splitter.ManifestSplitError, match="nonempty"):
        splitter.split_manifest(manifest, output)


def test_splitter_rejects_reordered_or_missing_profile_matrix(tmp_path):
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["profiles"].reverse()
    manifest.write_text(json.dumps(payload))
    manifest.chmod(0o600)
    output = _private_dir(manifest.parent / "profiles")
    with pytest.raises(splitter.ManifestSplitError, match="matrix"):
        splitter.split_manifest(manifest, output)


def test_splitter_rolls_back_partial_output(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    output = _private_dir(manifest.parent / "profiles")
    original = splitter._write_private_json
    calls = 0

    def fail_second(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise splitter.ManifestSplitError("synthetic write failure")
        original(path, payload)

    monkeypatch.setattr(splitter, "_write_private_json", fail_second)
    with pytest.raises(splitter.ManifestSplitError, match="synthetic"):
        splitter.split_manifest(manifest, output)
    assert list(output.iterdir()) == []

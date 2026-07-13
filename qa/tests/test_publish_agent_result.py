from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from qa import publish_agent_result as publisher


_DEFAULT = object()


def _private_result(path: Path, value: object = _DEFAULT) -> None:
    path.write_text(json.dumps({"ok": True} if value is _DEFAULT else value))
    path.chmod(0o600)


def test_publish_exclusively_installs_private_regular_json(tmp_path: Path):
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()
    source = private / "agent-result.json"
    destination = public / "run-result.json"
    _private_result(source)

    publisher.publish(source.absolute(), destination.absolute())

    assert json.loads(destination.read_text()) == {"ok": True}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", (0o400, 0o640, 0o644))
def test_private_result_requires_exact_owner_only_mode(tmp_path: Path, mode: int):
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()
    source = private / "agent-result.json"
    _private_result(source)
    source.chmod(mode)

    with pytest.raises(publisher.PublishError, match="metadata"):
        publisher.publish(source.absolute(), (public / "run-result.json").absolute())


def test_private_result_rejects_symlink_and_hardlink(tmp_path: Path):
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()
    target = private / "target.json"
    _private_result(target)
    symlink = private / "agent-result.json"
    symlink.symlink_to(target)
    with pytest.raises(publisher.PublishError, match="missing or unsafe"):
        publisher.publish(symlink.absolute(), (public / "run-result.json").absolute())

    symlink.unlink()
    os.link(target, symlink)
    with pytest.raises(publisher.PublishError, match="metadata"):
        publisher.publish(symlink.absolute(), (public / "run-result.json").absolute())


def test_existing_public_file_or_symlink_is_never_followed_or_replaced(tmp_path: Path):
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()
    source = private / "agent-result.json"
    _private_result(source)
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged")
    destination = public / "run-result.json"
    destination.symlink_to(protected)

    with pytest.raises(publisher.PublishError, match="exclusively"):
        publisher.publish(source.absolute(), destination.absolute())

    assert protected.read_text() == "unchanged"
    assert destination.is_symlink()


@pytest.mark.parametrize("value", ([], "text", 1, None))
def test_result_must_be_one_json_object(tmp_path: Path, value: object):
    private = tmp_path / "private"
    public = tmp_path / "public"
    private.mkdir()
    public.mkdir()
    source = private / "agent-result.json"
    _private_result(source, value)

    with pytest.raises(publisher.PublishError, match="JSON object"):
        publisher.publish(source.absolute(), (public / "run-result.json").absolute())

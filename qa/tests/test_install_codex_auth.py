from __future__ import annotations

import base64
import json
import stat

import pytest

from qa import install_codex_auth as installer


def _document(**overrides):
    document = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "header.payload.signature-for-dedicated-qa",
            "access_token": "access-token-for-dedicated-qa-account",
            "refresh_token": "refresh-token-for-dedicated-qa-account",
            "account_id": "account-dedicated-qa",
        },
        "last_refresh": "2026-07-13T00:00:00Z",
        "agent_identity": None,
        "personal_access_token": None,
        "bedrock_api_key": None,
    }
    document.update(overrides)
    return document


def _encoded(document: dict) -> bytes:
    return base64.b64encode(json.dumps(document).encode("utf-8"))


def test_installs_only_refreshable_chatgpt_auth_with_private_mode(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    destination, masks = installer.install_auth(codex_home, _encoded(_document()))

    installed = json.loads(destination.read_text(encoding="utf-8"))
    assert installed["auth_mode"] == "chatgpt"
    assert installed["OPENAI_API_KEY"] is None
    assert installed["tokens"]["refresh_token"] in masks
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "document",
    (
        _document(auth_mode="apikey"),
        _document(OPENAI_API_KEY="sk-proj-forbidden-provider-key"),
        _document(personal_access_token="at-forbidden-personal-access-token"),
        _document(agent_identity={"agent_private_key": "forbidden"}),
        _document(tokens={"access_token": "missing-refresh-token-value"}),
        {**_document(), "unexpected": "value"},
    ),
)
def test_rejects_non_subscription_or_unsupported_auth(document):
    with pytest.raises(installer.CodexAuthInstallError):
        installer.decode_and_validate(_encoded(document))


def test_rejects_invalid_base64_without_echoing_input():
    secret = b"not-base64-with-a-sensitive-value"
    with pytest.raises(installer.CodexAuthInstallError) as exc:
        installer.decode_and_validate(secret)
    assert secret.decode() not in str(exc.value)


def test_refuses_to_overwrite_existing_auth(tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("existing", encoding="utf-8")
    with pytest.raises(installer.CodexAuthInstallError, match="unable to create"):
        installer.install_auth(codex_home, _encoded(_document()))


def test_cli_masks_decoded_tokens_and_never_prints_bundle(
    tmp_path, monkeypatch, capsys
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    document = _document()
    bundle = _encoded(document)

    class FakeStdin:
        class Buffer:
            @staticmethod
            def read():
                return bundle

        buffer = Buffer()

    monkeypatch.setattr(installer.sys, "stdin", FakeStdin())
    rc = installer.main(["--codex-home", str(codex_home)])
    output = capsys.readouterr().out

    assert rc == 0
    assert bundle.decode() not in output
    for field in ("id_token", "access_token", "refresh_token"):
        assert f"::add-mask::{document['tokens'][field]}" in output

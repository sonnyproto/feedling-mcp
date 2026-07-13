#!/usr/bin/env python3
"""Install a dedicated QA account's ChatGPT OAuth bundle without logging it.

The base64-encoded ``auth.json`` is read from stdin.  The accepted shape is
deliberately narrower than Codex's full auth schema: only refreshable ChatGPT
OAuth credentials are allowed.  API keys, PATs, and managed agent identities
are rejected so the qualification workflow cannot silently change billing or
identity modes.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import stat
import sys
from pathlib import Path
from typing import Sequence


MAX_ENCODED_BYTES = 256 * 1024
MAX_DECODED_BYTES = 128 * 1024
_TOP_LEVEL_KEYS = {
    "auth_mode",
    "OPENAI_API_KEY",
    "tokens",
    "last_refresh",
    "agent_identity",
    "personal_access_token",
    "bedrock_api_key",
}
_TOKEN_KEYS = {"id_token", "access_token", "refresh_token", "account_id"}


class CodexAuthInstallError(RuntimeError):
    """Safe fixed-category error for malformed or unsafe auth input."""


def _nonempty_secret(value: object, field: str) -> str:
    if not isinstance(value, str) or not (20 <= len(value) <= 64 * 1024):
        raise CodexAuthInstallError(f"Codex OAuth field {field} is missing or invalid")
    if any(character.isspace() for character in value):
        raise CodexAuthInstallError(f"Codex OAuth field {field} is missing or invalid")
    return value


def decode_and_validate(encoded: bytes) -> tuple[dict, tuple[str, ...]]:
    """Return a canonical refreshable ChatGPT auth document and mask values."""
    compact = b"".join(encoded.split())
    if not compact or len(compact) > MAX_ENCODED_BYTES:
        raise CodexAuthInstallError("Codex OAuth bundle is missing or too large")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        raise CodexAuthInstallError("Codex OAuth bundle is not valid base64") from None
    if not raw or len(raw) > MAX_DECODED_BYTES:
        raise CodexAuthInstallError("Codex OAuth bundle is empty or too large")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise CodexAuthInstallError("Codex OAuth bundle is not valid JSON") from None
    if not isinstance(document, dict) or set(document) - _TOP_LEVEL_KEYS:
        raise CodexAuthInstallError("Codex OAuth bundle has an unsupported shape")
    if document.get("auth_mode") not in (None, "chatgpt"):
        raise CodexAuthInstallError("Codex OAuth bundle must use ChatGPT auth")
    for forbidden in (
        "OPENAI_API_KEY",
        "agent_identity",
        "personal_access_token",
        "bedrock_api_key",
    ):
        if document.get(forbidden) not in (None, ""):
            raise CodexAuthInstallError(
                "Codex OAuth bundle contains a forbidden auth mode"
            )

    tokens = document.get("tokens")
    if not isinstance(tokens, dict) or set(tokens) - _TOKEN_KEYS:
        raise CodexAuthInstallError("Codex OAuth token bundle has an unsupported shape")
    id_token = _nonempty_secret(tokens.get("id_token"), "id_token")
    if len(id_token.split(".")) != 3 or any(not part for part in id_token.split(".")):
        raise CodexAuthInstallError("Codex OAuth field id_token is missing or invalid")
    access_token = _nonempty_secret(tokens.get("access_token"), "access_token")
    refresh_token = _nonempty_secret(tokens.get("refresh_token"), "refresh_token")
    account_id = tokens.get("account_id")
    if account_id is not None and (
        not isinstance(account_id, str)
        or not account_id
        or len(account_id) > 4096
        or any(character.isspace() for character in account_id)
    ):
        raise CodexAuthInstallError("Codex OAuth account id is invalid")
    last_refresh = document.get("last_refresh")
    if last_refresh is not None and (
        not isinstance(last_refresh, str) or not last_refresh or len(last_refresh) > 256
    ):
        raise CodexAuthInstallError("Codex OAuth refresh timestamp is invalid")

    canonical = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
        },
        "last_refresh": last_refresh,
        "agent_identity": None,
        "personal_access_token": None,
        "bedrock_api_key": None,
    }
    return canonical, (id_token, access_token, refresh_token)


def install_auth(codex_home: Path, encoded: bytes) -> tuple[Path, tuple[str, ...]]:
    if not codex_home.is_absolute() or codex_home.is_symlink():
        raise CodexAuthInstallError("run-scoped CODEX_HOME is unsafe")
    try:
        resolved_home = codex_home.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CodexAuthInstallError("run-scoped CODEX_HOME is missing") from None
    if not resolved_home.is_dir():
        raise CodexAuthInstallError("run-scoped CODEX_HOME is not a directory")
    resolved_home.chmod(0o700)

    document, mask_values = decode_and_validate(encoded)
    destination = resolved_home / "auth.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(destination, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise CodexAuthInstallError("unable to create run-scoped Codex auth") from None
    if stat.S_IMODE(destination.stat().st_mode) != 0o600:
        raise CodexAuthInstallError("run-scoped Codex auth permissions are unsafe")
    return destination, mask_values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install run-scoped Codex OAuth auth")
    parser.add_argument("--codex-home", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        destination, mask_values = install_auth(
            args.codex_home, sys.stdin.buffer.read()
        )
    except CodexAuthInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "ERROR: Codex OAuth installation encountered an internal error",
            file=sys.stderr,
        )
        return 1
    for value in mask_values:
        print(f"::add-mask::{value}")
    print(f"Codex OAuth installed with mode 0600 at {destination.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
User-MCP consumer wiring tests for tools/chat_resident_consumer.py
==================================================================

Covers Task 6 of the user-MCP-servers feature:
  - `{mcp}` placeholder value per lane/driver (`_user_mcp_cli_value`)
  - poll fingerprint sensing (`_update_user_mcp_advertised`)
  - fetch + decrypt + materialize (`_maybe_apply_user_mcp` / `_materialize_user_mcp`)
  - self-update whitelist covers the new pure module

Network + enclave decrypt are mocked — no real backend/enclave needed.

Run with:
    cd backend && PYTHONPATH=. /path/to/venv/python -m pytest \
        ../tests/test_user_mcp_consumer.py -v
"""

import base64  # noqa: F401  (kept for parity with sibling suites / future use)
import json
import os
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module bootstrap — set required env vars BEFORE importing consumer.
# consumer reads env at module scope; these must exist first.
# ---------------------------------------------------------------------------

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    # Shared suite convention. Must match the other consumer test modules'
    # bootstrap value: these are module-scope os.environ.setdefault()s, so the
    # first-imported module wins the key and it leaks cross-module — a divergent
    # value here breaks test_chat_resident_consumer's X-API-Key assertions when
    # this module is collected first.
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_CLI_CMD": "claude --allowed-tools 'x' {mcp} -p {message}",
    "CHECKPOINT_FILE": "/tmp/feedling_test_user_mcp_checkpoint.json",
}

for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

# Ensure repo root + backend + tools on path (mirrors existing test suite).
# tools/ is needed so the consumer's bare `import user_mcp_materialize` resolves
# when the consumer itself is imported as the `tools.chat_resident_consumer`
# package (tools/ is NOT sys.path[0] in that case, unlike the CLI entrypoint).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

# Stub content_encryption when backend tree is absent.
try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as c  # noqa: E402  (after env setup)


# ---------------------------------------------------------------------------
# {mcp} placeholder value — per lane, per driver
# ---------------------------------------------------------------------------


def test_mcp_value_claude_and_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(
        c,
        "_user_mcp_applied",
        {"fingerprint": "sha256:x", "servers": [
            {"name": "jira", "enabled": True,
             "url": "https://a.example.com", "headers": {}}]},
    )
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    tpl_claude = "claude --allowed-tools 'x' {mcp} -p {message}"
    tpl_codex = "codex exec --json {mcp} {message}"

    # codex-ness is read from the global AGENT_CLI_CMD (canonical
    # _cli_template_is_codex helper), not from the `template` argument, so
    # each branch below keeps AGENT_CLI_CMD in sync with the template it's
    # exercising — mirroring the one real call site (_render_cli_template),
    # which always passes template=AGENT_CLI_CMD.

    # claude → --mcp-config only on chat lane
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_claude)
    assert c._user_mcp_cli_value(tpl_claude, "chat") == \
        f"--mcp-config {tmp_path}/mcp.json"
    assert c._user_mcp_cli_value(tpl_claude, "background") == ""

    # codex → per-server `enabled=false` override only on non-chat (background)
    # lane. `-c mcp_servers={}` is a no-op (codex deep-merges -c onto config, so
    # an empty parent table leaves each [mcp_servers.<name>] enabled); only an
    # explicit per-server enabled=false actually disables it.
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex)
    assert c._user_mcp_cli_value(tpl_codex, "chat") == ""
    assert c._user_mcp_cli_value(tpl_codex, "background") == \
        "-c mcp_servers.jira.enabled=false"

    # no {mcp} placeholder → empty regardless of lane
    assert c._user_mcp_cli_value("claude -p {message}", "chat") == ""


def test_mcp_value_codex_background_disables_every_enabled_server(monkeypatch, tmp_path):
    """codex background lane must emit one `enabled=false` override per enabled
    server (not one empty map), so every advertised server is hard-off on
    proactive/background turns. Order is stable (sorted by name)."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:multi", "servers": [
            {"name": "jira", "enabled": True, "url": "u", "headers": {}},
            {"name": "confluence", "enabled": True, "url": "u", "headers": {}},
            {"name": "disabled_one", "enabled": False, "url": "u", "headers": {}},
        ]})
    tpl_codex = "codex exec --json {mcp} {message}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex)

    # sorted by name → confluence before jira; the disabled server is skipped.
    assert c._user_mcp_cli_value(tpl_codex, "background") == (
        "-c mcp_servers.confluence.enabled=false "
        "-c mcp_servers.jira.enabled=false"
    )
    # chat lane still relies on config.toml (no per-turn override).
    assert c._user_mcp_cli_value(tpl_codex, "chat") == ""


def test_mcp_value_empty_when_no_enabled_server(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    # no servers at all
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})
    assert c._user_mcp_cli_value("claude {mcp} -p {message}", "chat") == ""
    # a server that is present but disabled must not gate the placeholder open
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x",
         "servers": [{"name": "jira", "enabled": False, "url": "u", "headers": {}}]})
    assert c._user_mcp_cli_value("codex exec {mcp} {message}", "background") == ""


def test_mcp_value_codex_absolute_path_is_recognized(monkeypatch, tmp_path):
    """codex-ness must be driven by the canonical _cli_template_is_codex()
    helper (Path(cmd[0]).name == "codex"), not a naive
    startswith/substring check on the template string — otherwise a
    systemd unit that pins an absolute path (e.g. /usr/local/bin/codex,
    common when PATH isn't guaranteed under a service) would be
    misdetected as the claude driver."""
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x", "servers": [
            {"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    tpl_codex_abs = "/usr/local/bin/codex exec {mcp} {message}"
    monkeypatch.setattr(c, "AGENT_CLI_CMD", tpl_codex_abs)

    assert c._user_mcp_cli_value(tpl_codex_abs, "background") == \
        "-c mcp_servers.jira.enabled=false"
    assert c._user_mcp_cli_value(tpl_codex_abs, "chat") == ""


# ---------------------------------------------------------------------------
# {mcp} injection through _render_cli_template (chat lane splits into 2 args)
# ---------------------------------------------------------------------------


def test_render_cli_template_injects_mcp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:x",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setattr(c, "AGENT_CLI_CMD",
                        "claude {mcp} -p {message}")
    cmd = c._render_cli_template("hello", "sid-1", lane="chat")
    assert "--mcp-config" in cmd
    assert f"{tmp_path}/mcp.json" in cmd
    # background lane collapses the placeholder to nothing
    cmd_bg = c._render_cli_template("hello", "sid-1", lane="background")
    assert "--mcp-config" not in cmd_bg


# ---------------------------------------------------------------------------
# poll fingerprint sensing
# ---------------------------------------------------------------------------


def test_update_user_mcp_advertised(monkeypatch):
    monkeypatch.setattr(c, "_user_mcp_advertised", {})
    c._update_user_mcp_advertised({"fingerprint": "sha256:abc"})
    assert c._user_mcp_advertised == {"fingerprint": "sha256:abc"}
    # non-dict payloads are ignored (no crash, keeps prior state)
    c._update_user_mcp_advertised(None)
    assert c._user_mcp_advertised == {"fingerprint": "sha256:abc"}


# ---------------------------------------------------------------------------
# fetch + decrypt + materialize
# ---------------------------------------------------------------------------


def test_apply_user_mcp_materializes_files(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "claude-home").mkdir()
    (tmp_path / "codex-home").mkdir()
    (tmp_path / "claude-home" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(x:*)"]}}))

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", lambda: {
        "fingerprint": "sha256:new",
        "servers": [{"name": "jira", "enabled": True,
                     "config_envelope": {"id": "e1"}}]})
    monkeypatch.setattr(c, "_decrypt_envelope", lambda env: json.dumps(
        {"url": "https://a.example.com/mcp", "headers": {"X-K": "v"}}).encode())

    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "sha256:new"})
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    c._maybe_apply_user_mcp()

    # generic --mcp-config file
    doc = json.loads((tmp_path / "mcp.json").read_text())
    assert doc["mcpServers"]["jira"]["url"] == "https://a.example.com/mcp"
    # claude settings.json permission rule merged in
    settings = json.loads((tmp_path / "claude-home" / "settings.json").read_text())
    assert "mcp__jira__*" in settings["permissions"]["allow"]
    # pre-existing non-mcp rule preserved
    assert "Bash(x:*)" in settings["permissions"]["allow"]
    # codex config.toml managed block
    codex_config = tmp_path / "codex-home" / "config.toml"
    assert "[mcp_servers.jira]" in codex_config.read_text()
    # applied state advanced
    assert c._user_mcp_applied["fingerprint"] == "sha256:new"
    # both plaintext-secret files are chmod 0600
    assert (os.stat(tmp_path / "mcp.json").st_mode & 0o777) == 0o600
    assert (os.stat(codex_config).st_mode & 0o777) == 0o600


def test_apply_user_mcp_skips_when_fingerprint_unchanged(monkeypatch):
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "sha256:same"})
    monkeypatch.setattr(
        c, "_user_mcp_applied", {"fingerprint": "sha256:same", "servers": []})

    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise AssertionError("should not fetch when fingerprint is unchanged")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _boom)
    c._maybe_apply_user_mcp()
    assert calls["n"] == 0


def test_apply_user_mcp_failure_does_not_raise(monkeypatch):
    """A fetch/decrypt failure logs and returns — never blocks the chat loop —
    and leaves applied state untouched so the next poll retries."""
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": "sha256:z"})
    monkeypatch.setattr(c, "_user_mcp_applied", {"fingerprint": None, "servers": []})

    def _explode():
        raise RuntimeError("network down")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _explode)
    c._maybe_apply_user_mcp()  # must not raise
    assert c._user_mcp_applied["fingerprint"] is None


def test_apply_user_mcp_deletion_prunes_only_managed_rule(monkeypatch, tmp_path):
    """修复 1 回归：删除一个 server 后，settings.json 里它自己的
    mcp__<name>__* 规则要被清掉，但用户在 .mcp.json 里自行配置的、本功能
    不管理的 mcp__othersrv__* 规则必须原样保留（不能被整体 prune 逻辑误删）。
    """
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / "claude-home").mkdir()
    (tmp_path / "claude-home" / "settings.json").write_text(json.dumps(
        {"permissions": {"allow": [
            "Bash(x:*)",
            "mcp__othersrv__*",  # unrelated — not managed by this feature
            "mcp__jira__*",      # stale rule from when jira was applied
        ]}}))

    # jira was previously applied; the new advertised fingerprint carries no
    # servers at all (user deleted the last managed server).
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:old",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": ""})

    def _boom():
        raise AssertionError("empty fingerprint must not fetch envelopes")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _boom)
    c._maybe_apply_user_mcp()

    settings = json.loads((tmp_path / "claude-home" / "settings.json").read_text())
    allow = settings["permissions"]["allow"]
    assert "mcp__jira__*" not in allow          # deleted server's rule pruned
    assert "mcp__othersrv__*" in allow          # unrelated rule untouched
    assert "Bash(x:*)" in allow


def test_apply_user_mcp_clears_to_empty(monkeypatch, tmp_path):
    """Advertised fingerprint empty (all servers removed) materializes an empty
    config without hitting the network."""
    monkeypatch.setattr(c, "USER_MCP_FILE", str(tmp_path / "mcp.json"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(c, "_user_mcp_advertised", {"fingerprint": ""})
    monkeypatch.setattr(
        c, "_user_mcp_applied",
        {"fingerprint": "sha256:old",
         "servers": [{"name": "jira", "enabled": True, "url": "u", "headers": {}}]})

    def _boom():
        raise AssertionError("empty fingerprint must not fetch envelopes")

    monkeypatch.setattr(c, "_fetch_user_mcp_envelopes", _boom)
    c._maybe_apply_user_mcp()
    assert c._user_mcp_applied["fingerprint"] == ""
    assert json.loads((tmp_path / "mcp.json").read_text()) == {"mcpServers": {}}


# ---------------------------------------------------------------------------
# self-update whitelist covers the new pure module
# ---------------------------------------------------------------------------


def test_runtime_repo_files_covers_materialize_module():
    assert "tools/user_mcp_materialize.py" in c._runtime_repo_files()

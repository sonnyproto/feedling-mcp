"""Spawn strategies for per-user consumers — the isolation seam (plan §P5).

The canonical consumer is the existing VPS resident consumer
(``tools/chat_resident_consumer.py``): the agent-runner hosts it in the CVM,
one process per user, driven in ``cli`` mode against ``claude`` / ``codex exec``.
The resident consumer already does poll / enclave-decrypt / reply / output
cleaning / verify-ping / proactive — so the agent-runner only adds multi-tenant
supervision (lease + spawn + per-user isolation), which the single-user resident
consumer lacks.

Default ``process`` strategy = one child process per user in the shared
agent-runner container. ``container`` is the opt-in strong-isolation strategy
(per-user container/volume) — see docs/AGENT_RUNTIME_ISOLATION.md; live spawn
falls back to process until its lifecycle is finished.

``consumer_env`` / ``build_container_argv`` are pure (testable); process/docker
spawn is thin glue.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("feedling.agent_runtime.spawners")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# The canonical consumer (repo_root/tools/chat_resident_consumer.py).
_RESIDENT_CONSUMER = str(_REPO_ROOT / "tools" / "chat_resident_consumer.py")
# The Feedling context CLI the hosted agent pulls perception/memory/screen
# through (skill + Bash, see docs/AGENT_CLI_INTEGRATION_SURVEY.md). Absolute so
# the path resolves the same from the agent's cwd in dev (repo root) and image
# (/app).
_IO_CLI = str(_REPO_ROOT / "tools" / "io_cli.py")
# io_cli verbs exposed to hosted/resident agents; each becomes a scoped Bash
# allow-rule + is documented in the agent prompt so an unattended `claude -p`
# can pull the same native context tools as VPS/OpenClaw.
_IO_CLI_VERBS = (
    "perception",
    "perception-trend",
    "perception-history",
    "memory-index",
    "memory-fetch",
    "screen-recent",
    "screen-read",
)
# Host-side resident sessions rotate at this many turns (vs the shared consumer
# default of 40) so the persona file re-grounds voice more often within a long
# relationship. Host-only — set via consumer_env(); VPS keeps the default.
_HOST_SESSION_MAX_TURNS = "24"

# The how-to prompt shipped beside this module (into the image via COPY backend/).
_AGENT_PROMPT_TEXT = (Path(__file__).resolve().parent / "agent_tools_prompt.md").read_text()
_AGENT_PROMPT_BASENAME = "agent-tools-prompt.md"


def runtime_token_path(home: str) -> str:
    """Path of the per-user runtime-token file the supervisor writes and the
    consumer reads (Stage D)."""
    return f"{home}/runtime-token"


def write_runtime_token(home: str, token: str) -> None:
    """Write/refresh the per-user runtime token (0600). The supervisor calls this
    at spawn and on each heartbeat so the long-running consumer always has a
    fresh, short-lived token."""
    p = Path(runtime_token_path(home))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# Codex speaks the OpenAI Responses wire only. It reaches OpenAI DIRECTLY
# ("native"); every other codex-driven provider (gemini/openrouter/
# openai_compatible) is bridged through the in-CVM LiteLLM gateway, which
# exposes a Responses endpoint and fans out to the real provider. Keep this set
# in sync with hosted/agent_runtime_cutover._CODEX_NATIVE_PROVIDERS.
_CODEX_NATIVE_PROVIDERS = {"openai"}
# The codex provider id for the gateway (referenced in config.toml + cli).
_GATEWAY_PROVIDER_ID = "feedling_gateway"


def _codex_transport(entry: dict) -> str:
    """For a codex entry, how it reaches the provider: ``native`` (direct OpenAI
    Responses) or ``gateway`` (via the in-CVM LiteLLM Responses endpoint). Empty
    for non-codex entries. A missing/unknown provider defaults to ``native`` so a
    dev roster carrying only an OpenAI ``provider_key`` keeps working."""
    if (entry.get("driver") or "").strip().lower() != "codex":
        return ""
    prov = (entry.get("provider") or "").strip().lower()
    if not prov or prov in _CODEX_NATIVE_PROVIDERS:
        return "native"
    return "gateway"


def _codex_gateway_config(*, base_url: str, model: str) -> str:
    """codex ``config.toml`` routing it through the in-CVM LiteLLM gateway: codex
    talks OpenAI Responses to ``base_url`` (the gateway), authenticating with the
    gateway key in ``CODEX_API_KEY``; the gateway holds the upstream provider key
    and translates to the real provider."""
    lines = []
    if model:
        lines.append(f'model = "{model}"')
    lines += [
        f'model_provider = "{_GATEWAY_PROVIDER_ID}"',
        "",
        f"[model_providers.{_GATEWAY_PROVIDER_ID}]",
        'name = "feedling-litellm"',
        f'base_url = "{base_url}"',
        'wire_api = "responses"',
        'env_key = "CODEX_API_KEY"',
    ]
    return "\n".join(lines) + "\n"


def _io_cli_allow_rules(io_cli: str = _IO_CLI) -> list[str]:
    """Claude Bash permission allow-rules scoping the agent to just io_cli."""
    return [f"Bash(python {io_cli} {verb}:*)" for verb in _IO_CLI_VERBS]


# claude (Anthropic-wire) providers that are NOT anthropic itself: they expose an
# Anthropic-compatible API at ``<base_url>/anthropic`` and use their own model id.
# Keep in sync with hosted/agent_runtime_cutover._CLAUDE_PROVIDERS.
_CLAUDE_COMPAT_BASE_URLS = {"deepseek": "https://api.deepseek.com"}


def _claude_anthropic_base_url(entry: dict) -> str:
    """For a claude-driver entry, the ANTHROPIC_BASE_URL the CLI must use, or "".

    Native anthropic returns "" (the CLI default api.anthropic.com is correct).
    deepseek (and any future Anthropic-wire third party) returns its
    ``<base_url>/anthropic`` endpoint — without this the CLI sends the foreign key
    to api.anthropic.com and every turn fails with a non-zero exit."""
    provider = (entry.get("provider") or "").strip().lower()
    if provider not in _CLAUDE_COMPAT_BASE_URLS:
        return ""
    base = (entry.get("base_url") or _CLAUDE_COMPAT_BASE_URLS[provider]).strip().rstrip("/")
    return f"{base}/anthropic"


def _default_cli_cmd(driver: str, home: str, io_cli: str = _IO_CLI) -> str:
    """Default cli command per driver (resident substitutes ``{message}``).

    For claude we pre-grant the io_cli verbs (so an unattended
    ``claude -p`` runs them without an interactive permission prompt) and append
    the how-to as a system prompt from the per-user home. Operators can override
    the whole thing per roster entry via ``cli_cmd``.
    """
    if driver == "codex":
        # --skip-git-repo-check: the consumer's cwd is the user's home, not a git
        # repo; without it `codex exec` refuses ("Not inside a trusted directory")
        # and exits 1 before any model call.
        return "codex exec --skip-git-repo-check --json {message}"
    grant = ",".join(_io_cli_allow_rules(io_cli))
    prompt_file = f"{home}/{_AGENT_PROMPT_BASENAME}"
    return (
        f"claude --allowed-tools '{grant}' "
        f"--append-system-prompt-file {prompt_file} -p {{message}}"
    )


def agent_home_files(
    home: str,
    *,
    driver: str,
    io_cli: str = _IO_CLI,
    codex_transport: str = "native",
    gateway_base_url: str = "",
    model: str = "",
) -> dict[str, str]:
    """Per-user files seeded into the agent home before spawn (pure: path→content).

    Always seeds the perception how-to (referenced by ``--append-system-prompt-file``
    for claude, and read as ``AGENTS.md`` by codex). For claude it also writes a
    ``settings.json`` under ``CLAUDE_CONFIG_DIR`` whose ``permissions.allow``
    pre-authorizes the io_cli command (defense-in-depth alongside the CLI flag).
    For a codex user on the LiteLLM gateway (non-openai provider) it also writes a
    ``config.toml`` pointing codex at the gateway's Responses endpoint.
    """
    files = {f"{home}/{_AGENT_PROMPT_BASENAME}": _AGENT_PROMPT_TEXT}
    if driver == "codex":
        files[f"{home}/codex-home/AGENTS.md"] = _AGENT_PROMPT_TEXT
        if codex_transport == "gateway":
            files[f"{home}/codex-home/config.toml"] = _codex_gateway_config(
                base_url=gateway_base_url, model=model)
    else:
        settings = {"permissions": {"allow": _io_cli_allow_rules(io_cli)}}
        files[f"{home}/claude-home/settings.json"] = json.dumps(settings, indent=2)
    return files


def consumer_env(base_env: dict, entry: dict, *, user_id: str, home: str) -> dict:
    """Build the env for a per-user resident-consumer child.

    Sets the resident consumer's contract (AGENT_MODE/AGENT_CLI_CMD + per-user
    checkpoint/session/image paths) and the provider key (plaintext — the
    supervisor decrypts the envelope via the enclave before spawn). FEEDLING_API_URL
    / FEEDLING_ENCLAVE_URL flow through from ``base_env``. ``base_env`` is not
    mutated.
    """
    driver = (entry.get("driver") or "claude").strip().lower()
    env = dict(base_env)
    # Stage D zero-roster entries carry no api_key — the consumer authenticates
    # with the runtime-token file instead (FEEDLING_RUNTIME_TOKEN_FILE below).
    env["FEEDLING_API_KEY"] = entry.get("api_key", "")
    env["AGENT_MODE"] = entry.get("agent_mode", "cli")
    env["AGENT_CLI_CMD"] = entry.get("cli_cmd") or _default_cli_cmd(driver, home)
    # Per-user isolation: separate checkpoint, agent session, image temp dir, and
    # a per-user agent home (Claude/Codex) so nothing is shared across users.
    env["CHECKPOINT_FILE"] = f"{home}/checkpoint.json"
    env["AGENT_SESSION_FILE"] = f"{home}/agent-session.txt"
    # Host (agent-runner) sessions rotate sooner than the shared consumer default
    # (AGENT_SESSION_MAX_TURNS=40 in chat_resident_consumer.py) to tighten the
    # in-session voice-drift window — the persona file is reread on every fresh
    # spawn, so a shorter session re-grounds voice more often. This is host-only:
    # VPS consumers don't go through consumer_env, so they keep the default 40.
    # Operator env (base_env) wins if it already set the cap.
    env.setdefault("AGENT_SESSION_MAX_TURNS", _HOST_SESSION_MAX_TURNS)
    env["IMAGE_TEMP_DIR"] = f"{home}/images"
    env["CONSUMER_ID"] = f"agent-runner:{user_id}"
    # Stage D: the consumer reads its short-lived runtime token from this file
    # (refreshed by the supervisor). Absent/empty → it falls back to the api key.
    env["FEEDLING_RUNTIME_TOKEN_FILE"] = runtime_token_path(home)
    if driver == "codex":
        env["CODEX_HOME"] = f"{home}/codex-home"
        if _codex_transport(entry) == "gateway":
            # Codex authenticates to the in-CVM LiteLLM gateway with the GATEWAY
            # key; the upstream provider key never enters the consumer process
            # (it lives in the gateway's own config). base_env carries the
            # gateway creds (supervisor environment).
            gw_key = base_env.get("FEEDLING_LITELLM_API_KEY", "")
            if gw_key:
                env["CODEX_API_KEY"] = gw_key
        elif entry.get("provider_key"):
            env["CODEX_API_KEY"] = entry["provider_key"]
    else:
        env["CLAUDE_CONFIG_DIR"] = f"{home}/claude-home"
        if entry.get("provider_key"):
            env["ANTHROPIC_API_KEY"] = entry["provider_key"]
        # Non-anthropic claude-wire providers (deepseek) must point the CLI at
        # their /anthropic endpoint + own model — otherwise the CLI hits
        # api.anthropic.com with a foreign key and every turn exits non-zero.
        anthropic_base = _claude_anthropic_base_url(entry)
        if anthropic_base:
            env["ANTHROPIC_BASE_URL"] = anthropic_base
            model = (entry.get("model") or "").strip()
            if model:
                env["ANTHROPIC_MODEL"] = model
                # claude Code also issues background "small/fast" model calls; point
                # them at the same model so they don't 404 a claude-* default.
                env["ANTHROPIC_SMALL_FAST_MODEL"] = model
    return env


# ---- process strategy (default) ----


def _signal_alive(pid: int) -> bool:
    """Best-effort liveness for a pid we don't own a handle for. NOTE: on POSIX
    this returns True for an unreaped zombie — only a fallback for pids not in a
    ProcessSpawner's registry (e.g. after a supervisor restart)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal_kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class ProcessSpawner:
    """Default isolation: resident consumer as a child process per user, reaped
    via the Popen handle.

    Keeping the ``Popen`` and using ``poll()`` avoids the zombie trap:
    ``os.kill(pid, 0)`` succeeds for a zombie, so a crashed/idle-exited consumer
    would otherwise look alive forever and never be respawned. ``poll()`` reaps
    the child and reports the real exit.
    """

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}

    def register(self, proc: subprocess.Popen) -> int:
        self._procs[proc.pid] = proc
        return proc.pid

    def spawn(self, entry: dict, user_id: str, home: str) -> int:
        driver = (entry.get("driver") or "claude").strip().lower()
        files = agent_home_files(
            home, driver=driver,
            codex_transport=_codex_transport(entry),
            gateway_base_url=os.environ.get("FEEDLING_LITELLM_BASE_URL", ""),
            model=str(entry.get("model") or ""),
        )
        for path, content in files.items():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        env = consumer_env(os.environ, entry, user_id=user_id, home=home)
        return self.register(subprocess.Popen([sys.executable, _RESIDENT_CONSUMER], env=env))

    def is_alive(self, pid: int) -> bool:
        proc = self._procs.get(pid)
        if proc is None:
            return _signal_alive(pid)
        return proc.poll() is None  # poll() reaps; None means still running

    def kill(self, pid: int) -> None:
        proc = self._procs.get(pid)
        if proc is None:
            _signal_kill(pid)
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._procs.pop(pid, None)


# ---- container strategy (opt-in strong isolation) ----

_CONSUMER_ENV_KEYS = (
    "FEEDLING_API_KEY", "FEEDLING_API_URL", "FEEDLING_ENCLAVE_URL",
    "AGENT_MODE", "AGENT_CLI_CMD", "CHECKPOINT_FILE", "AGENT_SESSION_FILE",
    "IMAGE_TEMP_DIR", "CONSUMER_ID", "FEEDLING_RUNTIME_TOKEN_FILE",
    "ANTHROPIC_API_KEY", "CODEX_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "FEEDLING_LITELLM_BASE_URL", "FEEDLING_LITELLM_API_KEY",
)


def build_container_argv(entry: dict, *, user_id: str, home: str, image: str) -> list[str]:
    """`docker run` argv for a per-user, strongly-isolated resident consumer.

    Secrets pass by env-var *reference* (``-e KEY``, value inherited from the
    supervisor's environment via ``consumer_env``) so they never appear as
    plaintext argv. One named container + one named volume per user — no shared
    home.
    """
    env = consumer_env({}, entry, user_id=user_id, home="/agent-data")
    argv = [
        "docker", "run", "-d",
        "--name", f"feedling-agent-{user_id}",
        "--restart", "unless-stopped",
        "-v", f"feedling-agent-vol-{user_id}:/agent-data",
    ]
    for key in _CONSUMER_ENV_KEYS:
        if key in env:
            argv += ["-e", key]
    argv += [image, "python", "-u", "tools/chat_resident_consumer.py"]
    return argv


def get_spawner(kind: str):
    """Return (spawn_fn, alive_fn, kill_fn) bound to one shared ProcessSpawner.

    'process' (default) is the v1 path. 'container' falls back to process until
    the container lifecycle is finished — see docs/AGENT_RUNTIME_ISOLATION.md.
    """
    if kind == "container":
        log.warning("isolation=container not yet wired for live spawn; using process strategy")
    sp = ProcessSpawner()
    return sp.spawn, sp.is_alive, sp.kill

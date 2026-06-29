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
    "identity-write",
    "screen-recent",
    "screen-read",
    # photo-* are documented in agent_tools_prompt.md and implemented in io_cli;
    # without them in the allowlist Claude's --allowed-tools blocks the call while
    # the prompt says it's available (prompt/allowlist consistency, cutover gate 5).
    "photo-recent",
    "photo-read",
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
        #
        # --dangerously-bypass-approvals-and-sandbox: codex's Linux sandbox
        # (read-only / workspace-write) wraps every model-generated command in
        # bubblewrap, which needs unprivileged user namespaces. The dstack/TDX CVM
        # kernel DISABLES them, so bwrap dies with "No permissions to create a new
        # namespace" and EVERY shell command — including the io_cli memory /
        # perception / identity reads the agent depends on — fails to launch. The
        # agent then reports "can't read memory" although the data is present.
        # (Verified in-CVM on codex-cli 0.142.3: `--sandbox workspace-write` → bwrap
        # namespace error; bypass → commands run and reach the network.) The CVM
        # itself (TEE + per-user home) is the isolation boundary, so we run codex
        # with its sandbox bypassed — the documented mode for "environments that
        # are externally sandboxed". This supersedes the earlier `--sandbox
        # workspace-write + sandbox_workspace_write.network_access` approach, which
        # is moot when bwrap cannot initialize at all. claude-driver runs its Bash
        # in the normal process env and never used bwrap.
        return (
            "codex exec --skip-git-repo-check --json "
            "--dangerously-bypass-approvals-and-sandbox {message}"
        )
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
    persona_content: str = "",
) -> dict[str, str]:
    """Per-user files seeded into the agent home before spawn (pure: path→content).

    Always seeds the perception/tools how-to (referenced by ``--append-system-prompt-file``
    for claude, and read as ``AGENTS.md`` by codex). When ``persona_content`` is
    present (host genesis distilled a voice/persona file), it is prepended to that
    appended system prompt so the agent boots as itself ("TA"); absent → tools-only,
    which is today's behaviour (fresh start / no genesis / VPS). A single appended
    file avoids depending on the CLI honouring repeated --append-system-prompt-file.
    (persona-first vs tools-first ordering is the open question in spec §12.)

    For claude it also writes a ``settings.json`` under ``CLAUDE_CONFIG_DIR`` whose
    ``permissions.allow`` pre-authorizes the io_cli command (defense-in-depth alongside
    the CLI flag). For a codex user on the LiteLLM gateway (non-openai provider) it
    also writes a ``config.toml`` pointing codex at the gateway's Responses endpoint.
    """
    system_append = _AGENT_PROMPT_TEXT
    persona = (persona_content or "").strip()
    if persona:
        system_append = f"{persona}\n\n---\n\n{_AGENT_PROMPT_TEXT}"
    files = {f"{home}/{_AGENT_PROMPT_BASENAME}": system_append}
    if driver == "codex":
        files[f"{home}/codex-home/AGENTS.md"] = system_append
        if codex_transport == "gateway":
            files[f"{home}/codex-home/config.toml"] = _codex_gateway_config(
                base_url=gateway_base_url, model=model)
    else:
        settings = {"permissions": {"allow": _io_cli_allow_rules(io_cli)}}
        files[f"{home}/claude-home/settings.json"] = json.dumps(settings, indent=2)
    return files


def stale_home_files(home: str, *, driver: str, codex_transport: str = "native") -> list[str]:
    """Per-user home paths a (re)spawn must PRUNE — files ``agent_home_files`` does
    not write for the current driver/transport but a PERSISTENT home may still carry
    from a prior config. Absolute paths.

    The motivating case: a codex user who switched from a gateway provider
    (gemini/openrouter/openai_compatible) to native openai — or to the claude
    driver — leaves a ``codex-home/config.toml`` pointing at the in-CVM LiteLLM
    gateway (``127.0.0.1:4000``). ``agent_home_files`` writes that file only for
    ``gateway`` transport, so on the native/claude path the stale file survives and
    codex keeps routing every turn to a port the supervisor only opens when gateway
    users exist → ``error sending request`` → user-visible fallback. Listing it here
    lets the spawner delete it so native codex falls back to api.openai.com as
    designed. ``gateway`` transport returns [] — it WRITES that config this spawn and
    must never prune it."""
    stale: list[str] = []
    if codex_transport != "gateway":
        stale.append(f"{home}/codex-home/config.toml")
    return stale


def materialize_home(
    home: str,
    *,
    driver: str,
    io_cli: str = _IO_CLI,
    codex_transport: str = "native",
    gateway_base_url: str = "",
    model: str = "",
    persona_content: str = "",
) -> None:
    """Write the per-user home files for a spawn AND prune stale ones a persistent
    home may carry (see ``stale_home_files``). Idempotent — safe before every
    (re)spawn. A path written this spawn is never pruned (the prune list excludes the
    current transport's files, and a final guard skips anything just written)."""
    files = agent_home_files(
        home, driver=driver, io_cli=io_cli, codex_transport=codex_transport,
        gateway_base_url=gateway_base_url, model=model, persona_content=persona_content)
    for path, content in files.items():
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for path in stale_home_files(home, driver=driver, codex_transport=codex_transport):
        if path not in files:
            Path(path).unlink(missing_ok=True)


def _persona_from_blob(blob, decrypt_fn) -> str:
    """Pure: extract the persona markdown from a genesis_persona blob.

    Persona is stored ENCRYPTED (``content_envelope``, same shared-envelope posture
    as identity/memory — no plaintext at rest). ``decrypt_fn(envelope) -> str`` does
    the enclave decrypt. Absent / legacy / malformed / decrypt-error → '' so the
    caller falls back to tools-only (fresh start / no genesis / VPS).
    """
    if not isinstance(blob, dict):
        return ""
    env = blob.get("content_envelope")
    if not (isinstance(env, dict) and env.get("body_ct")):
        return ""
    try:
        return str(decrypt_fn(env) or "")
    except Exception:
        return ""


def _genesis_persona_content(user_id: str, api_key: str | None = None,
                             runtime_token: str = "") -> str:
    """Host genesis voice/persona for this user (decrypted), or '' when absent.

    Persona is stored encrypted (db blob 'genesis_persona' → content_envelope);
    decrypt it via the enclave at spawn so the agent boots as itself. Auth = api_key
    (base roster) OR ``runtime_token`` (Stage-D zero-roster host-all, no per-user
    api_key — cutover gate 3 P0). '' on absent / decrypt-error / no-credential →
    tools-only append. Local imports keep this module pure-unit importable without
    DB/enclave deps. Seam: Codex's genesis writes db.set_blob(user_id,
    'genesis_persona', {encrypted, content_envelope, sha256, ...}).
    """
    try:
        import db  # local import: avoid a module-level DB dep for pure-unit tests
        blob = db.get_blob(user_id, "genesis_persona")
    except Exception as e:
        log.warning("genesis persona blob read failed for %s: %s", user_id, e)
        return ""

    def _decrypt(env: dict) -> str:
        from core import enclave as core_enclave
        raw = core_enclave._decrypt_envelope_via_enclave(
            env, api_key, purpose="genesis_persona", runtime_token=runtime_token)
        return raw.decode("utf-8")

    return _persona_from_blob(blob, _decrypt)


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
        materialize_home(
            home, driver=driver,
            codex_transport=_codex_transport(entry),
            gateway_base_url=os.environ.get("FEEDLING_LITELLM_BASE_URL", ""),
            model=str(entry.get("model") or ""),
            persona_content=_genesis_persona_content(
                user_id, entry.get("api_key"),
                runtime_token=entry.get("runtime_token", "")),
        )
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

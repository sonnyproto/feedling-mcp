# Agent CLI Integration Survey

Date: 2026-06-24

Scope: answer whether Feedling's `tools/io_cli.py` needs a per-agent adapter, or
whether a common `skill/doc + shell exec` path is enough for OpenClaw, Hermes,
Claude Code, and Codex. This is a research document only. It does not change
`io_cli.py`, OpenClaw plugin code, Hermes config, or any running VPS service.

## Verdict

Claude's earlier claim was half right and half too strong.

Correct part: the current Node plugin at
`~/.openclaw/workspace/plugins/feedling-io-tools/index.js` is OpenClaw-specific.
It uses OpenClaw's plugin runtime and registers native OpenClaw tools. If the
goal is a native typed tool surface, each runtime still needs either its own
native registration path or a shared protocol such as MCP.

Overstated part: an agent does not always need a custom adapter just to call
`io_cli.py`. If the agent already has a shell/exec/terminal tool, can see the
repo checkout, and receives a durable instruction file or skill that explains
the CLI contract, it can run:

```bash
python tools/io_cli.py perception now
```

and parse JSON stdout without a Node plugin. That is a common minimum path
across OpenClaw, Hermes, Claude Code, and Codex, subject to permissions,
sandboxing, environment variables, and tool availability.

The practical distinction is:

- Native typed tools/plugins/MCP: better production surface.
- Skill/prompt plus shell exec: good bootstrap and low-maintenance fallback.
- MCP server: best cross-agent typed adapter when more than one runtime needs
  first-class tool discovery and schemas.

## Local Contract

The repository contract in [PERCEPTION_CLI_DESIGN.md](PERCEPTION_CLI_DESIGN.md)
defines `tools/io_cli.py` as a thin, stdlib-only JSON CLI:

- Config from `FEEDLING_API_URL`, `FEEDLING_API_KEY`, and
  `FEEDLING_ENCLAVE_URL`.
- Backend perception route:
  `GET /v1/agent/perception?signals=...`.
- JSON only on stdout, including JSON-shaped errors.
- Current MVP: `perception` verbs. `send`, `wait-for-wake`, `schedule-wake`,
  and `photo` currently return clear phase-2 JSON errors.

This shape is specifically friendly to both native wrappers and generic shell
execution. It does not require SDK dependencies in the agent environment.

## Agent Mechanism Matrix

| Agent | Native typed path | Generic CLI path | MCP path | Conclusion for `io_cli.py` |
| --- | --- | --- | --- | --- |
| OpenClaw | Native plugin registers tools through OpenClaw plugin APIs. The current VPS plugin wraps `io_cli.py` as `perception_now`, `perception_location`, etc. | OpenClaw has an `exec` tool that runs shell commands in the workspace, and OpenClaw skills are markdown instructions that teach the agent how and when to use tools. A workspace skill can instruct the agent to run `python <repo>/tools/io_cli.py perception <signal>`. | OpenClaw can manage outbound MCP server definitions for OpenClaw-managed runs, and it can also expose OpenClaw conversations as an MCP server. | Keep the Node plugin for production OpenClaw native tools. A skill plus `exec` is feasible for bootstrap/manual use if `exec` is allowed and env/repo paths are present. |
| Hermes | Hermes native tools are self-registering Python functions grouped into toolsets. A dedicated Hermes tool would be a Hermes-specific implementation. | Hermes ships terminal/file tools, supports toolsets such as `terminal` and `skills`, and skills can instruct the agent to run external CLIs through the terminal tool. | Hermes has first-class MCP support via `mcp_servers` in `~/.hermes/config.yaml` and discovers MCP tools at startup. | Skill plus terminal is enough for CLI use. If Feedling needs typed discoverability inside Hermes, prefer MCP before writing a Hermes-only tool. |
| Claude Code | Custom non-built-in tools are added through MCP, not by arbitrary in-process plugin registration. | Claude Code has a Bash tool, `CLAUDE.md` project memory, and skills/custom commands. Documentation can tell Claude to run `python tools/io_cli.py ...`, assuming Bash permission and env are available. | Claude Code supports local stdio, HTTP, SSE, and WebSocket MCP servers via `claude mcp add`. | "Docs telling it the command" can work, but it is instruction-following over Bash, not a typed tool. MCP is the native typed route. |
| Codex | Codex can use skills, plugins, AGENTS.md instructions, and MCP. Plugins package reusable skills and can bundle MCP config. | Codex can run local commands in CLI, IDE, and app surfaces; spawned commands inherit Codex sandbox/approval boundaries. A repo skill or AGENTS.md can document `io_cli.py`. | Codex supports stdio and streamable HTTP MCP servers through `codex mcp` or `config.toml`, with allow/deny lists and approval settings. | Skill plus exec is the simple path for Codex. MCP is the typed, discoverable path for repeated use. |

## OpenClaw Findings

OpenClaw has three relevant surfaces:

1. Native plugin tools.
   The current VPS plugin is in
   `~/.openclaw/workspace/plugins/feedling-io-tools/index.js`. It uses
   `definePluginEntry`, reads a configured consumer root and env file, executes
   `tools/io_cli.py`, parses JSON, and registers nine schema-less perception
   tools. This is OpenClaw-specific and appropriate for production OpenClaw
   resident use because tool names are visible to the model as first-class
   callable tools.

2. Skills plus `exec`.
   OpenClaw docs say skills are `SKILL.md` files that teach the agent how and
   when to use tools, and the `exec` tool runs shell commands in the workspace.
   The VPS read-only check also found an existing
   `~/.openclaw/workspace/audited/opencli-skill/SKILL.md` that explicitly
   teaches the agent to run an external CLI (`opencli`) and prefer JSON output.
   That is direct evidence that the "skill instructs agent to run CLI" pattern
   already exists in this OpenClaw environment.

3. MCP.
   OpenClaw has an MCP command surface. `openclaw mcp serve` exposes OpenClaw
   conversations to external MCP clients, while `openclaw mcp add/set/...`
   manages outbound MCP server definitions for OpenClaw-managed runtimes. MCP
   tools are subject to OpenClaw's tool policy and sandbox allowlists.

OpenClaw-specific caveat: generic `exec` is not automatically safe. Its docs
call it a mutating shell surface. Tool policy, sandbox settings, and exec
approvals decide whether the agent may run it and where it runs. For Feedling,
the generic CLI path also needs the consumer checkout, Python, and the three
Feedling env vars in the command environment.

Sources:
[OpenClaw capabilities overview](https://docs.openclaw.ai/tools),
[OpenClaw exec tool](https://docs.openclaw.ai/tools/exec),
[OpenClaw skills](https://docs.openclaw.ai/tools/skills),
[OpenClaw creating skills](https://docs.openclaw.ai/tools/creating-skills),
[OpenClaw tools config](https://docs.openclaw.ai/gateway/config-tools),
[OpenClaw MCP](https://docs.openclaw.ai/cli/mcp),
[OpenClaw CLI backends](https://docs.openclaw.ai/gateway/cli-backends).

## Hermes Findings

Hermes has a clear generic CLI route:

- Built-in tools include terminal execution and file operations.
- Toolsets can include `terminal` and `skills`.
- Skills live under `~/.hermes/skills/` and use progressive disclosure.
- Hermes optional skills include examples that tell the agent to use the
  terminal tool to run a CLI and parse JSON output.

Hermes also has typed routes:

- Native Hermes tools are Python modules registered through Hermes' central
  registry and grouped into toolsets. That is Hermes-specific.
- MCP support ships with standard Hermes installs. `mcp_servers` in
  `~/.hermes/config.yaml` can define stdio or HTTP MCP servers, and Hermes
  discovers their tools at startup.

VPS read-only check:

- `~/.hermes/config.yaml` contains top-level `toolsets`, `terminal`, `skills`,
  `approvals`, and `mcp_servers` sections.
- `~/.hermes/.skills_prompt_snapshot.json` exists and contains a manifest of
  installed skills.
- `~/.hermes/skills/` contains many `SKILL.md` files, including MCP and
  autonomous-agent skills.

Conclusion: Hermes does not require a Hermes-native Feedling tool just to call
`io_cli.py`; a Feedling skill can use the terminal tool. If production Hermes
needs robust typed discovery, use MCP first because it is shared with other
agents.

Sources:
[Hermes tools and toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools),
[Hermes skills system](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills),
[Hermes context files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files),
[Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp),
[Hermes tools runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/tools-runtime),
[Hermes CLI commands](https://hermes-agent.nousresearch.com/docs/reference/cli-commands),
[Hermes CLI skill example](https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/devops/devops-cli).

## Claude Code Findings

Claude Code has two separate ideas that are easy to conflate:

1. Reusable instructions.
   Claude Code loads `CLAUDE.md` project memory and supports skills. Skills are
   prompt-based workflows; custom commands have been merged into skills. This
   is enough to teach Claude when and how to run `io_cli.py`.

2. Tools.
   Claude Code has built-in tools such as Bash. Its tools reference says Bash
   executes shell commands and requires permission. It also says custom tools
   are added by connecting an MCP server; skills run through the existing Skill
   tool rather than adding a new tool entry.

Therefore, "just put it in docs" can be sufficient only in the weaker sense:
Claude can read the instruction and decide to invoke Bash. That gives no native
argument schema, no automatic tool discovery beyond skill matching, and no
strong guarantee that the command is chosen every time. For a first-class
Feedling tool in Claude Code, build an MCP server or configure a local stdio
MCP wrapper.

Sources:
[Claude Code tools reference](https://code.claude.com/docs/en/tools-reference),
[Claude Code skills](https://code.claude.com/docs/en/skills),
[Claude Code memory](https://code.claude.com/docs/en/memory),
[Claude Code MCP](https://code.claude.com/docs/en/mcp),
[Claude Code MCP quickstart](https://code.claude.com/docs/en/mcp-quickstart),
[Claude Code interactive mode](https://code.claude.com/docs/en/interactive-mode).

## Codex Findings

Codex has the same separation:

1. Instructions and reusable workflows.
   Codex reads `AGENTS.md` project instructions and supports skills in CLI,
   IDE, and app surfaces. Skills package instructions, references, and optional
   scripts; they can be repo-scoped under `.agents/skills` or installed at user
   scope. Custom prompts are deprecated in favor of skills.

2. Tool access and sandboxing.
   Codex can run local commands in CLI, IDE, and app surfaces. Those spawned
   commands inherit the active sandbox and approval boundaries. A repo skill can
   document the exact `io_cli.py` invocation, but command execution still
   depends on permissions, network access, env vars, and filesystem scope.

3. MCP and plugins.
   Codex supports stdio and streamable HTTP MCP servers through `codex mcp` or
   `config.toml`, with server-level and per-tool policy. Plugins can bundle
   skills and MCP configuration for distribution.

Conclusion: Codex can use `io_cli.py` through a skill plus command execution.
For repeatable typed Feedling tools, an MCP server bundled by a plugin is the
more native Codex distribution path.

Sources:
[Codex manual](https://developers.openai.com/codex/codex-manual.md),
[Codex skills](https://developers.openai.com/codex/skills.md),
[Codex AGENTS.md guidance](https://developers.openai.com/codex/guides/agents-md.md),
[Codex MCP](https://developers.openai.com/codex/mcp.md),
[Codex sandboxing](https://developers.openai.com/codex/concepts/sandboxing.md),
[Codex plugin build guide](https://developers.openai.com/codex/plugins/build.md).

## Common Minimum Denominator

The common minimum denominator is:

1. A JSON-only CLI with stable commands and exit behavior.
2. Agent has a shell-like tool (`exec`, `terminal`, `Bash`, or Codex command
   execution).
3. Agent can see the repo checkout and Python interpreter.
4. The Feedling env vars are present in the process environment or injected by
   the shell command.
5. A durable instruction surface tells the agent when to call the CLI and how
   to parse the result.

This is valid for the four agents surveyed, but it is not universal for every
agent deployment. It breaks when:

- The agent has no shell/exec tool.
- Shell execution is disabled by policy.
- Sandbox filesystem or network scope cannot reach the repo/backend.
- Env vars are missing or not forwarded into the sandbox/container.
- The model forgets or ignores the instruction.
- The CLI output is not strict JSON.
- The command blocks longer than tool timeouts.

So the generic path is real, but it is an operational convention, not a typed
tool contract.

## Tradeoff Matrix

| Approach | Reliability | Discoverability | Types and validation | Permission gating | Failure visibility | Typical failure mode |
| --- | --- | --- | --- | --- | --- | --- |
| Native plugin/tool registration | High inside that runtime. Tool call is explicit and usually visible in traces. | High. Tool schema/name is in the tool catalog. | Strong if schema is detailed. Current OpenClaw Feedling plugin has no parameters, which is fine for fixed signals. | Runtime-native policy, approvals, sandbox hooks. | Usually clear tool call + structured result. | Runtime-specific code/config drift; must maintain one adapter per runtime if not using a shared protocol. |
| Skill/doc plus shell exec | Medium. Depends on model choosing the command and environment being correct. | Medium/low. Skill description helps, but the CLI is not a native tool. | Weak. The shell command can validate, but the model does not get a function schema. | Uses broad shell permission. Harder to grant narrowly unless the runtime supports command allowlists. | Command stdout/stderr visible, but errors may be less structured unless CLI is strict JSON. | Model forgets command, wrong cwd/env, shell denied, sandbox blocks network, non-JSON text contaminates stdout. |
| MCP server wrapping CLI/API | High across MCP-capable clients once configured. | High. Tools are discovered from MCP server schemas. | Strong. JSON schemas per tool; server can centralize validation. | Per-client MCP approval and allow/deny policy. | Tool calls are labeled by server/tool; startup/auth failures visible in MCP status. | More moving parts: server process, MCP config, auth/env forwarding, startup timeout, per-client installation. |

## Recommendation for Feedling `io_cli.py` Onboarding

1. Keep `tools/io_cli.py` as the canonical lowest-level integration contract.
   Its JSON-only stdlib shape is exactly what both native wrappers and generic
   shell execution need.

2. Keep the current OpenClaw Node plugin for production OpenClaw resident use.
   It is OpenClaw-specific, but it gives the resident model first-class
   Feedling perception tools and avoids relying on a prompt to remember shell
   syntax.

3. Add a shared Feedling CLI skill/doc as the generic onboarding layer in a
   later change. The content should be portable:
   - command examples for all perception signals,
   - required env vars,
   - "JSON stdout only" parsing expectations,
   - disabled-signal handling,
   - timeout and retry guidance,
   - clear warning that this is a shell path, not a native typed tool.

4. For Hermes, Claude Code, and Codex experiments, use the shared skill/doc
   plus terminal/Bash/exec first. Do not write a Hermes-native or Claude-only
   adapter unless shell access is unavailable or the eval shows the model
   repeatedly fails to call the CLI correctly.

5. If two or more non-OpenClaw runtimes need production-grade typed Feedling
   tools, build a small Feedling MCP server that wraps either `io_cli.py` or
   the backend API directly. Then each agent only needs MCP configuration,
   not custom business logic.

6. Rephrase the disputed claim as:

   "The current Node plugin is OpenClaw-specific. Native typed integration
   needs a runtime-specific plugin or a shared MCP server. But `io_cli.py`
   itself is portable: any agent with shell/exec access plus a suitable skill
   or instruction file can call it without a custom adapter."

## Suggested Acceptance Checks

- OpenClaw generic path: add a temporary skill in a non-production session and
  prove the agent can run `python tools/io_cli.py perception now` through
  `exec`. Do not replace the production plugin until this is measured.
- Hermes generic path: run `hermes chat --toolsets terminal,skills` with a
  Feedling skill and verify JSON output from the CLI.
- Claude Code generic path: add the command contract to a local skill or
  `CLAUDE.md`, confirm Bash permission prompt behavior, and verify JSON output.
- Codex generic path: add a repo skill under `.agents/skills/feedling-io/` and
  verify command execution under the intended sandbox/profile.
- MCP path: if built, verify each client can list tools, call
  `perception_now`, and surface disabled signals without direct shell access.

## VPS Read-Only Evidence

Read-only checks on 2026-06-24 using the `openclaw22` SSH alias confirmed:

- OpenClaw workspace contains `TOOLS.md`, `AGENTS.md`, and a `skills/` tree.
- OpenClaw workspace contains
  `audited/opencli-skill/SKILL.md`, a real skill that instructs the agent to
  use an external CLI and prefer JSON output.
- OpenClaw workspace contains
  `plugins/feedling-io-tools/index.js`, the current Feedling native plugin.
- Hermes home contains `config.yaml` with `toolsets`, `terminal`, `skills`,
  `approvals`, and `mcp_servers` top-level sections.
- Hermes home contains `.skills_prompt_snapshot.json` and many installed
  `SKILL.md` files.

No VPS files were edited and no gateway or Hermes process was restarted.

Note: external documentation URLs were cited during the 2026-06-24 survey.
Before reusing this document as an external-facing authority, re-check that the
linked pages are still reachable and still say the same thing.

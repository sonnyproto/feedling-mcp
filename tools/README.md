# tools/

Operator-facing utilities for Feedling. Each entry is independent — none
of these are imported by the backend at runtime.

## `chat_resident_consumer.py` — independent resident chat bridge

A long-running daemon that lets an agent backend participate in Feedling chat.
It owns the Feedling poll loop, calls the real agent entry, and posts the reply
back.

### When you need this

Pick the highest-priority path that can honestly own Live connection:

1. **Independent resident consumer** — use `chat_resident_consumer.py`. This is the normal path for Hermes / OpenClaw / Mac mini / VPS agents.
2. **HTTP/API agent backend** — still use `chat_resident_consumer.py`; it polls Feedling and POSTs user messages into your API.
3. **Desktop MCP runtime** — only skip the bridge if that desktop/runtime process truly stays alive and keeps polling without another operator prompt.

| Your agent runtime | Use chat-resident? |
|--|--|
| Server-resident agent daemon that already owns Feedling polling itself | **No.** It is already the resident. |
| Hermes / OpenClaw / Claude Code on a Mac mini or VPS | **Yes.** Run this independent consumer and point it at the runtime's HTTP or CLI entry. |
| Hermes CLI / mcporter / any CLI that exits after one invocation | **Yes.** The consumer keeps the long-running loop and invokes the CLI per message. |
| Custom Python script that just makes HTTP requests | **Yes.** |
| Plain Anthropic / OpenAI API loop without MCP support | **Yes.** |
| Local Llama / Ollama / vLLM serving a `/chat` endpoint | **Yes.** |
| A CLI tool you want to use as the agent (Hermes-CLI, etc.) | **Yes.** |

If you're in the "Yes" rows, `chat_resident_consumer.py` is the bridge. The
test is whether Feedling has a long-running poll owner, not brand name and not
whether MCP tools exist in some other surface.

### What it does

1. Long-polls `GET {FEEDLING_API_URL}/v1/chat/poll` for new user messages.
2. Fetches each message's plaintext from a configured **decrypt source**
   (the enclave's `/v1/chat/history` mirror, or — fallback — the MCP
   server's `feedling_chat_get_history` tool).
3. Calls your agent backend with the plaintext message and, for image
   messages, the decrypted image context (HTTP POST or CLI invocation,
   configurable).
4. Wraps the reply text into a v1 envelope using
   `backend/content_encryption.py` (imported at runtime) and POSTs it
   back to `/v1/chat/response`.
5. Maintains a checkpoint file so it never re-processes old messages
   after restart.

For image messages (`content_type=image`), the daemon extracts `image_b64`
from the decrypt source. OpenAI-compatible HTTP backends receive a
multimodal `image_url` block, simple HTTP backends receive an `images`
array, and CLI backends receive local image file paths in the message
or in `{image_path}` / `{image_paths}` template slots.

### Quick start

```bash
cp deploy/chat_resident.env.example ~/feedling-chat-resident.env
chmod 600 ~/feedling-chat-resident.env
# Edit ~/feedling-chat-resident.env — fill FEEDLING_API_URL, FEEDLING_API_KEY,
# AGENT_MODE, and one of FEEDLING_ENCLAVE_URL / FEEDLING_MCP_URL.

# Run in the foreground for testing
python tools/chat_resident_consumer.py

# Install as a systemd service for production on a root/server deployment
sudo cp deploy/feedling-chat-resident.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedling-chat-resident
sudo systemctl status feedling-chat-resident
```

For a user-space agent host such as Hermes/OpenClaw on a VPS, install it as
a user service instead of nesting it under the top-level gateway:

```ini
# ~/.config/systemd/user/feedling-chat-resident.service
[Unit]
Description=Feedling Chat Resident Consumer
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/openclaw/work/feedling-mcp
EnvironmentFile=/home/openclaw/feedling-chat-resident.env
ExecStart=/home/openclaw/.hermes/hermes-agent/venv/bin/python /home/openclaw/work/feedling-mcp/tools/chat_resident_consumer.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now feedling-chat-resident.service
journalctl --user -u feedling-chat-resident.service -f
```

The resident consumer may call Hermes/OpenClaw through `AGENT_CLI_CMD` or
`AGENT_HTTP_URL`, but it should be supervised as its own process. Do not
make it a child job inside the current Hermes chat turn or the top-level
Hermes gateway; otherwise the IO chat loop dies or restarts with that host
process.

### ⚠️ Decrypt source is mandatory

The backend stores all user chat messages as v1 encrypted envelopes.
`/v1/chat/poll` returns these with `content=""` — the daemon **must**
be pointed at a decrypt source to read what the user wrote.

Set one of:

- **`FEEDLING_ENCLAVE_URL`** (recommended) — direct HTTPS to the
  enclave's decrypt proxy. Same value as `mcp_server.py` uses.
- **`FEEDLING_MCP_URL`** (fallback) — calls `feedling_chat_get_history`
  on the MCP server. Requires `FEEDLING_MCP_TRANSPORT=streamable-http`.

Without either, the daemon logs `"no plaintext content"` for every
incoming message and never replies. You'd see this as: iOS app shows
your messages going out, but the agent never produces a response.

### Agent backend modes

#### `AGENT_MODE=http`

Use this when your agent exposes a JSON HTTP endpoint:

```
AGENT_MODE=http
AGENT_HTTP_URL=http://127.0.0.1:8080/chat
AGENT_HTTP_TOKEN=                            # Bearer token if your endpoint requires auth
AGENT_HTTP_FIELD=response                    # JSON field that contains the reply text
```

The daemon POSTs `{"message": "<user text>"}` and reads the named field
from the JSON response. For image messages it also includes
`images: [{"mime_type", "data", "data_url"}]`.

For Hermes' API server, use the OpenAI-compatible protocol instead of the
simple JSON shape:

```
AGENT_MODE=http
AGENT_HTTP_PROTOCOL=openai
AGENT_HTTP_URL=http://127.0.0.1:8642/v1/chat/completions
AGENT_HTTP_MODEL=hermes-agent
# AGENT_HTTP_SESSION_KEY is optional; defaults to feedling:{user_id}.
```

The daemon sends `X-Hermes-Session-Key`, stores the returned
`X-Hermes-Session-Id`, and sends it back on later turns.

#### `AGENT_MODE=cli`

Use this when your agent is a command-line tool:

```
AGENT_MODE=cli
AGENT_CLI_CMD=mycli ask {message}
```

`{message}` is substituted with the user's plaintext message. The
command's stdout becomes the reply. For image messages, the consumer writes
the decrypted image to `IMAGE_TEMP_DIR` and either appends the file path to
`{message}` or fills explicit `{image_path}` / `{image_paths}` placeholders
if your CLI supports image arguments.

When running under `systemd`, do not assume your interactive shell `PATH`
is available. Prefer an absolute executable path in `AGENT_CLI_CMD`; if that
is not stable, set `AGENT_CLI_PATH` to the directory that contains the agent
binary.

**CLI agents must produce clean stdout.** Session IDs, prompts, debug
footers, and other decorative output WILL leak into the user's chat
if your CLI doesn't have a quiet mode. The daemon strips a few known
patterns defensively (Hermes' `session_id:` footer for example) but
the safest path is configuring your CLI to be silent.

##### Hermes example

```
AGENT_CLI_PATH=/home/openclaw/.local/bin:/home/openclaw/.hermes/hermes-agent/venv/bin
HERMES_HOME=/home/openclaw/.hermes/profiles/daily
AGENT_CLI_CMD=hermes chat -Q --source tool --max-turns 60 -q "{message}"
```

Do not put `--continue` in `AGENT_CLI_CMD`. On the first turn, Hermes creates
a session and prints `session_id`; the consumer stores it. On later turns the
consumer injects `--resume <session_id>` so Feedling is bound to the same
conversation instead of whichever local Hermes session happens to be latest.
Set `HERMES_HOME` to the same profile used by the user's real resident agent
entry. Do not wrap `{message}` in a special persona prompt such as "You are
Dora..." or "reply naturally"; the resident should call the same agent profile
the user already trusts, with IO as only a new transport.

Before installing the daemon, run the exact Hermes command in a terminal with
a normal user message, a direct identity question, and one tool-using question.
Confirm stdout is a real model reply in the agent's voice each time. If it
returns a shell like "我看到了：<message>。你要我继续展开哪一块?", says tools are
unavailable for a normal tool-using request, prints internal reasoning, or
returns another template, the resident is correctly forwarding messages but the
configured CLI command is not reaching a production-quality agent session. Fix
`HERMES_HOME`, `AGENT_CLI_CMD`, toolset access, max-turns, or session selection
before running it as a service.

### Failure behavior

By default, agent-entry failures are log-only and **no fallback template is
posted into iOS Chat**:

```
SEND_FALLBACK_ON_AGENT_ERROR=false
```

This keeps user-visible chat clean. If the agent command, HTTP endpoint, or
reply sanitizer fails, inspect the resident logs and fix the entry; do not
mask it with "send that once more" / "temporarily unavailable" chat bubbles.
There is an opt-in fallback knob for local experiments, but production
onboarding should leave it disabled.

### Image messages

Image messages are routed to the agent backend as the placeholder
configured in `IMAGE_PLACEHOLDER` plus the decrypted image context. Default:

> `[The user sent an image in IO Chat. Inspect the attached/local image before replying. If your current runtime cannot open the image, say plainly that this connector has not enabled image vision yet.]`

OpenAI-compatible HTTP backends receive the image as a standard
`image_url` block. Simple HTTP backends receive an `images` array. CLI
backends receive a local image file path; use a template such as
`AGENT_CLI_CMD='mycli ask --image "{image_path}" "{message}"'` if your
agent CLI has a first-class image flag.

If the decrypt source cannot provide image bytes, the consumer logs the
failure and routes only the honest placeholder; it should not pretend to
have seen the image.

### Re-auth checklist

If you ran any of these on the iOS side:

- `Settings → Delete Account & Reset` (new account, new key)
- `Settings → Storage → Regenerate API Key`
- Migrated to a new self-hosted backend

… you MUST update `~/feedling-chat-resident.env` with the new
`FEEDLING_API_KEY` and `systemctl restart feedling-chat-resident`.
Otherwise tool calls return 401 `user_not_found` and the consumer logs
errors silently in the background.

Verify with:

```bash
curl -s -H "X-API-Key: <new_key>" $FEEDLING_API_URL/v1/users/whoami
# Expect: 200 with the user_id matching what iOS shows
```

---

## `check_chat_pipeline.py` — health check

End-to-end smoke test for the entire chat pipeline.

```bash
FEEDLING_API_URL=http://127.0.0.1:5001 \
FEEDLING_API_KEY=<your_key> \
python tools/check_chat_pipeline.py
```

Verifies four things:

| Check | OK | WARN | FAIL |
|---|---|---|---|
| Backend reachable | HTTP 200/401 | — | connection refused / 5xx |
| API key accepted | 200 | — | 401 Unauthorized |
| Resident consumer running | systemd active or process found | not running | — |
| Recent closed loop | user + assistant messages in last 10 min | unanswered user message | — |

Exit codes: `0` = OK · `1` = WARN · `2` = FAIL.

Common cases:

- "I configured the skill but nothing happens" → consumer not running (WARN on check 3).
- "Messages arrive but no replies" → consumer running but agent call failing (WARN on check 4 + check the consumer's journalctl).
- "Replies contain weird system noise" → CLI agent not configured with clean output mode.

---

## `audit_live_cvm.py` — TDX attestation CLI

Mirrors the 8 audit checks the iOS app runs. Good for CI gates,
third-party reviewers, agents doing "is this safe" checks.

```bash
export FEEDLING_CVM_APP_ID=9798850e096d770293c67305c6cfdceed68c1d28
export FEEDLING_CVM_GATEWAY_DOMAIN=dstack-pha-prod9.phala.network
export FEEDLING_ATTESTATION_URL="https://${FEEDLING_CVM_APP_ID}-5003s.${FEEDLING_CVM_GATEWAY_DOMAIN}/attestation"
export ETH_SEPOLIA_RPC_URL="https://sepolia.infura.io/v3/<key>"
export FEEDLING_APP_AUTH_CONTRACT=0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F

curl -sk "$FEEDLING_ATTESTATION_URL" > /tmp/fl_cvm_attest.json
python3 tools/audit_live_cvm.py
```

Exit code 0 = all rows pass; on prod9 row 8 is a green disclosure about
ingress-terminated MCP TLS. See `docs/AUDIT.md` for what each row proves.

---

## `dcap/` — DCAP quote parser

Python reference parser + verifier for Intel TDX DCAP quotes. Used by
`audit_live_cvm.py` and mirrors `testapp/FeedlingTest/DCAP/` (Swift) on
iOS so the audit logic is identical on both surfaces. Standalone tests
live in `tools/dcap/test_dcap_parse.py`.

---

## Envelope round-trip tests

| Tool | Verifies |
|---|---|
| `v1_envelope_roundtrip_test.py` | Python `build_envelope` + iOS-style unseal produce identical plaintext |
| `frame_envelope_roundtrip_test.py` | Frame envelope variant (image bytes) round-trips |
| `e2e_encryption_test.py` | Full end-to-end: write encrypted, fetch via enclave decrypt proxy, read back plaintext |

These are correctness tests. Run them after touching `content_encryption.py`
on either side.

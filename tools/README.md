# tools/

Operator-facing utilities for Feedling. Each entry is independent — none
of these are imported by the backend at runtime.

## `chat_resident_consumer.py` — HTTP-mode chat bridge

A long-running daemon that lets a **non-resident agent backend** participate
in Feedling chat. Without it, a Feedling iOS user can send chat messages
to a Feedling server but no one ever replies — unless they're connected
via a runtime that stays alive and handles polling/replying natively.

### When you need this

Pick the highest-priority path that can honestly own Live connection:

1. **Server-resident agent** — no bridge needed if the agent itself is a stable daemon that can poll Feedling and post replies.
2. **Resident bridge for one-shot / local CLI agents** — use `chat_resident_consumer.py`; this is the normal path for Hermes CLI / mcporter / shell-invoked agents.
3. **HTTP/API agent backend** — use `chat_resident_consumer.py` to poll Feedling and POST user messages into your API.
4. **Desktop MCP runtime** — only skip the bridge if the desktop/runtime process truly stays alive and keeps polling without another operator prompt.

| Your agent runtime | Use chat-resident? |
|--|--|
| Server-resident agent daemon that owns polling itself | **No.** It is already the resident. |
| Claude Desktop, Claude Code, OpenClaw, Cursor, Hermes-MCP **when the process stays alive between user turns** | **No.** A resident MCP runtime long-polls and replies natively via `feedling_chat_post_message`. |
| Hermes CLI / mcporter / any MCP-capable CLI that exits after one invocation | **Yes.** MCP tools are not enough; if the process is not resident, it cannot own the chat loop. |
| Custom Python script that just makes HTTP requests | **Yes.** |
| Plain Anthropic / OpenAI API loop without MCP support | **Yes.** |
| Local Llama / Ollama / vLLM serving a `/chat` endpoint | **Yes.** |
| A CLI tool you want to use as the agent (Hermes-CLI, etc.) | **Yes.** |

If you're in the "Yes" rows, `chat_resident_consumer.py` is the bridge.
The test is process lifetime, not brand name and not whether MCP tools exist.

### What it does

1. Long-polls `GET {FEEDLING_API_URL}/v1/chat/poll` for new user messages.
2. Fetches each message's plaintext from a configured **decrypt source**
   (the enclave's `/v1/chat/history` mirror, or — fallback — the MCP
   server's `feedling_chat_get_history` tool).
3. Calls your agent backend with the plaintext message (HTTP POST or
   CLI invocation, configurable).
4. Wraps the reply text into a v1 envelope using
   `backend/content_encryption.py` (imported at runtime) and POSTs it
   back to `/v1/chat/response`.
5. Maintains a checkpoint file so it never re-processes old messages
   after restart.

For image messages (`content_type=image`), the daemon routes a
configurable `IMAGE_PLACEHOLDER` to text-only backends. Vision-capable
backends can opt to call `feedling_chat_get_history` themselves and
read `image_b64` directly.

### Quick start

```bash
cp deploy/chat_resident.env.example ~/feedling-chat-resident.env
chmod 600 ~/feedling-chat-resident.env
# Edit ~/feedling-chat-resident.env — fill FEEDLING_API_URL, FEEDLING_API_KEY,
# AGENT_MODE, and one of FEEDLING_ENCLAVE_URL / FEEDLING_MCP_URL.

# Run in the foreground for testing
python tools/chat_resident_consumer.py

# Install as a systemd service for production
sudo cp deploy/feedling-chat-resident.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedling-chat-resident
sudo systemctl status feedling-chat-resident
```

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

#### `AGENT_MODE=http` (recommended)

Use this when your agent exposes a JSON HTTP endpoint:

```
AGENT_MODE=http
AGENT_HTTP_URL=http://127.0.0.1:8080/chat
AGENT_HTTP_TOKEN=                            # Bearer token if your endpoint requires auth
AGENT_HTTP_FIELD=response                    # JSON field that contains the reply text
```

The daemon POSTs `{"message": "<user text>"}` and reads the named field
from the JSON response.

#### `AGENT_MODE=cli`

Use this when your agent is a command-line tool:

```
AGENT_MODE=cli
AGENT_CLI_CMD=mycli ask {message}
```

`{message}` is substituted with the user's plaintext message. The
command's stdout becomes the reply.

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
# JSON output (preferred — unambiguous field extraction)
AGENT_CLI_PATH=/home/openclaw/.local/bin:/home/openclaw/.hermes/hermes-agent/venv/bin
AGENT_CLI_CMD=hermes chat -Q --continue --max-turns 1 -q "{message}"

# Plain text output (sanitizer strips known footers)
AGENT_CLI_CMD=hermes chat -Q --continue --max-turns 1 -q "{message}"
```

`--continue` keeps Hermes' conversation memory across turns.

Before installing the daemon, run the exact Hermes command in a terminal with
a normal user message and confirm stdout is a real model reply in the agent's
voice. If it returns a shell like "我看到了：<message>。你要我继续展开哪一块?"
or another template, the resident is correctly forwarding messages but the
configured CLI command is not reaching the real agent session. Fix
`AGENT_CLI_CMD` / session selection before running it as a service.

### Image messages

Image messages are routed to the agent backend as the placeholder
configured in `IMAGE_PLACEHOLDER`. Default:

> `[The user just sent you an image. Acknowledge it warmly in your normal voice and ask what they want to share about it. If you can read images, call feedling_chat_get_history to see it.]`

Vision-capable agent backends can ignore the placeholder, call
`GET {FEEDLING_API_URL}/v1/chat/history` themselves, and decode
`image_b64` from the most recent message.

Without this hint, images would be silently dropped (their `content`
field is empty by design; the JPEG lives in `image_b64`).

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

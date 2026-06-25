# feedling-io-tools (OpenClaw plugin)

Canonical source for the OpenClaw plugin that exposes the Feedling perception
CLI (`tools/io_cli.py`) as native `perception_<signal>` tools, so a resident
agent (OpenClaw / Hermes / Claude Code) can pull perception with real agentic
tool calls instead of a "make the model emit JSON" prompt.

This lives in-repo so it is version-controlled and redeployable — it used to
exist only on the VPS (`~/.openclaw/workspace/plugins/feedling-io-tools/`), which
meant the edits were lost on any VPS rebuild.

## What it does
- Registers one tool per signal in `SIGNALS` (`perception_now`, `perception_mood`,
  …). Each shells out to `io_cli.py perception <signal>` and returns the JSON.
- No hardcoded paths/keys: config (openclaw.json) → env → throw. The service env
  file (`FEEDLING_API_URL`/`FEEDLING_API_KEY`/`FEEDLING_ENCLAVE_URL`) is read at
  call time.

## Keep SIGNALS in sync
`SIGNALS` in `index.js` MUST mirror the agent-pullable signals — i.e.
`AGENT_PERCEPTION_SIGNALS` in `backend/agent/routes.py` and the groups in
`tools/io_cli.py`. When a new signal is exposed to the agent, add it here too,
copy this file to the VPS, and restart the gateway (below).

## Deploy to the VPS
```bash
# from a checkout of feedling-mcp:
scp -i <key> deploy/openclaw-plugins/feedling-io-tools/{index.js,openclaw.plugin.json,package.json} \
  openclaw@<host>:~/.openclaw/workspace/plugins/feedling-io-tools/

# configure once (either in openclaw.json plugins.entries['feedling-io-tools'].config
# or as env on the gateway service):
#   FEEDLING_CONSUMER_ROOT=/home/openclaw/feedling-mcp      (has tools/io_cli.py)
#   FEEDLING_SERVICE_ENV=/home/openclaw/feedling-chat-resident.env
#   FEEDLING_PYTHON=python3   (optional)

# the gateway caches plugins — restart to reload:
systemctl --user restart openclaw-gateway.service
```

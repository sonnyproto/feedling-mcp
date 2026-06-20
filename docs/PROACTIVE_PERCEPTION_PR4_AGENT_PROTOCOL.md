# PR4: TurnRunnerV2 Agent Protocol

## Scope

PR4 makes `TurnRunnerV2` speak the V2 agent protocol without cutting over any
production hosted or resident entrypoint.

It adds:

- A pure parser/context module: `backend/proactive/agent_protocol_v2.py`
- Model-facing context construction from tools, time, switches, digest, hints,
  background payloads, and recent chat
- V2 action parsing for `send_message`, `sleep`, `schedule_wake`,
  `cancel_wake`, and `needs_background`
- Manual-wake contract violation marking via `ignored_manual`
- Optional V2 turn/action persistence hooks on `TurnRunnerV2`
- A DB-backed `proactive_turn_actions_v2` stream for agent actions

## Spec Alignment

- Spec §1.1: `user_message` and proactive wakes share the same engine.
- Spec §1.2: raw perception values are not passively injected; the agent sees
  tools and digest/hints, then pulls perception explicitly when needed.
- Spec §1.3: turn protocol is `messages`, `actions`, and `needs_background`.
- Spec §1.4 / §6: background requests do not write chat directly.

## Runtime Decisions

- `run_agent` now receives a model-facing context dictionary, not the internal
  `MergedWakeContextV2` object.
- The parser accepts mappings, JSON strings, or existing outcome-like objects so
  tests can keep using injected stubs.
- Free-form invalid model output becomes `sleep` with `invalid_protocol`; V2
  does not reinterpret arbitrary text as chat.
- Top-level `messages` are also persisted as `send_message` action records so
  dashboard/eval can reconstruct what the agent planned.
- Manual wakes with no visible message are returned as `ignored_manual`. They do
  not silently count as normal sleeps.

## Non-Goals

- No hosted wake cutover.
- No resident cutover.
- No real LLM adapter.
- No direct chat or push write from the background path.
- No iOS perception ingress work; that is gated by PR6b/PR7.

## Tests

Run:

```sh
python3 -m pytest tests/test_proactive_agent_protocol_v2.py tests/test_proactive_runtime_v2.py tests/test_proactive_tool_executor_v2.py tests/test_proactive_store_v2.py -q
python3 -m py_compile backend/proactive/agent_protocol_v2.py backend/proactive/runtime_v2.py backend/proactive/store_v2.py
git diff --check
```

Current local result:

- `34 passed, 8 skipped`
- `py_compile` passed
- `git diff --check` passed

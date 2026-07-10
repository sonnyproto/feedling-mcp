# Self-authored thinking fallback

This is the hosted resident consumer fallback for model API providers that strip
native reasoning metadata. It is default-off and must be enabled per user with
`thinking_fallback=true` in `/v1/model_api/setup`.

Use it for stripped relay cohorts where provider responses only include visible
assistant content, such as the confirmed Jiushi/玖时 relay family, kiro/空悲切
family, and future `openai_compatible` relays that do not expose native
reasoning fields.

Do not enable it for users already receiving native reasoning, such as
OpenRouter/DeepSeek reasoning responses, Anthropic thinking stream-json, Codex
`agent_reasoning`, or Hermes `session_*.json` reasoning. If it is accidentally
enabled for a native-reasoning user, native reasoning still wins in the consumer
metadata merge, and the self-authored protocol is stripped from the chat body
before POST.

Runtime contract:

- Setup persists `thinking_fallback` in the user's `model_api` blob.
- Discovery passes it into the agent-runner roster.
- The spawned consumer receives
  `FEEDLING_SELF_AUTHORED_THINKING_FALLBACK=1`.
- Foreground/proactive agent prompts ask for:
  `<<FEEDLING_THINKING_V2>>...<</FEEDLING_THINKING_V2>>` followed by
  `<<FEEDLING_REPLY_V2>>...<</FEEDLING_REPLY_V2>>`.
- The parser stores the thinking disclosure with
  `thinking_source=self_authored_v2` and `thinking_native=false`.
- `post_reply()` strips any remaining fallback markers before `/v1/chat/response`;
  if a reply cannot be cleaned, it is dropped instead of being stored.

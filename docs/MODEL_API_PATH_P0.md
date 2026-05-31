# IO Model API Key Path P0

Date: 2026-05-31

This document is the implementation plan for the IO-hosted Model API key
route. It follows Option A: the Flask backend / hosted runtime calls the
provider from inside the TDX CVM deployment boundary.

## Decision

The Model API route is not a resident-consumer route and is not an
official-app import-only route.

P0 must support this full backend flow:

1. User selects `model_api`.
2. User saves provider config and API key.
3. Backend encrypts the provider key as a v1 envelope at rest.
4. User uploads chat history.
5. Backend parses/imports history, derives Memory Garden + Identity, and
   writes both as encrypted v1 envelopes.
6. User sends a live chat message to the hosted runtime.
7. Backend decrypts only the context needed for that request through the
   enclave, calls the provider, then writes both user message and assistant
   reply as encrypted v1 envelopes.
8. `/v1/onboarding/validate` evaluates model-api-specific completion instead
   of resident-consumer liveness.

## Privacy Boundary

At rest:

- Provider API key is stored only as a v1 envelope.
- Chat history is stored only as v1 envelopes.
- Memory Garden is stored only as v1 envelopes.
- Identity is stored only as a v1 envelope.
- Screen frames continue to use the existing FrameEnvelope flow.

During runtime:

- The backend may briefly hold provider key, user message, selected memories,
  identity, and optional screen context in process memory.
- The provider receives the prompt/context needed to produce a reply.
- Request/response bodies containing raw user text, provider keys, decrypted
  memory, decrypted identity, or decrypted screen data must not be logged.

Correct product copy:

> IO hosted runtime decrypts the minimum necessary context in TDX CVM memory
> to call your selected model provider. Your chat, memories, identity, and
> screen frames remain encrypted at rest. The selected provider receives the
> plaintext context needed to answer.

## P0 Endpoints

### `POST /v1/onboarding/route`

Body:

```json
{ "route": "resident" | "official_import" | "model_api" }
```

Writes `{FEEDLING_DIR}/{user_id}/onboarding_route.json`.

### `GET /v1/onboarding/route`

Returns the selected route. Defaults to `resident` for existing users.

### `POST /v1/model_api/setup`

Body:

```json
{
  "provider": "openai" | "openrouter" | "openai_compatible",
  "model": "gpt-4.1-mini",
  "api_key": "sk-...",
  "base_url": "https://api.openai.com/v1"
}
```

Behavior:

- Validates provider/model.
- Tests the key with a tiny non-streaming chat completion call.
- Encrypts `api_key` with the user's content public key and enclave content
  public key.
- Stores only metadata and `api_key_envelope`.

### `GET /v1/model_api/get`

Returns safe config metadata only: provider, model, masked key, test status.
Never returns the raw API key or raw envelope by default.

### `POST /v1/model_api/test`

Decrypts the stored provider key through the enclave, runs the provider test,
and updates test status.

### `DELETE /v1/model_api/delete`

Deletes provider config. It does not delete chat/memory/identity.

### `POST /v1/history_import/upload`

Body:

```json
{
  "format": "plaintext",
  "content": "...",
  "relationship_started_at": "2026-05-01",
  "fresh_start": false
}
```

P0 processes synchronously but writes a durable job file under:

```text
{FEEDLING_DIR}/{user_id}/history_import_jobs/{job_id}.json
```

P0 supports plaintext transcripts first. ChatGPT/Claude/Gemini exports are P1.

### `GET /v1/history_import/status/<job_id>`

Returns the durable job state.

### `POST /v1/model_api/chat/send`

Body:

```json
{
  "message": "hello",
  "include_screen_context": false
}
```

Behavior:

- Requires a tested model config.
- Encrypts and appends the user message.
- Decrypts recent chat, selected memories, and identity through the enclave.
- Optionally decrypts recent screen metadata only when requested.
- Calls provider.
- Encrypts and appends the assistant reply.
- Returns IDs, timestamps, and the plaintext reply for the immediate caller.

## Internal Enclave Addition

P0 adds:

```text
POST /v1/envelope/decrypt
```

The caller sends one envelope and authenticates with the user API key. The
enclave resolves the user through Flask `/v1/users/whoami`, checks
`owner_user_id`, decrypts with the enclave content key, and returns
`plaintext_b64`.

This endpoint is required so Flask can decrypt the stored provider key without
putting provider-key plaintext on disk.

## History Import P0 Strategy

P0 supports a pragmatic import path:

- Parse plaintext into normalized messages.
- Use the configured provider to extract memory cards and identity when
  available.
- If provider extraction returns malformed JSON or too few cards, fill the
  remaining required Story/About me floors with deterministic cards from the
  transcript. This keeps onboarding testable while still preferring model-
  extracted quality.
- Only write existing memory types:
  `moment`, `quote`, `fact`, `event`, `insight`, `reflection`.
- P0 writes `moment`, `quote`, `fact`, and `event`. `insight/reflection`
  extraction is P1 because those require anchor semantics.

Identity is initialized only after Story and About me floors are satisfied.
`days_with_user` is computed from `relationship_started_at` or the earliest
timestamp in the imported transcript. If neither exists, the caller must pass
`fresh_start: true`.

## Onboarding Validation

For `route=resident`, current validation stays unchanged.

For `route=model_api`, the validator checks:

- `model_api_config`
- `model_api_test`
- `history_import`
- `memory_garden`
- `identity_card`
- `relationship_anchor`
- `hosted_chat`

It must not require resident consumer headers or `/v1/chat/verify_loop`.

For `route=official_import`, P0 reports import readiness honestly and does not
claim realtime chat support.

## P0 Test Plan

Backend tests:

- Route selection persists.
- Model config never returns raw key.
- Setup writes v1 provider-key envelope.
- Model API validation no longer asks for resident consumer.
- History import writes durable job state.
- Hosted chat stores both user and assistant as encrypted envelopes.
- Enclave arbitrary-envelope decrypt rejects wrong owner.

Manual smoke:

1. Register or use an existing user.
2. `POST /v1/onboarding/route {"route":"model_api"}`.
3. `POST /v1/model_api/setup` with a real OpenAI/OpenRouter-compatible key.
4. `POST /v1/history_import/upload` with a real transcript.
5. Poll `GET /v1/history_import/status/<job_id>`.
6. Verify `/v1/memory/list`, `/v1/identity/get`, `/v1/onboarding/validate`.
7. `POST /v1/model_api/chat/send`.
8. Verify `/v1/chat/history` contains encrypted user + assistant rows.

## Curl Manual Test

Use a deployment where both backend and enclave are running. The local Flask
dev server alone cannot complete this route because provider-key decrypt needs
`FEEDLING_ENCLAVE_URL`.

```bash
export FEEDLING_API_URL="https://api.feedling.app"
export FEEDLING_API_KEY="<io user api key>"
export MODEL_PROVIDER="openrouter"
export MODEL_NAME="openai/gpt-4.1-mini"
export MODEL_API_KEY="<provider api key>"
```

Select route:

```bash
curl -sS "$FEEDLING_API_URL/v1/onboarding/route" \
  -H "X-API-Key: $FEEDLING_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"route":"model_api"}' | jq
```

Save and test provider config:

```bash
curl -sS "$FEEDLING_API_URL/v1/model_api/setup" \
  -H "X-API-Key: $FEEDLING_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"provider\":\"$MODEL_PROVIDER\",
    \"model\":\"$MODEL_NAME\",
    \"api_key\":\"$MODEL_API_KEY\"
  }" | jq
```

Confirm the safe public config does not expose the raw key:

```bash
curl -sS "$FEEDLING_API_URL/v1/model_api/get" \
  -H "X-API-Key: $FEEDLING_API_KEY" | jq
```

Import a small real transcript:

```bash
cat > /tmp/feedling-history.txt <<'EOF'
2026-05-31 User: I prefer direct answers and want to test the API path.
2026-05-31 Assistant: I will keep replies direct and grounded.
EOF

jq -Rs '{
  format: "plaintext",
  content: .,
  relationship_started_at: "2026-05-31"
}' /tmp/feedling-history.txt > /tmp/feedling-history.json

curl -sS "$FEEDLING_API_URL/v1/history_import/upload" \
  -H "X-API-Key: $FEEDLING_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/feedling-history.json | tee /tmp/feedling-import-result.json | jq
```

Poll status:

```bash
export HISTORY_JOB_ID="$(jq -r '.job.job_id' /tmp/feedling-import-result.json)"

curl -sS "$FEEDLING_API_URL/v1/history_import/status/$HISTORY_JOB_ID" \
  -H "X-API-Key: $FEEDLING_API_KEY" | jq
```

Validate onboarding:

```bash
curl -sS "$FEEDLING_API_URL/v1/onboarding/validate" \
  -H "X-API-Key: $FEEDLING_API_KEY" | jq
```

Run hosted chat:

```bash
curl -sS "$FEEDLING_API_URL/v1/model_api/chat/send" \
  -H "X-API-Key: $FEEDLING_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"Can you reply using my imported history?"}' | jq
```

Verify encrypted chat rows exist:

```bash
curl -sS "$FEEDLING_API_URL/v1/chat/history?limit=20" \
  -H "X-API-Key: $FEEDLING_API_KEY" \
  | jq '.messages[] | {id, role, source, has_body_ct: (.body_ct != null), content}'
```

Expected result:

- `model_api/get` shows masked key metadata only.
- `history_import/status` ends at `status=completed`.
- `onboarding/validate` advances to `hosted_chat`, then `complete` after the
  hosted chat call.
- `/v1/chat/history` rows have `body_ct` and empty plaintext `content`.

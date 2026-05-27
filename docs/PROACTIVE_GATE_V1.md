# Proactive Gate V1

V1 changes the Gate from a generic work-assistant trigger into a companion
connection gate. It should not ask "would help be useful?" first. It asks:

> Would the user's own AI companion naturally think of the user in this moment?

That has one required criterion: a concrete connection between the current
screen/context and something specific in the user's identity card, Memory
Garden, or recent passive observations. Recent proactive fires are cooldown
input, not a valid positive connection by themselves.

## Runtime Context

Each non-manual Gate tick builds this context before calling the vision model:

- `identity_card`: decrypted identity card, including agent name, dimensions,
  days together, signature, and any interruption/proactive preferences.
- `memory_set`: decrypted user-visible Memory Garden cards.
- `passive_observations`: recent cards marked as `agent_passive_observation`
  or `passive_observation`.
- `recent_fires`: Gate decisions that reached out in the last 24 hours. The
  backend uses this for cooldown only; one successful proactive reach-out blocks
  the next proactive reach-out for 10 minutes.
- `now_local`: local date/time context.
- `connection_candidates`: normalized source IDs the model is allowed to cite.
- `frames`: 3-5 sampled screen frames after visual scene clustering.

If identity/memory context cannot be loaded, automatic Gate should abstain with
`memory_context_unavailable`. Manual test ticks can still enqueue explicit
debug jobs.

## Model Prompt

System prompt used by the backend:

```text
You are the proactive gate for the user's personal AI companion. Your job is to
decide whether the companion would naturally think of the user in this moment.
Naturally think of has exactly one criterion: a concrete connection between the
current screen/context and something specific in identity_card, memory_set,
or passive_observations. Return JSON only. Do not reveal chain-of-thought.
Do not write the final user-facing message.
```

The user payload gives the model:

- valid connection examples
- invalid connection examples
- decision policy
- the required JSON schema
- all context and sampled frames

The model must return:

```json
{
  "should_reach_out": true,
  "confidence": 0.0,
  "intent_label": "short_snake_case",
  "context_hint": "hidden context for the resident agent",
  "reason": "short true-case audit reason",
  "abstention_reason": "required when false",
  "connection": {
    "source_type": "identity_card | memory_set | passive_observation",
    "source_id": "must match connection_candidates",
    "quote": "supporting text",
    "why_concrete": "why the current screen connects"
  },
  "frame_ids": ["..."]
}
```

The backend rejects a true decision when:

- `context_hint` is missing.
- `connection.source_id` is missing.
- `connection.source_id` is not in `connection_candidates`.

Recent user chat does not block Gate evaluation. This product is a companion
surface, not a work assistant that should disappear because the user has just
spoken. The only automatic send-rate guard in V1 is the proactive-fire cooldown:
after one proactive message bundle is sent, the backend will not enqueue another
proactive hidden job for 10 minutes.

## Hidden Job Realization

When the Gate returns true, the backend creates a hidden job. The independent
resident consumer claims that job and calls the user's configured real agent
entry with:

- `intent_label`
- `context_hint`
- `possible_connections`
- recent chat context: up to the last 20 decrypted chat messages by default
  (`PROACTIVE_RECENT_CHAT_LIMIT`), if the resident has a decrypt source available
- selected screen-frame text and the corresponding images / image paths

The agent writes the actual user-facing message. For a more human rhythm, the
agent may return one message or multiple short bubbles:

```json
{"messages":["first bubble","second bubble"]}
```

The resident enforces a hard cap of 5 bubbles per proactive job.

Recent chat context is only a local continuity aid. It helps the agent avoid a
proactive message that ignores the immediately preceding conversation, especially
for stateless HTTP entries or compressed CLI sessions. It is not the source of
persona or voice: the user's own runtime identity, memory, and normal agent
profile remain authoritative.

## Frame Sampling

The capture stream can produce many near-duplicate frames. Before the model call,
the backend:

1. Decrypts recent candidate frames.
2. Computes an 8x8 perceptual image hash with Pillow.
3. Clusters consecutive frames whose hash distance is below threshold.
4. Takes the last frame of each scene cluster.
5. Caps at 5 while preserving first and latest scene.

This keeps the latest screen state while avoiding token waste from duplicate
screens.

## Human Review Harness

The debug dashboard exposes a review form on every Gate decision:

- `correct_true`: Gate reached out and should have.
- `correct_false`: Gate abstained and should have.
- `missed_opportunity`: Gate abstained but should have reached out.
- `spam`: Gate reached out and should not have.
- `weak_connection`: Gate reached out but the cited connection was not concrete.
- `repeated`: Gate reached out too soon or repeated a prior fire.
- `privacy_bad`: Gate reached out in a context that felt too sensitive.
- `great_companion_moment`: Gate reached out and felt especially natural.

These labels are stored in `gate_reviews.jsonl` per user. They are not used to
auto-train the model directly. They form the beta feedback loop:

1. Review real Gate decisions daily.
2. Export metrics with `tools/proactive_gate_eval.py`.
3. Inspect false positives and false negatives by reason, intent, and connection
   source.
4. Update prompt/policy or memory-writing rules.
5. Replay the same reviewed decisions before shipping.

For human-AI companionship, false negatives matter: if the connection is real,
the companion should have permission to be present. The review goal is not only
"avoid interruption"; it is "reach out when the relationship context makes it
feel like the agent naturally noticed."

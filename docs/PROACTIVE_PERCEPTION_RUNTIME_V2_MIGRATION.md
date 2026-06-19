# Proactive Perception Runtime V2 Migration

This branch starts the V2 runtime spine without switching production traffic.

## Contract

- The new center is
  `WakeEventV2 -> WakeInboxV2 -> MergedWakeContextV2 -> TurnRunnerV2`.
- Legacy `proactive_jobs` are compatibility inputs only. They should be adapted
  into `WakeEventV2` and must not remain the strategic runtime surface.
- Per-user single-flight and wake merging are correctness primitives, not
  "less proactive" gates.
- Perception does not directly create turns. Raw/coarse signals first pass
  through `PerceptionDifferV2`, which owns delta, discrete wake selection,
  presence hints, connectivity anchors, and screen pHash changes.
- Every hosted/resident turn should see the same tool catalog and `cost_class`.
- `enabled/dnd/user_state/ai_state` are legacy settings/state names. New code
  should model Ambient, Scheduled, and Delivery controls explicitly.

## First Milestone

The first milestone is intentionally small:

1. Define the runtime dataclasses and in-memory inbox.
2. Define the v2 tool catalog.
3. Define the Perception Differ skeleton.
4. Define adapters from legacy jobs into `WakeEventV2`.
5. Add contract tests so later work cannot accidentally extend the old shape.

Production routes should be wired to this spine only after the contract tests
cover merge semantics, settings semantics, and hosted/resident parity.

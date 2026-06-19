# PR5b: Observability And Eval Scaffold

## Scope

PR5b adds the health metrics and synthetic per-episode eval scaffold required
before any real user is flipped onto hosted Runtime V2.

It adds:

- `backend/proactive/observability_v2.py`
- `backend/proactive/eval_v2.py`
- Runtime metrics hooks in `RuntimeSpineV2`, `TurnRunnerV2`,
  `BackgroundWorkerV2`, and `PerceptionDifferV2`
- `DBRuntimeMetricsSinkV2` writing `proactive_runtime_metrics_v2`
- Hosted V2 wake runtime wiring to the DB metrics sink
- Replayable synthetic Round 3 episodes and labels

## Spec Alignment

- Spec §10.2: adds a per-episode/session eval unit.
- Spec §10.3: tracks wake volume, merge rate, double-send count/rate, missed
  `scheduled_wake` rate, latency distribution, background append
  success/stale rate, and pHash dedupe rate.
- Spec §10.4: seeds the Round 3 review-label vocabulary:
  `good_presence`, `missed_moment`, `went_dark`, `too_much_buzz`,
  `too_chatty`, `wrong_voice`, `ignored_manual`, `stutter`,
  `late_irrelevant`, `privacy_bad`.

## Runtime Decisions

- Metrics are fire-and-forget. A metrics sink exception never changes wake,
  turn, or background behavior.
- Metrics are events first; aggregation happens outside the runtime so
  observability cannot become a new gate.
- `pHash` metrics are mechanical dedupe metrics only: same frame = deduped,
  changed frame = scene_change. They do not judge whether the content matters.
- Double-send is a health metric/event, not a behavior policy. The runtime can
  record or aggregate it without adding a second judgment layer.
- Hosted Runtime V2 still defaults off; these metrics are for future flag-on
  observation, not a user-visible cutover.

## Non-Goals

- No dashboard UI replacement yet.
- No real production episode import.
- No hosted flag flip.
- No perception ingress cutover.

## Tests

Run:

```sh
python3 -m pytest tests/test_proactive_observability_v2.py tests/test_proactive_background_v2.py tests/test_proactive_runtime_v2.py tests/test_hosted_wake_v2_cutover.py -q
python3 -m py_compile backend/proactive/observability_v2.py backend/proactive/eval_v2.py backend/proactive/runtime_v2.py backend/proactive/background_v2.py backend/perception/differ_v2.py backend/hosted/wake_consumer.py
git diff --check
```

Current local result:

- `tests/test_proactive_observability_v2.py`: 6 passed
- Observability/runtime/background/hosted regression: 46 passed
- Store/model-api/hosted smoke: 26 passed, 9 skipped locally because no
  `DATABASE_URL` is configured
- `py_compile` passed
- `git diff --check` passed

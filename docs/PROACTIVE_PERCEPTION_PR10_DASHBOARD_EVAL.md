# Proactive Perception Runtime V2 - PR10 Dashboard And Eval Surface

PR10 adds V2 runtime visibility to the proactive dashboard and moves the review
UI to Round 3 labels. It does not delete live legacy executors yet because all
three production cutover flags remain default-off and the observation window has
not happened.

## Dashboard Reads V2 Records

`/debug/proactive` now reads these Runtime V2 streams:

- `proactive_wakes_v2`
- `proactive_turns_v2`
- `proactive_turn_actions_v2`
- `proactive_background_jobs_v2`
- `proactive_scheduled_wakes_v2`
- `proactive_runtime_metrics_v2`
- `proactive_tool_traces_v2`

The page renders dedicated V2 sections for:

- runtime health metrics
- wakes and turns
- turn actions and tool traces
- background jobs and scheduled wakes

Legacy `gate_decisions` and `proactive_jobs` remain visible as compatibility
surfaces until flags have been enabled and observed.

## Review Labels

The dashboard review form now uses canonical Round 3 labels:

- `good_presence`
- `missed_moment`
- `went_dark`
- `too_much_buzz`
- `too_chatty`
- `wrong_voice`
- `ignored_manual`
- `stutter`
- `late_irrelevant`
- `privacy_bad`

The review endpoint still accepts old Gate labels for backward compatibility,
but records `label_family=legacy_gate` for those rows. New labels record
`label_family=round3`.

`tools/proactive_gate_eval.py` also treats Round 3 labels as primary while
keeping legacy Gate labels readable for old snapshots.

## Tool Trace Stream

PR10 defines `proactive_tool_traces_v2` and `DBToolTraceSinkV2`. Existing tool
executor behavior is unchanged; callers may opt into the DB sink by passing it
as `trace_sink`.

## Cleanup Boundary

No live executor or legacy projection is deleted in this PR. Deletion stays
blocked on the post-flag observation window required by invariant 13.

## Verification

```bash
python3 -m pytest tests/test_proactive_dashboard_v2.py tests/test_proactive_observability_v2.py tests/test_proactive_tool_executor_v2.py -q
python3 -m py_compile backend/proactive/dashboard.py backend/proactive/routes.py backend/proactive/tool_executor_v2.py
```

"""Synthetic episode eval scaffold for Proactive/Perception Runtime V2."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from proactive.observability_v2 import (
    InMemoryMetricsSinkV2,
    ProactiveHealthSnapshotV2,
    ProactiveMetricsAggregatorV2,
    ROUND3_REVIEW_LABELS_V2,
)
from proactive.runtime_v2 import RuntimeSpineV2, TurnRunnerV2, WakeEventV2


@dataclass(frozen=True)
class SyntheticWakeV2:
    source: str
    trigger: str
    at: float
    manual: bool = False
    latency_sensitive: bool = False
    change_digest: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SyntheticEpisodeV2:
    episode_id: str
    description: str
    wakes: tuple[SyntheticWakeV2, ...]
    agent_responses: tuple[Mapping[str, Any], ...]
    expected_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeReplayResultV2:
    episode_id: str
    labels: tuple[str, ...]
    turn_statuses: tuple[str, ...]
    health: ProactiveHealthSnapshotV2
    metric_events: tuple[Mapping[str, Any], ...]


def seed_synthetic_episodes_v2() -> tuple[SyntheticEpisodeV2, ...]:
    return (
        SyntheticEpisodeV2(
            episode_id="merged_anchor_unlock_good_presence",
            description="Anchor arrival and unlock collapse into one turn with one visible response.",
            wakes=(
                SyntheticWakeV2(
                    source="perception_event",
                    trigger="arrived_at_anchor",
                    at=100.0,
                    change_digest="anchor: home -> cafe",
                ),
                SyntheticWakeV2(
                    source="perception_event",
                    trigger="unlock_after_absence",
                    at=100.4,
                    change_digest="unlock_after_absence: false -> true",
                ),
            ),
            agent_responses=({"messages": ["你到了咖啡店呀。"]},),
            expected_labels=("good_presence",),
        ),
        SyntheticEpisodeV2(
            episode_id="manual_sleep_ignored_manual",
            description="Manual summon returning sleep is a contract violation.",
            wakes=(
                SyntheticWakeV2(
                    source="user_message",
                    trigger="user_message",
                    at=200.0,
                    manual=True,
                    latency_sensitive=True,
                ),
            ),
            agent_responses=({"actions": [{"type": "sleep", "reason": "not_now"}]},),
            expected_labels=("ignored_manual",),
        ),
    )


def replay_synthetic_episode_v2(
    episode: SyntheticEpisodeV2,
    *,
    user_id: str = "synthetic_user",
    merge_window_sec: float = 1.0,
) -> EpisodeReplayResultV2:
    metrics = InMemoryMetricsSinkV2()
    spine = RuntimeSpineV2(metrics_sink=metrics, merge_window_sec=merge_window_sec)
    responses = list(episode.agent_responses)

    def _run_agent(_context):
        if responses:
            return responses.pop(0)
        return {"actions": [{"type": "sleep", "reason": "no_scripted_response"}]}

    runner = TurnRunnerV2(spine, run_agent=_run_agent, metrics_sink=metrics)
    for wake in episode.wakes:
        spine.submit(
            WakeEventV2(
                user_id=user_id,
                source=wake.source,
                trigger=wake.trigger,
                created_at=wake.at,
                manual=wake.manual,
                latency_sensitive=wake.latency_sensitive,
                change_digest=wake.change_digest,
                payload=wake.payload,
            )
        )

    turn_statuses: list[str] = []
    labels: list[str] = []
    now = max((wake.at for wake in episode.wakes), default=0.0) + merge_window_sec + 0.1
    while True:
        result = runner.run_ready_turn(user_id, now=now)
        if result.status == "idle":
            break
        turn_statuses.append(result.status)
        labels.extend(_labels_for_turn_result(result))
        now += 0.1

    if len(turn_statuses) > 1:
        labels.append("stutter")
    for label in episode.expected_labels:
        if label not in labels:
            labels.append("missed_moment")
            break
    labels = tuple(label for label in _dedupe(labels) if label in ROUND3_REVIEW_LABELS_V2)
    health = ProactiveMetricsAggregatorV2().snapshot(metrics.events)
    return EpisodeReplayResultV2(
        episode_id=episode.episode_id,
        labels=labels,
        turn_statuses=tuple(turn_statuses),
        health=health,
        metric_events=tuple(event.to_doc() for event in metrics.events),
    )


def replay_seed_episodes_v2() -> tuple[EpisodeReplayResultV2, ...]:
    return tuple(replay_synthetic_episode_v2(episode) for episode in seed_synthetic_episodes_v2())


def _labels_for_turn_result(result: Any) -> list[str]:
    status = str(getattr(result, "status", "") or "")
    outcome = getattr(result, "outcome", None)
    if status == "ignored_manual":
        return ["ignored_manual"]
    messages = tuple(getattr(outcome, "messages", ()) or ()) if outcome is not None else ()
    if messages:
        return ["good_presence"]
    if status == "completed":
        return ["went_dark"]
    return []


def _dedupe(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


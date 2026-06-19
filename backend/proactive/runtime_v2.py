"""Proactive/perception V2 runtime spine.

The production routes still write legacy proactive jobs today. This module
defines the new center of gravity: wake events enter a per-user inbox, are
drained through a merge window, and become one merged turn context.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from proactive.tool_catalog_v2 import ToolCatalogV2, default_tool_catalog_v2

WAKE_SOURCES = {
    "user_message",
    "heartbeat",
    "perception_event",
    "scene_change",
    "scheduled_wake",
    "background_result",
}

PRIMARY_GROUP_ORDER = ("interactive", "event", "heartbeat")


def _new_wake_id() -> str:
    return "wake_" + uuid.uuid4().hex[:16]


def _new_lease_id() -> str:
    return "lease_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class WakeEventV2:
    user_id: str
    source: str
    trigger: str
    wake_id: str = field(default_factory=_new_wake_id)
    created_at: float = field(default_factory=time.time)
    latency_sensitive: bool = False
    manual: bool = False
    change_digest: str = ""
    presence_hints: Mapping[str, Any] = field(default_factory=dict)
    switches: Mapping[str, bool] = field(default_factory=dict)
    scheduled_note: str = ""
    origin_refs: tuple[str, ...] = ()
    background_payload: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in WAKE_SOURCES:
            raise ValueError(f"unknown wake source: {self.source}")

    @property
    def dedupe_key(self) -> str:
        if self.source == "user_message":
            return f"{self.source}:{self.wake_id}"
        return f"{self.source}:{self.trigger}"


@dataclass(frozen=True)
class MergedWakeContextV2:
    user_id: str
    trigger: str
    merged_triggers: tuple[str, ...]
    wake_ids: tuple[str, ...]
    latency_sensitive: bool
    manual: bool
    created_at: float
    change_digest: str
    presence_hints: Mapping[str, Any]
    switches: Mapping[str, bool]
    scheduled_note: str = ""
    origin_refs: tuple[str, ...] = ()
    background_payloads: tuple[Mapping[str, Any], ...] = ()
    tools: tuple[Mapping[str, Any], ...] = ()

    def as_turn_context(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "merged_triggers": list(self.merged_triggers),
            "latency_sensitive": self.latency_sensitive,
            "manual": self.manual,
            "time": self.created_at,
            "change_digest": self.change_digest,
            "presence_hints": dict(self.presence_hints or {}),
            "switches": dict(self.switches or {}),
            "scheduled_note": self.scheduled_note,
            "origin_refs": list(self.origin_refs),
            "background_payloads": [dict(item) for item in self.background_payloads],
            "wake_ids": list(self.wake_ids),
            "tools": [dict(item) for item in self.tools],
        }


def _primary_group(event: WakeEventV2) -> str:
    if event.latency_sensitive or event.source == "user_message":
        return "interactive"
    if event.source == "heartbeat":
        return "heartbeat"
    # TODO(Round 3 eval): scheduled_wake, perception_event, scene_change, and
    # background_result are all episode events here. Do not add a product
    # priority between them until reviewed wake episodes prove one.
    return "event"


def _wake_sort_key(event: WakeEventV2) -> tuple[int, float, str]:
    return (
        PRIMARY_GROUP_ORDER.index(_primary_group(event)),
        event.created_at,
        event.wake_id,
    )


def _unique_wakes_v2(wakes: list[WakeEventV2]) -> list[WakeEventV2]:
    out: list[WakeEventV2] = []
    seen: set[str] = set()
    for event in sorted(wakes, key=lambda item: (item.created_at, item.wake_id)):
        key = event.dedupe_key
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def merge_wakes_v2(
    wakes: list[WakeEventV2] | tuple[WakeEventV2, ...],
    *,
    tool_catalog: ToolCatalogV2 | None = None,
) -> MergedWakeContextV2:
    if not wakes:
        raise ValueError("cannot merge empty wake list")
    unique = _unique_wakes_v2(list(wakes))
    primary = sorted(unique, key=_wake_sort_key)[0]
    trigger = primary.trigger or primary.source
    merged = tuple(
        event.trigger or event.source
        for event in unique
        if event.wake_id != primary.wake_id
    )
    presence: dict[str, Any] = {}
    switches: dict[str, bool] = {}
    digest_parts: list[str] = []
    origin_refs: list[str] = []
    background_payloads: list[Mapping[str, Any]] = []
    scheduled_note = ""

    for event in unique:
        presence.update(dict(event.presence_hints or {}))
        switches.update(dict(event.switches or {}))
        if event.change_digest:
            digest_parts.append(event.change_digest)
        if event.scheduled_note and not scheduled_note:
            scheduled_note = event.scheduled_note
        for ref in event.origin_refs:
            if ref not in origin_refs:
                origin_refs.append(ref)
        if event.background_payload:
            background_payloads.append(event.background_payload)

    catalog = tool_catalog or default_tool_catalog_v2()
    return MergedWakeContextV2(
        user_id=primary.user_id,
        trigger=trigger,
        merged_triggers=merged,
        wake_ids=tuple(event.wake_id for event in unique),
        latency_sensitive=any(event.latency_sensitive for event in unique),
        manual=any(event.manual for event in unique),
        created_at=min(event.created_at for event in unique),
        change_digest="; ".join(digest_parts),
        presence_hints=presence,
        switches=switches,
        scheduled_note=scheduled_note,
        origin_refs=tuple(origin_refs),
        background_payloads=tuple(background_payloads),
        tools=tuple(catalog.context_tools()),
    )


class WakeInboxV2:
    """In-memory per-user wake inbox for the v2 contract tests.

    Production can swap this for PostgreSQL or a per-user actor queue without
    changing the merge contract.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, list[WakeEventV2]] = defaultdict(list)

    def push(self, event: WakeEventV2) -> None:
        with self._lock:
            self._items[event.user_id].append(event)
            self._items[event.user_id].sort(key=lambda item: (item.created_at, item.wake_id))

    def drain_ready(
        self,
        user_id: str,
        *,
        now: float | None = None,
        merge_window_sec: float = 2.0,
    ) -> list[WakeEventV2]:
        now = time.time() if now is None else float(now)
        with self._lock:
            items = list(self._items.get(user_id) or [])
            if not items:
                return []
            latency_indices = [idx for idx, event in enumerate(items) if event.latency_sensitive]
            if latency_indices:
                end = latency_indices[0] + 1
            else:
                first_ts = items[0].created_at
                if now - first_ts < merge_window_sec:
                    return []
                end = 0
                cutoff = first_ts + merge_window_sec
                while end < len(items) and items[end].created_at <= cutoff:
                    end += 1
            drained = items[:end]
            remaining = items[end:]
            if remaining:
                self._items[user_id] = remaining
            else:
                self._items.pop(user_id, None)
            return drained


class SingleFlightRegistryV2:
    """Per-user lock registry for turn execution."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, user_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[user_id] = lock
            return lock

    def try_acquire(self, user_id: str) -> threading.Lock | None:
        lock = self.lock_for(user_id)
        if not lock.acquire(blocking=False):
            return None
        return lock


@dataclass(frozen=True)
class LeaseV2:
    scope: str
    owner_id: str
    lease_id: str = field(default_factory=_new_lease_id)
    acquired_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    user_id: str = ""

    def expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else float(now)
        return self.expires_at <= now


class LeaseRegistryV2:
    """In-memory lease registry for contract tests.

    Production should replace this with a DB-backed CAS/advisory-lock lease.
    The semantics are fixed here: active leases block, expired leases are
    reclaimed by the next acquirer, and only the current lease holder can
    release a scope.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, LeaseV2] = {}

    def try_acquire(
        self,
        scope: str,
        *,
        owner_id: str,
        now: float | None = None,
        ttl_sec: float,
        user_id: str = "",
    ) -> LeaseV2 | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            current = self._leases.get(scope)
            if current is not None and not current.expired(now):
                return None
            lease = LeaseV2(
                scope=scope,
                owner_id=str(owner_id or "unknown"),
                acquired_at=now,
                expires_at=now + float(ttl_sec),
                user_id=str(user_id or ""),
            )
            self._leases[scope] = lease
            return lease

    def release(self, lease: LeaseV2) -> bool:
        with self._lock:
            current = self._leases.get(lease.scope)
            if current is None or current.lease_id != lease.lease_id:
                return False
            self._leases.pop(lease.scope, None)
            return True

    def current(self, scope: str, *, now: float | None = None) -> LeaseV2 | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            current = self._leases.get(scope)
            if current is not None and current.expired(now):
                self._leases.pop(scope, None)
                return None
            return current


class TurnLeaseRegistryV2(LeaseRegistryV2):
    def try_acquire_user(
        self,
        user_id: str,
        *,
        owner_id: str,
        now: float | None = None,
        ttl_sec: float,
    ) -> LeaseV2 | None:
        return self.try_acquire(
            f"turn:{user_id}",
            owner_id=owner_id,
            now=now,
            ttl_sec=ttl_sec,
            user_id=user_id,
        )


class BackgroundLeaseRegistryV2(LeaseRegistryV2):
    def try_acquire_job(
        self,
        job_id: str,
        *,
        user_id: str = "",
        owner_id: str,
        now: float | None = None,
        ttl_sec: float,
    ) -> LeaseV2 | None:
        return self.try_acquire(
            f"background:{job_id}",
            owner_id=owner_id,
            now=now,
            ttl_sec=ttl_sec,
            user_id=user_id,
        )


@dataclass(frozen=True)
class TurnOutcomeV2:
    messages: tuple[str, ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()
    needs_background: bool = False
    background_request: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnRunResultV2:
    status: str
    context: MergedWakeContextV2 | None = None
    outcome: TurnOutcomeV2 | None = None
    turn_lease: LeaseV2 | None = None
    background_job_id: str = ""
    background_lease: LeaseV2 | None = None


class RuntimeSpineV2:
    """Small facade for the new flow: submit wake, drain merged context."""

    def __init__(
        self,
        *,
        inbox: WakeInboxV2 | None = None,
        tool_catalog: ToolCatalogV2 | None = None,
        merge_window_sec: float = 2.0,
    ) -> None:
        self.inbox = inbox or WakeInboxV2()
        self.tool_catalog = tool_catalog or default_tool_catalog_v2()
        self.merge_window_sec = merge_window_sec

    def submit(self, event: WakeEventV2) -> None:
        self.inbox.push(event)

    def drain_context(self, user_id: str, *, now: float | None = None) -> MergedWakeContextV2 | None:
        wakes = self.inbox.drain_ready(
            user_id,
            now=now,
            merge_window_sec=self.merge_window_sec,
        )
        if not wakes:
            return None
        return merge_wakes_v2(wakes, tool_catalog=self.tool_catalog)


def _sleep_outcome_v2(_context: MergedWakeContextV2) -> TurnOutcomeV2:
    return TurnOutcomeV2(actions=({"type": "sleep"},))


class TurnRunnerV2:
    """Single-flight turn executor shell.

    This does not call production LLMs yet. It fixes the runtime contract:
    foreground turns hold a reclaimable per-user lease; background work has its
    own lease; foreground leases are released before background results re-enter
    the inbox as `background_result` wakes.
    """

    def __init__(
        self,
        spine: RuntimeSpineV2,
        *,
        run_agent: Callable[[MergedWakeContextV2], TurnOutcomeV2] | None = None,
        turn_leases: TurnLeaseRegistryV2 | None = None,
        background_leases: BackgroundLeaseRegistryV2 | None = None,
        turn_lease_ttl_sec: float = 120.0,
        background_lease_ttl_sec: float = 600.0,
        owner_id: str = "turn_runner_v2",
    ) -> None:
        self.spine = spine
        self.run_agent = run_agent or _sleep_outcome_v2
        self.turn_leases = turn_leases or TurnLeaseRegistryV2()
        self.background_leases = background_leases or BackgroundLeaseRegistryV2()
        self.turn_lease_ttl_sec = float(turn_lease_ttl_sec)
        self.background_lease_ttl_sec = float(background_lease_ttl_sec)
        self.owner_id = owner_id

    def run_ready_turn(
        self,
        user_id: str,
        *,
        now: float | None = None,
        owner_id: str | None = None,
    ) -> TurnRunResultV2:
        now = time.time() if now is None else float(now)
        owner = owner_id or self.owner_id
        turn_lease = self.turn_leases.try_acquire_user(
            user_id,
            owner_id=owner,
            now=now,
            ttl_sec=self.turn_lease_ttl_sec,
        )
        if turn_lease is None:
            return TurnRunResultV2(status="busy")
        try:
            context = self.spine.drain_context(user_id, now=now)
            if context is None:
                return TurnRunResultV2(status="idle", turn_lease=turn_lease)
            outcome = self.run_agent(context)
            if outcome.needs_background:
                background_job_id = "bg_" + uuid.uuid4().hex[:16]
                background_lease = self.background_leases.try_acquire_job(
                    background_job_id,
                    user_id=user_id,
                    owner_id=owner,
                    now=now,
                    ttl_sec=self.background_lease_ttl_sec,
                )
                return TurnRunResultV2(
                    status="background_queued",
                    context=context,
                    outcome=outcome,
                    turn_lease=turn_lease,
                    background_job_id=background_job_id,
                    background_lease=background_lease,
                )
            return TurnRunResultV2(
                status="completed",
                context=context,
                outcome=outcome,
                turn_lease=turn_lease,
            )
        finally:
            self.turn_leases.release(turn_lease)

    def submit_background_result(
        self,
        user_id: str,
        payload: Mapping[str, Any],
        *,
        origin_refs: tuple[str, ...] = (),
        now: float | None = None,
    ) -> WakeEventV2:
        event = WakeEventV2(
            user_id=user_id,
            source="background_result",
            trigger="background_result",
            created_at=time.time() if now is None else float(now),
            origin_refs=origin_refs,
            background_payload=payload,
        )
        self.spine.submit(event)
        return event

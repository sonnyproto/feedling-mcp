"""Executable contract for Proactive/Perception V2 tools.

PR3 keeps this layer independent from hosted/resident cutover. Callers can
inject output adapters for side-effecting actions while the shared catalog,
budgeting, unavailable-tool behavior, and traces stay identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

import db
from proactive.tool_catalog_v2 import FAST, SLOW, CostClass, ToolCatalogV2, default_tool_catalog_v2

TOOL_TRACE_STREAM_V2 = "proactive_tool_traces_v2"

PR3_UNIMPLEMENTED_TOOLS_V2 = frozenset({"schedule_wake", "cancel_wake"})  # screen.read/screen.recent now implemented in _execute_available


def _new_tool_call_id() -> str:
    return "tool_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class ToolCallV2:
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    user_id: str = ""
    wake_id: str = ""
    turn_id: str = ""
    call_id: str = field(default_factory=_new_tool_call_id)


@dataclass(frozen=True)
class ToolTraceV2:
    call_id: str
    name: str
    cost_class: CostClass
    outcome: str
    latency_ms: float
    wake_id: str = ""
    turn_id: str = ""
    user_id: str = ""
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "cost_class": self.cost_class,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            "wake_id": self.wake_id,
            "turn_id": self.turn_id,
            "user_id": self.user_id,
            "error_code": self.error_code,
        }


class DBToolTraceSinkV2:
    def __call__(self, trace: ToolTraceV2) -> None:
        doc = {
            "kind": "tool_trace_v2",
            **trace.as_dict(),
            "ts": time.time(),
        }
        db.log_append(
            trace.user_id,
            TOOL_TRACE_STREAM_V2,
            doc,
            ts=doc["ts"],
            item_key=trace.call_id,
        )


@dataclass(frozen=True)
class ToolResultV2:
    ok: bool
    outcome: str
    result: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    needs_background: bool = False
    trace: ToolTraceV2 | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outcome": self.outcome,
            "result": dict(self.result or {}),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "needs_background": self.needs_background,
            "trace": self.trace.as_dict() if self.trace else None,
        }


@dataclass(frozen=True)
class ToolBudgetV2:
    fast_hard_limit: int = 6
    slow_inline_limit: int = 1
    background_hard_limit: int = 25
    background: bool = False


@dataclass
class ToolBudgetStateV2:
    fast_calls: int = 0
    slow_calls: int = 0
    background_calls: int = 0

    def note(self, cost_class: CostClass, *, background: bool = False) -> None:
        if background:
            self.background_calls += 1
        elif cost_class == SLOW:
            self.slow_calls += 1
        else:
            self.fast_calls += 1


@dataclass(frozen=True)
class ToolRuntimeAdaptersV2:
    perception_snapshot: Callable[[str], Mapping[str, Any]] | None = None
    perception_pull_snapshot: Callable[[str], Mapping[str, Any]] | None = None
    photos_recent: Callable[[str, int], Mapping[str, Any]] | None = None
    memory_load: Callable[[str], Sequence[Mapping[str, Any]]] | None = None
    send_message: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    screen_read: Callable[[str, str | None, str], Mapping[str, Any]] | None = None
    screen_recent: Callable[[str, int], Mapping[str, Any]] | None = None


def default_tool_runtime_adapters_v2() -> ToolRuntimeAdaptersV2:
    def perception_snapshot(user_id: str) -> Mapping[str, Any]:
        from perception import service as perception_service

        return perception_service.snapshot(user_id)

    def perception_pull_snapshot(user_id: str) -> Mapping[str, Any]:
        from perception import service as perception_service

        return perception_service.pull_snapshot(user_id)

    def photos_recent(user_id: str, limit: int) -> Mapping[str, Any]:
        from perception import service as perception_service

        body, code = perception_service.photos_recent(user_id, limit=limit)
        return {"status_code": code, **dict(body or {})}

    def memory_load(user_id: str) -> Sequence[Mapping[str, Any]]:
        import db

        return db.memory_load(user_id)

    return ToolRuntimeAdaptersV2(
        perception_snapshot=perception_snapshot,
        perception_pull_snapshot=perception_pull_snapshot,
        photos_recent=photos_recent,
        memory_load=memory_load,
    )


def screen_runtime_adapters_v2(api_key: str, store) -> ToolRuntimeAdaptersV2:
    """Screen adapters bound to the per-turn api_key (needed to reach the
    enclave) and gated by the fail-closed screen_caption_enabled flag."""
    from screen import caption as screen_caption
    from proactive.screen_flag_v2 import screen_caption_enabled

    def screen_read(user_id: str, frame_id: str | None, mode: str) -> Mapping[str, Any]:
        if not screen_caption_enabled(store):
            raise ToolUnavailableV2("screen_caption_disabled", "screen captioning is off for this user")
        return screen_caption.caption_frame(user_id, api_key, frame_id, mode)

    def screen_recent(user_id: str, limit: int) -> Mapping[str, Any]:
        return screen_caption.recent_frames(user_id, limit)

    return ToolRuntimeAdaptersV2(screen_read=screen_read, screen_recent=screen_recent)


def combined_runtime_adapters_v2(api_key: str, store) -> ToolRuntimeAdaptersV2:
    """Default perception/memory adapters + screen adapters bound to this turn's
    api_key/store, so the executor can reach every implemented tool."""
    import dataclasses
    base = default_tool_runtime_adapters_v2()
    screen = screen_runtime_adapters_v2(api_key, store)
    return dataclasses.replace(base, screen_read=screen.screen_read,
                               screen_recent=screen.screen_recent)


class ToolExecutorV2:
    def __init__(
        self,
        *,
        catalog: ToolCatalogV2 | None = None,
        adapters: ToolRuntimeAdaptersV2 | None = None,
        budget: ToolBudgetV2 | None = None,
        trace_sink: Callable[[ToolTraceV2], None] | None = None,
    ) -> None:
        self.catalog = catalog or default_tool_catalog_v2()
        self.adapters = adapters or default_tool_runtime_adapters_v2()
        self.budget = budget or ToolBudgetV2()
        self.budget_state = ToolBudgetStateV2()
        self.trace_sink = trace_sink
        self.traces: list[ToolTraceV2] = []

    def execute(self, call: ToolCallV2) -> ToolResultV2:
        started = time.perf_counter()
        args = dict(call.args or {})
        try:
            cost_class = self.catalog.cost_class_for(call.name, args)
        except KeyError:
            return self._finish(
                call,
                FAST,
                started,
                ok=False,
                outcome="error",
                error_code="unknown_tool",
                error_message=f"unknown tool: {call.name}",
            )

        if call.name in PR3_UNIMPLEMENTED_TOOLS_V2:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="unavailable",
                error_code="tool_not_implemented_in_pr3",
                error_message=f"{call.name} is cataloged but not implemented in PR3.",
            )
        dynamic_unavailable = self._dynamic_unavailable(call, args)
        if dynamic_unavailable:
            code, message = dynamic_unavailable
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="unavailable",
                error_code=code,
                error_message=message,
            )

        handoff = self._budget_handoff_reason(cost_class)
        if handoff:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="needs_background",
                error_code=handoff,
                error_message="tool budget reached; hand off to background",
                needs_background=True,
            )

        try:
            result = self._execute_available(call, args)
        except ToolUnavailableV2 as e:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="unavailable",
                error_code=e.code,
                error_message=e.message,
            )
        except Exception as e:
            return self._finish(
                call,
                cost_class,
                started,
                ok=False,
                outcome="error",
                error_code=type(e).__name__,
                error_message=str(e)[:240],
            )

        self.budget_state.note(cost_class, background=self.budget.background)
        return self._finish(call, cost_class, started, ok=True, outcome="ok", result=result)

    def _dynamic_unavailable(self, call: ToolCallV2, args: Mapping[str, Any]) -> tuple[str, str] | None:
        if call.name in {
            "perception.now",
            "perception.location",
            "perception.calendar",
            "perception.now_playing",
            "perception.motion",
        } and not self.adapters.perception_snapshot:
            return ("perception_snapshot_adapter_missing", "perception snapshot adapter is not configured")
        if call.name in {
            "perception.audio_route",
            "perception.weather",
            "perception.steps",
            "perception.sleep_last_night",
            "perception.workout",
            "perception.vitals",
        } and not (self.adapters.perception_pull_snapshot or self.adapters.perception_snapshot):
            return ("perception_snapshot_adapter_missing", "perception snapshot adapter is not configured")
        if call.name == "perception.photo_recent" and not self.adapters.photos_recent:
            return ("photo_recent_adapter_missing", "photo recent adapter is not configured")
        if call.name in {"memory.index", "memory.fetch"} and not self.adapters.memory_load:
            return ("memory_adapter_missing", "memory adapter is not configured")
        if call.name == "memory.fetch" and not _string_list(args.get("ids") or args.get("id")):
            return ("memory_ids_required", "memory.fetch requires one or more ids")
        if call.name == "send_message":
            if not self.adapters.send_message:
                return ("send_message_adapter_missing", "send_message requires a hosted/resident output adapter")
            if not str(args.get("text") or "").strip():
                return ("send_message_text_required", "send_message requires non-empty text")
        if call.name == "screen.read" and not self.adapters.screen_read:
            return ("screen_adapter_missing", "screen.read requires a screen runtime adapter")
        if call.name == "screen.recent" and not self.adapters.screen_recent:
            return ("screen_adapter_missing", "screen.recent requires a screen runtime adapter")
        return None

    def _budget_handoff_reason(self, cost_class: CostClass) -> str:
        if self.budget.background:
            return (
                "background_budget_soft_handoff"
                if self.budget_state.background_calls >= self.budget.background_hard_limit
                else ""
            )
        if cost_class == SLOW and self.budget_state.slow_calls >= self.budget.slow_inline_limit:
            return "slow_budget_soft_handoff"
        if cost_class == FAST and self.budget_state.fast_calls >= self.budget.fast_hard_limit:
            return "fast_budget_soft_handoff"
        return ""

    def _execute_available(self, call: ToolCallV2, args: Mapping[str, Any]) -> Mapping[str, Any]:
        if call.name == "perception.now":
            return {"snapshot": dict(self._snapshot(call.user_id))}
        if call.name == "perception.location":
            snap = self._snapshot(call.user_id)
            return {"location": {
                "place_label": snap.get("place_label"),
                "wifi_label": snap.get("wifi_label"),
                "country": snap.get("country"),
                "wifi_anchor_id": snap.get("wifi_anchor_id"),
            }}
        if call.name == "perception.calendar":
            snap = self._snapshot(call.user_id)
            return {
                "window_days": args.get("window_days", args.get("days", 1)),
                "calendar_next_event": snap.get("calendar_next_event"),
            }
        if call.name == "perception.now_playing":
            return {"now_playing": self._snapshot(call.user_id).get("now_playing")}
        if call.name == "perception.motion":
            return {"motion_state": self._snapshot(call.user_id).get("motion_state")}
        if call.name == "perception.audio_route":
            state = self._pull_snapshot(call.user_id)
            return {"audio_route": {
                "output_type": state.get("output_type"),
                "is_bluetooth": state.get("is_bluetooth"),
                "device_name": state.get("device_name"),
            }}
        if call.name == "perception.weather":
            state = self._pull_snapshot(call.user_id)
            return {"weather": {
                "condition": state.get("condition"),
                "temperature_bucket": state.get("temperature_bucket"),
                "is_daylight": state.get("is_daylight"),
            }}
        if call.name == "perception.steps":
            return {"steps": {"step_count_bucket": self._pull_snapshot(call.user_id).get("step_count_bucket")}}
        if call.name == "perception.sleep_last_night":
            return {"sleep_last_night": {
                "asleep_minutes_bucket": self._pull_snapshot(call.user_id).get("asleep_minutes_bucket"),
            }}
        if call.name == "perception.workout":
            state = self._pull_snapshot(call.user_id)
            return {"workout": {
                "workout_type": state.get("workout_type"),
                "duration_min_bucket": state.get("duration_min_bucket"),
                "count_today": state.get("count_today"),
            }}
        if call.name == "perception.vitals":
            state = self._pull_snapshot(call.user_id)
            return {"vitals": {
                "resting_heart_rate_bucket": state.get("resting_heart_rate_bucket"),
                "step_count_bucket": state.get("step_count_bucket"),
            }}
        if call.name == "perception.photo_recent":
            limit = _int_arg(args.get("limit"), default=10, lo=1, hi=50)
            assert self.adapters.photos_recent is not None
            return dict(self.adapters.photos_recent(call.user_id, limit))
        if call.name == "memory.index":
            return {"memories": [_memory_index_item(memory) for memory in self._memories(call.user_id)]}
        if call.name == "memory.fetch":
            ids = _string_list(args.get("ids") or args.get("id"))
            by_id = {str(memory.get("id") or ""): dict(memory) for memory in self._memories(call.user_id)}
            return {"memories": [by_id[item] for item in ids if item in by_id], "missing_ids": [item for item in ids if item not in by_id]}
        if call.name == "send_message":
            text = str(args.get("text") or "").strip()
            assert self.adapters.send_message is not None
            return dict(self.adapters.send_message(call.user_id, text, args))
        if call.name == "sleep":
            return {"sleep": True, "reason": str(args.get("reason") or "")[:240]}
        if call.name == "screen.read":
            assert self.adapters.screen_read is not None
            mode = str(args.get("mode") or "caption").lower()
            frame_id = args.get("frame_id")
            res = dict(self.adapters.screen_read(call.user_id, frame_id, mode))
            if res.get("error"):
                raise ToolUnavailableV2(str(res["error"]), f"screen.read: {res['error']}")
            return res
        if call.name == "screen.recent":
            assert self.adapters.screen_recent is not None
            limit = _int_arg(args.get("limit"), default=10, lo=1, hi=50)
            return dict(self.adapters.screen_recent(call.user_id, limit))
        raise ToolUnavailableV2("tool_not_implemented_in_pr3", f"{call.name} is cataloged but not implemented in PR3")

    def _snapshot(self, user_id: str) -> Mapping[str, Any]:
        if not self.adapters.perception_snapshot:
            raise ToolUnavailableV2("perception_snapshot_adapter_missing", "perception snapshot adapter is not configured")
        return self.adapters.perception_snapshot(user_id)

    def _pull_snapshot(self, user_id: str) -> Mapping[str, Any]:
        if self.adapters.perception_pull_snapshot:
            return self.adapters.perception_pull_snapshot(user_id)
        return self._snapshot(user_id)

    def _memories(self, user_id: str) -> Sequence[Mapping[str, Any]]:
        if not self.adapters.memory_load:
            raise ToolUnavailableV2("memory_adapter_missing", "memory adapter is not configured")
        return self.adapters.memory_load(user_id)

    def _finish(
        self,
        call: ToolCallV2,
        cost_class: CostClass,
        started: float,
        *,
        ok: bool,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        needs_background: bool = False,
    ) -> ToolResultV2:
        trace = ToolTraceV2(
            call_id=call.call_id,
            name=call.name,
            cost_class=cost_class,
            outcome=outcome,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000.0),
            wake_id=call.wake_id,
            turn_id=call.turn_id,
            user_id=call.user_id,
            error_code=error_code,
        )
        self.traces.append(trace)
        if self.trace_sink:
            self.trace_sink(trace)
        return ToolResultV2(
            ok=ok,
            outcome=outcome,
            result=dict(result or {}),
            error_code=error_code,
            error_message=error_message,
            needs_background=needs_background,
            trace=trace,
        )


class ToolUnavailableV2(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _int_arg(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if str(item)]
    return []


def _memory_index_item(memory: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(memory.get("id") or ""),
        "type": str(memory.get("type") or "")[:80],
        "title": str(memory.get("title") or "")[:160],
        "occurred_at": str(memory.get("occurred_at") or ""),
        "updated_at": str(memory.get("updated_at") or ""),
        "is_archived": bool(memory.get("is_archived")),
    }

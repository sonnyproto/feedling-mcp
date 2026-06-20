"""Proactive/perception V2 tool catalog.

This is the runtime-facing catalog, not an HTTP router. The first migration
step is to make the new contract explicit: every turn sees the same tool names
and cost classes, even while the old hosted/resident paths still use adapters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

CostClass = str
FAST: CostClass = "fast"
SLOW: CostClass = "slow"


@dataclass(frozen=True)
class ToolSpecV2:
    name: str
    group: str
    cost_class: CostClass
    description: str = ""
    wake_source: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_context(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "cost_class": self.cost_class,
            "description": self.description,
            "wake_source": self.wake_source,
            "metadata": dict(self.metadata or {}),
        }


class ToolCatalogV2:
    def __init__(self, specs: list[ToolSpecV2] | tuple[ToolSpecV2, ...]):
        self._specs = tuple(specs)
        self._by_name = {spec.name: spec for spec in self._specs}

    def specs(self) -> tuple[ToolSpecV2, ...]:
        return self._specs

    def context_tools(self) -> list[dict[str, Any]]:
        return [spec.as_context() for spec in self._specs]

    def signature(self) -> tuple[tuple[str, str, CostClass], ...]:
        return tuple((spec.name, spec.group, spec.cost_class) for spec in self._specs)

    def get(self, name: str) -> ToolSpecV2:
        return self._by_name[name]

    def cost_class_for(self, name: str, args: Mapping[str, Any] | None = None) -> CostClass:
        """Return the effective cost class.

        Most tools are static. A few have spec-approved parameter thresholds;
        keep that logic here so agents do not infer pricing themselves.
        """
        args = args or {}
        if name == "perception.calendar":
            try:
                window_days = float(args.get("window_days", args.get("days", 1)))
            except (TypeError, ValueError):
                window_days = 1
            return FAST if window_days <= 7 else SLOW
        if name == "screen.read":
            return SLOW if str(args.get("mode") or "caption").lower() == "full" else FAST
        return self.get(name).cost_class


DEFAULT_TOOL_SPECS_V2: tuple[ToolSpecV2, ...] = (
    ToolSpecV2("perception.now", "perception", FAST, "Current cheap authorized signals."),
    ToolSpecV2("perception.location", "perception", FAST, "Connectivity-derived coarse presence label."),
    ToolSpecV2("perception.calendar", "perception", FAST, "Calendar window; >7 days is slow."),
    ToolSpecV2("perception.now_playing", "perception", FAST, "Current media playback."),
    ToolSpecV2("perception.motion", "perception", FAST, "Motion is pull-only; it is not a wake source."),
    ToolSpecV2("perception.audio_route", "perception", FAST, "Current coarse audio output route reported by iOS."),
    ToolSpecV2("perception.weather", "perception", FAST, "Coarse WeatherKit context reported by iOS."),
    ToolSpecV2("perception.steps", "perception", SLOW, "HealthKit-backed step-count bucket reported by iOS."),
    ToolSpecV2("perception.sleep_last_night", "perception", SLOW, "HealthKit-backed sleep trend."),
    ToolSpecV2("perception.workout", "perception", SLOW, "HealthKit-backed workout trend."),
    ToolSpecV2("perception.vitals", "perception", SLOW, "HealthKit-backed vitals trend."),
    ToolSpecV2("perception.photo_recent", "perception", SLOW, "Recent photo metadata and pullable content."),
    ToolSpecV2("screen.read", "screen", FAST, "Caption by default; mode=full is slow."),
    ToolSpecV2("screen.recent", "screen", SLOW, "Recent screen frames."),
    ToolSpecV2("memory.index", "memory", FAST, "Compact memory index."),
    ToolSpecV2("memory.fetch", "memory", SLOW, "Verbatim memory fetch."),
    ToolSpecV2("send_message", "action", FAST, "Write chat message through DeliveryGate."),
    ToolSpecV2("sleep", "action", FAST, "End turn without visible speech."),
    ToolSpecV2("schedule_wake", "action", FAST, "Create a durable future wake."),
    ToolSpecV2("cancel_wake", "action", FAST, "Cancel a durable future wake."),
)


def default_tool_catalog_v2() -> ToolCatalogV2:
    return ToolCatalogV2(DEFAULT_TOOL_SPECS_V2)


def tool_catalog_v2_for_runtime(runtime: str) -> ToolCatalogV2:
    """Return the shared V2 catalog for hosted/resident runtime surfaces."""
    normalized = str(runtime or "").strip().lower()
    if normalized not in {"hosted", "resident"}:
        raise ValueError(f"unknown v2 runtime surface: {runtime}")
    return default_tool_catalog_v2()


def tool_context_v2_for_runtime(runtime: str) -> list[dict[str, Any]]:
    return tool_catalog_v2_for_runtime(runtime).context_tools()

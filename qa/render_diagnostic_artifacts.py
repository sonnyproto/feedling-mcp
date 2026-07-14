#!/usr/bin/env python3
"""Render deterministic operator views for a local diagnostic qualification.

Unlike the protected release renderer, this renderer accepts a locked subset of
profiles and incomplete diagnostic evidence.  Missing evidence is rendered as
missing; it is never upgraded into a release-qualified success.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

try:
    from qa.orchestration_contract import PROFILE_IDS
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from orchestration_contract import PROFILE_IDS


SCENARIO_IDS = tuple(f"P0-{index:02d}" for index in range(1, 14))
TRACE_STAGES = ("routing", "queue", "provider", "persistence", "delivery")
TERMINAL_STATUSES = frozenset(
    (
        "PASS",
        "PRODUCT_FAIL",
        "BLOCKED_CREDENTIAL",
        "BLOCKED_EVIDENCE",
        "BLOCKED_DEPLOYMENT",
        "AGENT_ERROR",
        "SECURITY_FAIL",
    )
)
FAILURE_STATUSES = frozenset(("PRODUCT_FAIL", "SECURITY_FAIL"))
COT_JUNIT_FAILURE = "COT_DELIVERY_FAIL"
COT_DELIVERY_STATUSES = frozenset(("PASS", "FAIL", "UNVERIFIED", "NOT_RUN"))


class DiagnosticRenderError(RuntimeError):
    """Fixed diagnostic safe to return to a local operator."""


def _status(value: Any, *, missing: str = "MISSING") -> str:
    return value if value in TERMINAL_STATUSES else missing


def _profile(
    profile_results: Mapping[str, Mapping[str, Any]], profile_id: str
) -> Mapping[str, Any] | None:
    value = profile_results.get(profile_id)
    return value if isinstance(value, Mapping) else None


def _scenario_statuses(profile: Mapping[str, Any] | None) -> dict[str, str]:
    statuses: dict[str, str] = {}
    rows = profile.get("scenarios") if profile else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            scenario_id = row.get("scenario_id")
            if scenario_id in SCENARIO_IDS and scenario_id not in statuses:
                statuses[str(scenario_id)] = _status(row.get("status"))
    return statuses


def _observed_runtime(profile: Mapping[str, Any] | None) -> str:
    if profile and profile.get("observed_runtime") in (
        "hosted_resident",
        "resident_cli",
    ):
        return str(profile["observed_runtime"])
    return "UNVERIFIED"


def _boolean_evidence(reasoning: Any, field: str) -> str:
    if not isinstance(reasoning, Mapping) or not isinstance(
        reasoning.get(field), bool
    ):
        return "UNVERIFIED"
    return "PRESENT" if reasoning[field] else "ABSENT"


def _reasoning_event(reasoning: Any) -> str:
    if not isinstance(reasoning, Mapping):
        return "UNVERIFIED"
    count = reasoning.get("reasoning_event_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return "UNVERIFIED"
    return "OBSERVED" if count > 0 else "NOT_OBSERVED"


def _cot_row(summary: Mapping[str, Any], profile_id: str) -> Mapping[str, Any] | None:
    matrix = summary.get("cot_delivery")
    if not isinstance(matrix, Mapping):
        return None
    row = matrix.get(profile_id)
    return row if isinstance(row, Mapping) else None


def _cot_token_evidence(cot: Mapping[str, Any] | None) -> str:
    if cot is None:
        return "UNVERIFIED"
    return "PRESENT" if cot.get("token_metadata_status") == "PRESENT" else "UNVERIFIED"


def _junit_scenario_status(
    summary: Mapping[str, Any],
    profile_id: str,
    scenario_id: str,
    scenario_statuses: Mapping[str, str],
) -> str:
    if scenario_id == "P0-12":
        cot = _cot_row(summary, profile_id)
        if cot is not None and cot.get("status") != "PASS":
            return COT_JUNIT_FAILURE
    return scenario_statuses.get(scenario_id, "MISSING")


def _cot_junit_message(summary: Mapping[str, Any], profile_id: str) -> str:
    cot = _cot_row(summary, profile_id)
    status = cot.get("status") if cot is not None else None
    normalized = status if status in COT_DELIVERY_STATUSES else "INVALID"
    return f"trusted-cot-delivery:{normalized}"


def _safe_number(value: Any, *, integer: bool = False) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if not math.isfinite(value) or value < 0 or (integer and not isinstance(value, int)):
        return ""
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def render_matrix(
    summary: Mapping[str, Any],
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
) -> str:
    """Return a fixed-field Markdown view of the selected diagnostic matrix."""

    harness = summary.get("qualification_harness")
    if not isinstance(harness, Mapping):
        harness = {}
    lines = [
        "# Feedling local API-key diagnostic",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Candidate SHA: `{summary['candidate_sha']}`",
        f"- Harness Git HEAD: `{harness.get('git_head', 'UNAVAILABLE')}`",
        f"- Harness source SHA-256: `{harness.get('source_sha256', 'UNAVAILABLE')}`",
        f"- Worker snapshot SHA-256: `{harness.get('worker_snapshot_sha256', 'UNAVAILABLE')}`",
        f"- Harness dirty: `{str(harness.get('dirty', 'UNAVAILABLE')).lower()}`",
        f"- Diagnostic status: `{summary['status']}`",
        "- Release qualified: `false`",
        f"- Strict evidence gaps: `{len(summary.get('missing_strict_evidence', []))}`",
        "",
    ]
    header = (
        "Profile",
        "Status",
        "Runtime",
        "COT delivery",
        "COT code",
        "COT observation",
        "COT observation code",
        "Reasoning event",
        "Reasoning metadata",
        "Reasoning tokens",
        "User disclosure",
        *(f"Agent {scenario_id}" for scenario_id in SCENARIO_IDS),
    )
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for profile_id in profile_ids:
        profile = _profile(profile_results, profile_id)
        cot = _cot_row(summary, profile_id)
        scenario_statuses = _scenario_statuses(profile)
        reasoning = profile.get("reasoning") if profile else None
        profile_status = (
            _status(profile.get("status"), missing="AGENT_ERROR")
            if profile
            else "NOT_RUN"
        )
        row = (
            profile_id,
            profile_status,
            _observed_runtime(profile),
            str(cot.get("status") or "UNVERIFIED") if cot else "UNVERIFIED",
            str(cot.get("failure_code") or "UNVERIFIED") if cot else "UNVERIFIED",
            str(cot.get("receipt_status") or "UNVERIFIED") if cot else "UNVERIFIED",
            (
                str(cot.get("receipt_failure_code") or "UNVERIFIED")
                if cot
                else "UNVERIFIED"
            ),
            (
                _reasoning_event(cot)
                if cot is not None
                else _reasoning_event(reasoning)
            ),
            (
                _boolean_evidence(cot, "metadata_present")
                if cot is not None
                else _boolean_evidence(reasoning, "metadata_present")
            ),
            _cot_token_evidence(cot),
            (
                _boolean_evidence(cot, "user_visible_disclosure_present")
                if cot is not None
                else _boolean_evidence(reasoning, "user_visible_disclosure_present")
            ),
            *(scenario_statuses.get(scenario_id, "MISSING") for scenario_id in SCENARIO_IDS),
        )
        lines.append("| " + " | ".join(row) + " |")

    lines.extend(
        (
            "",
            "`MISSING` and `UNVERIFIED` are evidence gaps. This local report cannot "
            "be used as the protected release gate.",
        )
    )
    return "\n".join(lines) + "\n"


def render_latency(
    profile_results: Mapping[str, Mapping[str, Any]], profile_ids: Sequence[str]
) -> str:
    """Return one deterministic latency-attribution row per selected profile."""

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "profile_id",
            "status",
            "sample_count",
            "ack_p50_ms",
            "reply_p50_ms",
            "reply_p95_ms",
            *(f"{stage}_p50_ms" for stage in TRACE_STAGES),
            "missing_stages",
            "release_qualified",
        )
    )
    for profile_id in profile_ids:
        profile = _profile(profile_results, profile_id)
        latency = profile.get("latency") if profile else None
        latency = latency if isinstance(latency, Mapping) else {}
        stage_values = latency.get("stage_p50_ms")
        stage_values = stage_values if isinstance(stage_values, Mapping) else {}
        missing = latency.get("missing_stages")
        if not isinstance(missing, list):
            missing = list(TRACE_STAGES)
        missing_stages = ";".join(
            stage for stage in TRACE_STAGES if stage in missing
        )
        writer.writerow(
            (
                profile_id,
                (
                    _status(profile.get("status"), missing="AGENT_ERROR")
                    if profile
                    else "NOT_RUN"
                ),
                _safe_number(latency.get("sample_count"), integer=True),
                _safe_number(latency.get("ack_p50_ms")),
                _safe_number(latency.get("reply_p50_ms")),
                _safe_number(latency.get("reply_p95_ms")),
                *(_safe_number(stage_values.get(stage)) for stage in TRACE_STAGES),
                missing_stages,
                "false",
            )
        )
    return stream.getvalue()


def render_junit(
    summary: Mapping[str, Any],
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
) -> str:
    """Return fixed-message JUnit; missing evidence is an error, not a pass."""

    preflight_only = summary.get("preflight_only") is True
    tests = len(profile_ids) * len(SCENARIO_IDS)
    failures = 0
    errors = 0
    skipped = tests if preflight_only else 0
    rows: list[tuple[str, dict[str, str]]] = []
    for profile_id in profile_ids:
        statuses = _scenario_statuses(_profile(profile_results, profile_id))
        rows.append((profile_id, statuses))
        if preflight_only:
            continue
        for scenario_id in SCENARIO_IDS:
            status = _junit_scenario_status(
                summary, profile_id, scenario_id, statuses
            )
            if status in FAILURE_STATUSES or status == COT_JUNIT_FAILURE:
                failures += 1
            elif status != "PASS":
                errors += 1

    root = ElementTree.Element(
        "testsuites",
        {
            "name": "feedling-local-api-key-diagnostic",
            "tests": str(tests),
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
            "release_qualified": "false",
        },
    )
    for profile_id, statuses in rows:
        suite = ElementTree.SubElement(
            root,
            "testsuite",
            {
                "name": f"feedling.diagnostic.{profile_id}",
                "tests": str(len(SCENARIO_IDS)),
            },
        )
        for scenario_id in SCENARIO_IDS:
            testcase = ElementTree.SubElement(
                suite,
                "testcase",
                {
                    "classname": f"feedling.diagnostic.{profile_id}",
                    "name": scenario_id,
                },
            )
            if preflight_only:
                ElementTree.SubElement(
                    testcase, "skipped", {"message": "preflight-only"}
                )
                continue
            status = _junit_scenario_status(
                summary, profile_id, scenario_id, statuses
            )
            if status == "PASS":
                continue
            child = (
                "failure"
                if status in FAILURE_STATUSES or status == COT_JUNIT_FAILURE
                else "error"
            )
            message = (
                _cot_junit_message(summary, profile_id)
                if status == COT_JUNIT_FAILURE
                else f"diagnostic-evidence:{status}"
            )
            ElementTree.SubElement(
                testcase,
                child,
                {"type": status, "message": message},
            )

    ElementTree.indent(root, space="  ")
    return (
        ElementTree.tostring(
            root, encoding="unicode", xml_declaration=True, short_empty_elements=True
        )
        + "\n"
    )


def _write_private_text(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise DiagnosticRenderError("unable to create diagnostic operator artifact") from None


def render_operator_artifacts(
    *,
    summary: Mapping[str, Any],
    profile_results: Mapping[str, Mapping[str, Any]],
    profile_ids: Sequence[str],
    artifact_root: Path,
) -> None:
    """Write the three derived local-diagnostic views with owner-only modes."""

    selected = tuple(profile_ids)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(profile_id not in PROFILE_IDS for profile_id in selected)
        or set(profile_results) - set(selected)
        or summary.get("qualification_mode") != "diagnostic"
        or summary.get("release_qualified") is not False
    ):
        raise DiagnosticRenderError("diagnostic operator artifact input is invalid")
    try:
        root = artifact_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DiagnosticRenderError("diagnostic artifact root is unavailable") from None
    if artifact_root.is_symlink() or not root.is_dir():
        raise DiagnosticRenderError("diagnostic artifact root is unsafe")

    outputs = {
        root / "matrix.md": render_matrix(summary, profile_results, selected),
        root / "latency.csv": render_latency(profile_results, selected),
        root / "junit.xml": render_junit(summary, profile_results, selected),
    }
    for path, content in outputs.items():
        _write_private_text(path, content)

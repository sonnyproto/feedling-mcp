#!/usr/bin/env python3
"""Validate parent-owned receipts for live P0 scenario probes.

The authoritative file is deliberately metadata-only.  Decrypted replies used
for P0-10/P0-11 semantic judgment stay in a separate agent-private facts copy;
the receipt binds that copy by SHA-256 without publishing its contents.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from qa.request_live_scenario_probe import LIVE_SCENARIO_IDS
except ModuleNotFoundError:  # Direct ``python qa/...py`` execution.
    from request_live_scenario_probe import LIVE_SCENARIO_IDS


RECEIPT_SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
_FAILURE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_STATUSES = frozenset(
    {
        "PASS",
        "AGENT_ERROR",
        "PRODUCT_FAIL",
        "BLOCKED_CREDENTIAL",
        "BLOCKED_EVIDENCE",
        "BLOCKED_DEPLOYMENT",
        "SECURITY_FAIL",
    }
)
_RETRYABLE_SCENARIOS = frozenset({"P0-08", "P0-09", "P0-10", "P0-11"})
_TURN_COUNTS = {
    "P0-02": 0,
    "P0-03": 0,
    "P0-04": 0,
    "P0-05": 0,
    "P0-07": 0,
    "P0-08": 1,
    "P0-09": 10,
    "P0-10": 2,
    "P0-11": 1,
}
DETERMINISTIC_ASSERTIONS = {
    "P0-02": frozenset(
        {"synthetic_account_is_fresh", "whoami_matches", "trace_cleared"}
    ),
    "P0-03": frozenset(
        {"invalid_key_rejected", "invalid_key_not_echoed", "hosted_chat_not_started"}
    ),
    "P0-04": frozenset(
        {"valid_key_accepted", "provider_config_matches", "credential_omitted"}
    ),
    "P0-05": frozenset(
        {
            "runtime_status_readback_succeeds",
            "runtime_configured",
            "runtime_metadata_recorded",
        }
    ),
    "P0-07": frozenset(
        {
            "driver_enabled",
            "chat_loop_verified",
            "runtime_status_readback_succeeds",
            "no_orphan_turn",
        }
    ),
    "P0-08": frozenset(
        {
            "async_ack_received",
            "exact_reply_correlated",
            "nonce_echo_confirmed",
            "fallback_absent",
            "latency_recorded",
        }
    ),
    "P0-09": frozenset(
        {
            "ten_turns_ordered",
            "exact_replies_correlated",
            "memory_recall_confirmed",
            "no_orphan_turn",
        }
    ),
    "P0-10": frozenset({"transport_correlated"}),
    "P0-11": frozenset(
        {"transport_correlated", "provider_config_matches", "trace_route_correlated"}
    ),
}
SEMANTIC_ASSERTIONS = {
    "P0-02": (),
    "P0-03": (),
    "P0-04": (),
    "P0-05": (),
    "P0-07": (),
    "P0-08": (),
    "P0-09": (),
    "P0-10": (
        "imported_memory_recalled",
        "persona_consistency_confirmed",
        "contradictory_facts_absent",
    ),
    "P0-11": ("agent_identity_confirmed", "model_route_confirmed"),
}
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "run_id",
        "profile_id",
        "scenario_id",
        "attempt",
        "nonce",
        "started_at",
        "finished_at",
        "status",
        "failure_code",
        "assertions",
        "semantic_assertions",
        "request_ids",
        "turn_ids",
        "trace_ids",
        "turns",
        "private_facts_sha256",
        "raw_content_stored",
    }
)
_TURN_KEYS = frozenset(
    {
        "turn_index",
        "request_id",
        "turn_id",
        "trace_id",
        "ack_latency_ms",
        "reply_latency_ms",
        "reply_count",
        "content_assertion_passed",
        "fallback_detected",
        "duplicate_detected",
        "out_of_order_detected",
    }
)


class LiveScenarioReceiptError(RuntimeError):
    """A live receipt is unsafe, malformed, replayed, or inconsistent."""


def canonical_json_sha256(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise LiveScenarioReceiptError("live receipt JSON is invalid") from None
    return hashlib.sha256(payload).hexdigest()


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _safe_id(value: object, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value))
        and len(value) <= 256
        and (not value or _IDENTIFIER_RE.fullmatch(value) is not None)
    )


def _number_or_none(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _read_owned_private(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise LiveScenarioReceiptError("live receipt path is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LiveScenarioReceiptError("live receipt is unavailable") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_RECEIPT_BYTES
        ):
            raise LiveScenarioReceiptError("live receipt is unsafe")
        content = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or any(
            getattr(before, field) != getattr(after, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        ):
            raise LiveScenarioReceiptError("live receipt changed while reading")
        return content
    finally:
        os.close(descriptor)


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LiveScenarioReceiptError("live receipt contains duplicate keys")
        result[key] = value
    return result


def validate_receipt_object(
    receipt: object,
    *,
    run_id: str,
    profile_id: str,
    scenario_id: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
        raise LiveScenarioReceiptError("live scenario receipt shape is invalid")
    actual_scenario = receipt.get("scenario_id")
    actual_attempt = receipt.get("attempt")
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("kind") != "live_scenario_probe"
        or receipt.get("run_id") != run_id
        or receipt.get("profile_id") != profile_id
        or actual_scenario not in LIVE_SCENARIO_IDS
        or (scenario_id is not None and actual_scenario != scenario_id)
        or type(actual_attempt) is not int
        or actual_attempt not in (1, 2)
        or (
            actual_attempt == 2
            and actual_scenario not in _RETRYABLE_SCENARIOS
        )
        or (attempt is not None and actual_attempt != attempt)
        or not _safe_id(run_id)
        or not _safe_id(profile_id)
        or not _safe_id(receipt.get("nonce"))
        or receipt.get("status") not in _STATUSES
        or not isinstance(receipt.get("failure_code"), str)
        or _FAILURE_RE.fullmatch(receipt["failure_code"]) is None
        or not isinstance(receipt.get("raw_content_stored"), bool)
        or receipt["raw_content_stored"] is not False
        or not isinstance(receipt.get("private_facts_sha256"), str)
        or _SHA256_RE.fullmatch(receipt["private_facts_sha256"]) is None
    ):
        raise LiveScenarioReceiptError("live scenario receipt identity is invalid")
    if (receipt["status"] == "PASS") is not (receipt["failure_code"] == "NONE"):
        raise LiveScenarioReceiptError("live scenario receipt status is inconsistent")
    started = _timestamp(receipt.get("started_at"))
    finished = _timestamp(receipt.get("finished_at"))
    if started is None or finished is None or finished < started:
        raise LiveScenarioReceiptError("live scenario receipt timestamps are invalid")

    assertions = receipt.get("assertions")
    if (
        not isinstance(assertions, dict)
        or set(assertions) != set(DETERMINISTIC_ASSERTIONS[actual_scenario])
        or any(type(value) is not bool for value in assertions.values())
        or receipt.get("semantic_assertions")
        != list(SEMANTIC_ASSERTIONS[actual_scenario])
    ):
        raise LiveScenarioReceiptError("live scenario assertions are invalid")
    if receipt["status"] == "PASS" and not all(assertions.values()):
        raise LiveScenarioReceiptError("passing live receipt has failed assertions")

    turns = receipt.get("turns")
    if not isinstance(turns, list) or len(turns) > _TURN_COUNTS[actual_scenario]:
        raise LiveScenarioReceiptError("live scenario turn evidence is invalid")
    if receipt["status"] == "PASS" and len(turns) != _TURN_COUNTS[actual_scenario]:
        raise LiveScenarioReceiptError("passing live receipt has incomplete turns")
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict) or set(turn) != _TURN_KEYS:
            raise LiveScenarioReceiptError("live scenario turn shape is invalid")
        if (
            turn.get("turn_index") != index
            or not _safe_id(turn.get("request_id"))
            or turn.get("turn_id") != turn.get("request_id")
            or turn.get("trace_id") != turn.get("request_id")
            or not _number_or_none(turn.get("ack_latency_ms"))
            or not _number_or_none(turn.get("reply_latency_ms"))
            or type(turn.get("reply_count")) is not int
            or turn["reply_count"] < 0
            or turn.get("content_assertion_passed") not in (True, False, None)
            or any(
                type(turn.get(field)) is not bool
                for field in (
                    "fallback_detected",
                    "duplicate_detected",
                    "out_of_order_detected",
                )
            )
        ):
            raise LiveScenarioReceiptError("live scenario turn evidence is invalid")
        if (
            turn["ack_latency_ms"] is not None
            and turn["reply_latency_ms"] is not None
            and turn["reply_latency_ms"] < turn["ack_latency_ms"]
        ):
            raise LiveScenarioReceiptError("live scenario latency is inconsistent")
        if receipt["status"] == "PASS" and (
            turn["ack_latency_ms"] is None
            or turn["reply_latency_ms"] is None
            or turn["reply_count"] != 1
            or turn["fallback_detected"]
            or turn["duplicate_detected"]
            or turn["out_of_order_detected"]
            or (
                actual_scenario not in {"P0-10", "P0-11"}
                and turn["content_assertion_passed"] is not True
            )
        ):
            raise LiveScenarioReceiptError("passing live receipt turn is incomplete")

    request_ids = receipt.get("request_ids")
    turn_ids = receipt.get("turn_ids")
    trace_ids = receipt.get("trace_ids")
    if not all(
        isinstance(values, list)
        and len(values) == len(set(values))
        and all(_safe_id(value) for value in values)
        for values in (request_ids, turn_ids, trace_ids)
    ):
        raise LiveScenarioReceiptError("live scenario identifiers are invalid")
    if turns:
        expected = [turn["request_id"] for turn in turns]
        if request_ids != expected or turn_ids != expected or trace_ids != expected:
            raise LiveScenarioReceiptError("live scenario identifiers do not match turns")
    elif actual_scenario in {"P0-08", "P0-09", "P0-10", "P0-11"}:
        if request_ids or turn_ids or trace_ids:
            raise LiveScenarioReceiptError("live scenario identifiers are inconsistent")
    elif (
        (receipt["status"] == "PASS" and len(request_ids) != 1)
        or len(request_ids) > 1
        or turn_ids
        or trace_ids
    ):
        raise LiveScenarioReceiptError("live scenario probe identifier is invalid")
    return dict(receipt)


def validate_aggregate_object(
    payload: object, *, run_id: str, profile_id: str
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "kind",
        "run_id",
        "profile_id",
        "receipts",
    }:
        raise LiveScenarioReceiptError("live receipt aggregate shape is invalid")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != "live_scenario_receipt_set"
        or payload.get("run_id") != run_id
        or payload.get("profile_id") != profile_id
        or not isinstance(payload.get("receipts"), list)
    ):
        raise LiveScenarioReceiptError("live receipt aggregate identity is invalid")
    receipts = payload["receipts"]
    validated: list[dict[str, Any]] = []
    cursor = 0
    for scenario_id in LIVE_SCENARIO_IDS:
        attempts: list[int] = []
        while cursor < len(receipts):
            candidate = receipts[cursor]
            if not isinstance(candidate, dict) or candidate.get("scenario_id") != scenario_id:
                break
            row = validate_receipt_object(
                candidate,
                run_id=run_id,
                profile_id=profile_id,
                scenario_id=scenario_id,
            )
            attempts.append(row["attempt"])
            validated.append(row)
            cursor += 1
        if attempts not in ([1], [1, 2]):
            raise LiveScenarioReceiptError("live receipt attempts are incomplete")
        scenario_rows = validated[-len(attempts) :]
        if len(attempts) == 2 and (
            scenario_id not in _RETRYABLE_SCENARIOS
            or [row["status"] for row in scenario_rows] != ["AGENT_ERROR", "PASS"]
            or scenario_rows[0].get("failure_code")
            not in {"CHAT_TIMEOUT", "MISSING_REPLY"}
        ):
            raise LiveScenarioReceiptError(
                "live receipt retry is not a bounded transient retry"
            )
    if cursor != len(receipts):
        raise LiveScenarioReceiptError("live receipt scenario order is invalid")
    result = dict(payload)
    result["receipts"] = validated
    return result


def validate_live_scenario_receipts(
    path: Path, *, run_id: str, profile_id: str
) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(
            _read_owned_private(path),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LiveScenarioReceiptError("live receipt JSON is invalid") from None
    result = validate_aggregate_object(payload, run_id=run_id, profile_id=profile_id)
    return result, canonical_json_sha256(result)


def validate_result_binding(
    profile_result: Mapping[str, Any], aggregate: Mapping[str, Any]
) -> None:
    """Bind agent-authored projections to immutable transport observations.

    The profile agent retains authority only for the explicitly listed semantic
    assertions in P0-10/P0-11.  It cannot invent calls, identifiers, latencies,
    deterministic assertions, or turn ordering, and cannot turn a parent failure
    into PASS.
    """

    scenarios = profile_result.get("scenarios")
    turns = profile_result.get("turns")
    receipts = aggregate.get("receipts")
    if not isinstance(scenarios, list) or not isinstance(turns, list) or not isinstance(receipts, list):
        raise LiveScenarioReceiptError("live receipts do not match worker result")
    by_scenario: dict[str, list[Mapping[str, Any]]] = {
        scenario_id: [] for scenario_id in LIVE_SCENARIO_IDS
    }
    for receipt in receipts:
        if not isinstance(receipt, Mapping) or receipt.get("scenario_id") not in by_scenario:
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        by_scenario[str(receipt["scenario_id"])].append(receipt)
    result_scenarios = {
        row.get("scenario_id"): row
        for row in scenarios
        if isinstance(row, Mapping)
    }
    if len(result_scenarios) != len(scenarios):
        raise LiveScenarioReceiptError("live receipts do not match worker result")

    expected_turns: list[tuple[str, Mapping[str, Any]]] = []
    for scenario_id in LIVE_SCENARIO_IDS:
        scenario = result_scenarios.get(scenario_id)
        rows = by_scenario[scenario_id]
        if not isinstance(scenario, Mapping) or not rows:
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        if (
            scenario.get("attempts") != len(rows)
            or scenario.get("started_at") != rows[0].get("started_at")
            or scenario.get("finished_at") != rows[-1].get("finished_at")
        ):
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        attempt_results = scenario.get("attempt_results")
        if not isinstance(attempt_results, list) or len(attempt_results) != len(rows):
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        for index, (attempt_result, receipt) in enumerate(
            zip(attempt_results, rows, strict=True), start=1
        ):
            if (
                not isinstance(attempt_result, Mapping)
                or attempt_result.get("attempt") != index
                or (
                    receipt.get("status") != "PASS"
                    and attempt_result.get("status") != receipt.get("status")
                )
            ):
                raise LiveScenarioReceiptError("worker result is greener than live receipt")
        bounded_retry = [row.get("status") for row in rows] == [
            "AGENT_ERROR",
            "PASS",
        ]
        if (
            any(row.get("status") != "PASS" for row in rows)
            and not bounded_retry
            and scenario.get("status") == "PASS"
        ):
            raise LiveScenarioReceiptError("worker result is greener than live receipt")
        assertions = scenario.get("assertions")
        if not isinstance(assertions, Mapping):
            raise LiveScenarioReceiptError("live receipts do not match worker result")
        for key, value in rows[-1]["assertions"].items():
            if key in assertions and assertions.get(key) is not value:
                raise LiveScenarioReceiptError("live assertion does not match worker result")
        for field in ("request_ids", "turn_ids", "trace_ids"):
            expected_ids = [
                value for receipt in rows for value in receipt.get(field, [])
            ]
            if scenario.get(field) != expected_ids:
                raise LiveScenarioReceiptError("live identifiers do not match worker result")
        expected_turns.extend(
            (scenario_id, turn)
            for receipt in rows
            for turn in receipt.get("turns", [])
        )

    actual_turns = [
        row
        for row in turns
        if isinstance(row, Mapping) and row.get("scenario_id") in LIVE_SCENARIO_IDS
    ]
    if len(actual_turns) != len(expected_turns):
        raise LiveScenarioReceiptError("live turns do not match worker result")
    for actual, (expected_scenario, expected) in zip(
        actual_turns, expected_turns, strict=True
    ):
        if (
            actual.get("scenario_id") != expected_scenario
            or actual.get("turn_index") != expected.get("turn_index")
            or actual.get("request_id") != expected.get("request_id")
            or actual.get("turn_id") != expected.get("turn_id")
            or actual.get("trace_id") != expected.get("trace_id")
            or actual.get("ack_latency_ms") != expected.get("ack_latency_ms")
            or actual.get("reply_latency_ms") != expected.get("reply_latency_ms")
            or actual.get("reply_count") != expected.get("reply_count")
            or actual.get("fallback_detected") != expected.get("fallback_detected")
            or actual.get("duplicate_detected") != expected.get("duplicate_detected")
            or actual.get("out_of_order_detected")
            != expected.get("out_of_order_detected")
            or (
                expected.get("content_assertion_passed") is not None
                and actual.get("content_assertion_passed")
                != expected.get("content_assertion_passed")
            )
        ):
            raise LiveScenarioReceiptError("live turns do not match worker result")

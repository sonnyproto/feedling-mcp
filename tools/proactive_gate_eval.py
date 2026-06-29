#!/usr/bin/env python3
"""Summarize human-reviewed Proactive / Runtime V2 decisions.

Inputs are either:
  1. A JSON snapshot from /v1/proactive/debug.
  2. A live backend URL + API key.

The script does not train anything. It gives the review loop a stable confusion
matrix and the concrete examples to inspect before changing runtime policy.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


FALSE_POSITIVE_LABELS = {
    "too_much_buzz",
    "wrong_voice",
    "late_irrelevant",
    "privacy_bad",
    # Legacy Gate labels accepted for old snapshots.
    "spam",
    "weak_connection",
    "repeated",
}
FALSE_NEGATIVE_LABELS = {
    "missed_moment",
    "went_dark",
    # Legacy Gate label accepted for old snapshots.
    "missed_opportunity",
}
TRUE_POSITIVE_LABELS = {
    "good_presence",
    # Legacy Gate labels accepted for old snapshots.
    "correct_true",
    "great_companion_moment",
}
TRUE_NEGATIVE_LABELS = {"correct_false"}
RUNTIME_OBSERVATION_LABELS = {"too_chatty", "ignored_manual", "stutter"}


def _load_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if args.debug_json:
        return json.loads(Path(args.debug_json).read_text())
    if not args.api_url or not args.api_key:
        raise SystemExit("Provide --debug-json or both --api-url and --api-key")
    url = args.api_url.rstrip("/") + "/v1/proactive/debug"
    req = urllib.request.Request(url, headers={"X-API-Key": args.api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_reviews(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    latest = snapshot.get("latest_review_by_decision")
    if isinstance(latest, dict):
        return {str(k): v for k, v in latest.items() if isinstance(v, dict)}
    out: dict[str, dict[str, Any]] = {}
    for review in snapshot.get("reviews") or []:
        if isinstance(review, dict) and review.get("decision_id"):
            out[str(review["decision_id"])] = review
    return out


def _bucket(label: str, decision_true: bool) -> str:
    if label in RUNTIME_OBSERVATION_LABELS:
        return "runtime_observation"
    if label in TRUE_POSITIVE_LABELS:
        return "tp" if decision_true else "fn_review_inconsistent"
    if label in TRUE_NEGATIVE_LABELS:
        return "tn" if not decision_true else "fp_review_inconsistent"
    if label in FALSE_POSITIVE_LABELS:
        return "fp" if decision_true else "tn_review_inconsistent"
    if label in FALSE_NEGATIVE_LABELS:
        return "fn" if not decision_true else "tp_review_inconsistent"
    return "unknown"


def summarize(snapshot: dict[str, Any]) -> dict[str, Any]:
    reviews = _latest_reviews(snapshot)
    decisions = snapshot.get("decisions") or []
    by_id = {
        str(d.get("decision_id")): d
        for d in decisions
        if isinstance(d, dict) and d.get("decision_id")
    }

    confusion = Counter()
    by_label = Counter()
    by_reason: dict[str, Counter] = defaultdict(Counter)
    by_intent: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for decision_id, review in reviews.items():
        decision = by_id.get(decision_id)
        if not decision:
            continue
        label = str(review.get("label") or "unknown")
        decision_true = bool(decision.get("should_reach_out"))
        bucket = _bucket(label, decision_true)
        confusion[bucket] += 1
        by_label[label] += 1
        reason = str(decision.get("reason") or decision.get("abstention_reason") or "")
        intent = str(decision.get("intent_label") or "")
        by_reason[bucket][reason] += 1
        by_intent[bucket][intent] += 1
        if len(examples[bucket]) < 10:
            examples[bucket].append({
                "decision_id": decision_id,
                "label": label,
                "decision_true": decision_true,
                "reason": reason,
                "intent_label": intent,
                "connection": decision.get("connection") or {},
                "context_hint": decision.get("context_hint", ""),
                "notes": review.get("notes", ""),
                "frame_ids": decision.get("frame_ids") or [],
            })

    tp = confusion["tp"]
    fp = confusion["fp"]
    fn = confusion["fn"]
    tn = confusion["tn"]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    false_positive_rate = fp / (fp + tn) if fp + tn else None

    return {
        "reviewed": sum(confusion.values()),
        "confusion": dict(confusion),
        "precision": precision,
        "recall": recall,
        "false_positive_rate": false_positive_rate,
        "labels": dict(by_label),
        "top_reasons_by_bucket": {
            bucket: counter.most_common(10)
            for bucket, counter in by_reason.items()
        },
        "top_intents_by_bucket": {
            bucket: counter.most_common(10)
            for bucket, counter in by_intent.items()
        },
        "examples": examples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug-json", help="Path to a /v1/proactive/debug JSON snapshot")
    parser.add_argument("--api-url", help="Backend URL, e.g. https://api.feedling.app")
    parser.add_argument("--api-key", help="Feedling API key for one reviewed user")
    parser.add_argument("--out", help="Optional path to write the JSON report")
    args = parser.parse_args()

    report = summarize(_load_snapshot(args))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Genesis v2 — CONSERVATIVE near-exact dedup backstop for the foreground/background seam.

The e2e on the real test deploy proved the background can write a reworded twin of a
foreground core memory ("用户计划从前端转向 AI agent" vs "用户当前职业方向是前端，计划
转向 AI agent"). The PRIMARY dedup for those is semantic and lives in the MODEL: the
background fact_write gets the foreground core as known_memories ("already saved, don't
repeat"), and the model reliably tells a reworded twin from a genuinely different fact.

A pure lexical signal CANNOT do that job: measured on real data, same-template-
different-value facts that must STAY distinct ("喜欢美式咖啡" vs "喜欢拿铁" = 0.63,
"狗叫蛋子" vs "养了金毛" = 0.71) score HIGHER than the real twins (~0.47). Any
threshold low enough to catch twins would wrongly merge those. So this module is only
a CONSERVATIVE backstop: a high containment threshold (default 0.82) that catches a
near-identical survivor the model missed, while staying safely above the distinct-but-
similar band so it never merges two real facts. Env-tunable
(FEEDLING_GENESIS_DEDUP_CONTAINMENT) for the e2e, but keep it high.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Sequence

# strip whitespace + CJK/latin punctuation so "催 AI 写代码" == "催AI写代码"
_STRIP = re.compile(r"[\s　,.!?;:、，。！？；：…·\-—_()\[\]{}<>\"'`~/\\|@#$%^&*+=]+")

_DEFAULT_THRESHOLD = 0.82


def _normalize(text: str) -> str:
    return _STRIP.sub("", str(text or "").strip().lower())


def _bigrams(text: str) -> set[str]:
    n = _normalize(text)
    if len(n) <= 1:
        return {n} if n else set()
    return {n[i:i + 2] for i in range(len(n) - 1)}


def containment(a: str, b: str) -> float:
    """Bigram overlap divided by the SHORTER side's bigram count (0..1). Catches the
    'b is a reworded superset of a' case that drags Jaccard down."""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / min(len(ba), len(bb))


def _threshold() -> float:
    try:
        return max(0.0, min(float(os.environ.get("FEEDLING_GENESIS_DEDUP_CONTAINMENT",
                                                  str(_DEFAULT_THRESHOLD))), 1.0))
    except Exception:
        return _DEFAULT_THRESHOLD


def is_semantic_dup(a: str, b: str, *, threshold: float | None = None) -> bool:
    return containment(a, b) >= (_threshold() if threshold is None else threshold)


def memory_text(m: Mapping) -> str:
    """The text we dedup on: summary + content (either may carry the fact)."""
    if not isinstance(m, Mapping):
        return ""
    return f"{str(m.get('summary') or '').strip()} {str(m.get('content') or '').strip()}".strip()


def filter_semantic_dups(
    memories: Sequence[Mapping],
    against_texts: Iterable[str],
    *,
    threshold: float | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split `memories` into (kept, dropped): drop any whose text is a semantic dup of
    any anchor in `against_texts` (e.g. the foreground core already written). Anchors
    are compared against summary+content so reworded twins are caught."""
    anchors = [t for t in (str(x or "").strip() for x in against_texts) if t]
    if not anchors:
        return [dict(m) if isinstance(m, Mapping) else m for m in memories], []
    kept: list[dict] = []
    dropped: list[dict] = []
    for m in memories:
        txt = memory_text(m)
        if txt and any(is_semantic_dup(txt, a, threshold=threshold) for a in anchors):
            dropped.append(dict(m) if isinstance(m, Mapping) else {"_raw": m})
        else:
            kept.append(dict(m) if isinstance(m, Mapping) else m)
    return kept, dropped

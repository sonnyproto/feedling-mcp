"""
context_memories selection — pure helpers, no native deps.
============================================================

Lives outside enclave_app.py so it can be unit-tested without the full
nacl / cryptography stack. enclave_app.py imports from here.

The selection feeds into every /v1/chat/history response: up to 8 plaintext
memory cards the agent reads alongside chat history. Rules:
  · Up to 3 turning-point cards (title prefix `转折｜`), newest first
  · Up to 2 most-recently-created cards
  · Up to 3 cards with highest character-bigram Jaccard overlap against
    the latest user message
  · Dedupe by id, cap total at 8

Bigrams (no tokenization) so zh + en + mixed all work without language
deps. Acceptable up to ~5000 cards/user; beyond that, swap in vector
search.
"""

from __future__ import annotations

import re


def char_bigrams(s: str) -> set:
    """Lowercase character bigrams. Empty/single-char input → empty set."""
    if not s:
        return set()
    s = s.lower()
    if len(s) < 2:
        return set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def bigram_jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


_EN_STOPWORDS = {
    "the", "and", "you", "your", "for", "with", "that", "this", "what",
    "when", "where", "why", "how", "can", "could", "would", "should",
}
_ZH_STOP_CHARS = set("我你他她它的是了在和就都不吗呢啊吧这那什么怎么一个有没给要说")


def _meaning_units(text: str) -> set[str]:
    """Small bilingual lexical units for relevance without heavy deps."""

    raw = (text or "").lower()
    units = {
        token
        for token in re.findall(r"[a-z0-9_]{3,}", raw)
        if token not in _EN_STOPWORDS
    }
    for ch in re.findall(r"[\u4e00-\u9fff]", raw):
        if ch not in _ZH_STOP_CHARS:
            units.add(ch)
    return units


def _memory_relevance_score(query: str, memory: dict) -> float:
    hay = " ".join(
        str(memory.get(key) or "")
        for key in ("title", "description", "her_quote", "context", "linked_dimension")
    )
    bigram_score = bigram_jaccard(char_bigrams(query), char_bigrams(hay))
    q_units = _meaning_units(query)
    h_units = _meaning_units(hay)
    unit_score = 0.0
    if q_units and h_units:
        unit_score = len(q_units & h_units) / max(len(q_units), 1)
    return max(bigram_score, unit_score)


def memory_relevance_score(query: str, memory: dict) -> float:
    """Public wrapper used by the hosted API state resolver."""

    return _memory_relevance_score(query, memory)


def _is_correction_memory(memory: dict) -> bool:
    source = str(memory.get("source") or "").lower()
    if source in {"model_api_correction", "user_correction", "settings_correction"}:
        return True
    title = str(memory.get("title") or "").lower()
    return any(marker in title for marker in ("correction", "纠正", "设定更新", "边界更新"))


def select_context_memories(
    moments: list[dict],
    latest_user_text: str,
    cap: int = 8,
    mode: str = "default",
) -> list[dict]:
    """Pick up to `cap` memory cards.

    Default mode keeps the resident/MCP behavior: turning points + recent +
    relevance. ``model_api`` / ``strict`` mode avoids context pollution for
    hosted chat by returning only explicit corrections plus query-relevant cards.

    `moments` are pre-decrypted dicts with at least these keys:
      id, title, description, occurred_at, created_at
    """
    if not moments:
        return []

    moments = [
        m for m in moments
        if not (
            m.get("is_archived") is True
            or str(m.get("archived_at") or "").strip()
            or str(m.get("archive_reason") or "").strip()
        )
    ]
    if not moments:
        return []

    strict = str(mode or "").strip().lower() in {"model_api", "strict"}
    chosen_ids: set = set()
    out: list[dict] = []

    if strict:
        corrections = []
        for m in moments:
            if not _is_correction_memory(m):
                continue
            score = _memory_relevance_score(latest_user_text, m) if latest_user_text else 0.0
            text = " ".join(str(m.get(k) or "") for k in ("title", "description", "context")).lower()
            # Global corrections are usually "never say/use/call X again" or
            # broad identity/persona boundaries. Keep a tiny newest slice so
            # model_api does not keep resurrecting explicitly-forbidden state,
            # but avoid dumping unrelated ordinary recent cards into chat.
            global_boundary = any(
                marker in text
                for marker in (
                    "不要", "别再", "不准", "不许", "do not", "don't",
                    "never", "称呼", "名字", "name", "persona", "设定",
                    "人设", "口吻", "语气", "boundary", "边界",
                )
            )
            if score >= 0.08 or global_boundary:
                corrections.append((score, m.get("created_at") or "", m))
        corrections.sort(key=lambda item: (item[0], item[1]), reverse=True)
        corrections = [m for _, __, m in corrections[:3]]
        for m in corrections:
            if m["id"] not in chosen_ids:
                out.append(m)
                chosen_ids.add(m["id"])

        if latest_user_text:
            scored = []
            for m in moments:
                if m["id"] in chosen_ids:
                    continue
                score = _memory_relevance_score(latest_user_text, m)
                if score >= 0.20:
                    scored.append((score, m.get("occurred_at") or "", m))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            for _, __, m in scored[: max(0, cap - len(out))]:
                if m["id"] not in chosen_ids:
                    out.append(m)
                    chosen_ids.add(m["id"])
        return out[:cap]

    # Bucket 1 — turning points by occurred_at desc, max 3
    turning = sorted(
        [m for m in moments if (m.get("title") or "").startswith("转折｜")],
        key=lambda m: m.get("occurred_at") or "",
        reverse=True,
    )[:3]
    for m in turning:
        if m["id"] not in chosen_ids:
            out.append(m)
            chosen_ids.add(m["id"])

    # Bucket 2 — most-recently-created (skip already-chosen), max 2
    recent_pool = [m for m in moments if m["id"] not in chosen_ids]
    recent = sorted(
        recent_pool,
        key=lambda m: m.get("created_at") or "",
        reverse=True,
    )[:2]
    for m in recent:
        if m["id"] not in chosen_ids:
            out.append(m)
            chosen_ids.add(m["id"])

    # Bucket 3 — relevance to latest user message, max 3
    if latest_user_text:
        scored = []
        for m in moments:
            if m["id"] in chosen_ids:
                continue
            score = _memory_relevance_score(latest_user_text, m)
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        for _, m in scored[:3]:
            if m["id"] not in chosen_ids:
                out.append(m)
                chosen_ids.add(m["id"])

    return out[:cap]

"""Deterministic world book matcher. Pure (no nacl/crypto), so it unit-tests
without the enclave stack — enclave_app.py imports and calls it after decrypt.
Mirrors the context_memory_selection.py "pure selection module" convention.

Rules (see docs/superpowers/specs/2026-07-03-worldbook-server-design.md §2B):
  - scan the last N=5 messages (user + assistant);
  - an entry matches if enabled AND (alwaysOn OR any keyword is a
    case-insensitive substring of the scanned text);
  - each entry is injected at most once, in list order;
  - matched content is injected in full (NO truncation — length is bounded at
    upload time, not here);
  - output is wrapped in <world_book>…</world_book>; empty match → "".
"""
from __future__ import annotations

WORLD_BOOK_SCAN_MESSAGES = 5  # N: scan the last N messages


def _recent_text(messages: list[dict], n: int) -> str:
    recent = messages[-n:] if n and n > 0 else messages
    parts = []
    for m in recent:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            parts.append(c)
    return "\n".join(parts)


def _triggered(entry: dict, scan_lower: str) -> bool:
    if not entry.get("enabled", True):
        return False
    if entry.get("alwaysOn", False):
        return True
    for kw in entry.get("keywords") or []:
        kw = (kw or "").strip()
        if kw and kw.lower() in scan_lower:
            return True
    return False


def matched_entries(entries: list[dict], messages: list[dict], *,
                    n: int = WORLD_BOOK_SCAN_MESSAGES) -> list[dict]:
    scan_lower = _recent_text(messages, n).lower()
    out: list[dict] = []
    seen: set = set()
    for e in entries or []:
        eid = e.get("id")
        if eid in seen:
            continue
        if _triggered(e, scan_lower):
            out.append(e)
            seen.add(eid)
    return out


def build_world_book_block(entries: list[dict], messages: list[dict], *,
                           n: int = WORLD_BOOK_SCAN_MESSAGES) -> str:
    lines = []
    for e in matched_entries(entries, messages, n=n):
        content = (e.get("content") or "").strip()
        if not content:
            continue
        name = (e.get("name") or "").strip()
        lines.append(f"[{name}] {content}" if name else content)
    if not lines:
        return ""
    return "<world_book>\n" + "\n".join(lines) + "\n</world_book>"

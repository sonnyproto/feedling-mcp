#!/usr/bin/env python3
"""Seed legacy (pre-v1) memory cards, for testing the old-card -> v1 migration.

WHY THIS EXISTS
---------------
The normal write path (`/v1/memory/actions` -> `_memory_inner_from_action`) ALWAYS
normalizes to the v1 shape {summary, content, bucket, threads}. So you CANNOT make a
legacy card through it — a "legacy add" produces a v1 card. A genuine legacy card
needs an OLD-shape inner ({title, description, her_quote, context, linked_dimension}
and NONE of the v1 fields) sealed into an envelope.

This script seals old-shape inners with `content_encryption.build_envelope` (the same
client-side seal the consumer's capture lane uses) and writes them via the
prebuilt-envelope `memory.add` path. The result is real legacy cards that
`is_legacy_card_inner` flags True, so `/v1/memory/legacy_batch` and the capture-tick
migration will detect and upgrade them.

HOW TO RUN
----------
From the consumer checkout, with the consumer's env loaded
(FEEDLING_API_URL / FEEDLING_API_KEY / FEEDLING_ENCLAVE_URL — the same vars the
resident consumer uses):

    python3 tools/seed_legacy_memory.py             # seed the full default set (10)
    python3 tools/seed_legacy_memory.py --count 3   # seed only the first N
    python3 tools/seed_legacy_memory.py --type fact # override memory type

It prints the raw write response; record the new ids (or read them back via
`/v1/memory/legacy_batch`) so you can assert id-stability after migration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# tools/ on path so we can reuse the consumer's crypto + whoami + write helpers.
# Importing it also puts backend/ on sys.path (content_encryption lives there).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chat_resident_consumer as c  # noqa: E402


# Old-shape inners (pre-v1): title/description/her_quote/context/linked_dimension,
# and deliberately NO summary/content/bucket/threads -> is_legacy_card_inner == True.
# `days_ago` only sets occurred_at so the seeded garden looks like a real history.
_LEGACY_CARDS = [
    {"title": "去西湖", "description": "上周末一起骑车环了西湖，边骑边聊到天黑。",
     "her_quote": "下次还要来", "context": "周末约会", "linked_dimension": "我们的关系", "days_ago": 40},
    {"title": "她的狗叫蛋子", "description": "她养了只柯基，名字叫蛋子，特别黏人。",
     "context": "聊到宠物", "linked_dimension": "她的生活", "days_ago": 90},
    {"title": "怕香菜", "description": "她说绝对不能吃香菜，闻到都难受。",
     "her_quote": "香菜是邪恶的", "context": "一起点外卖", "linked_dimension": "口味偏好", "days_ago": 75},
    {"title": "换了工作", "description": "她从原来的公司离职，去了一家做设计的小团队。",
     "context": "职业变动", "linked_dimension": "工作", "days_ago": 30},
    {"title": "妈妈住院", "description": "她妈妈那阵子住院，她请假回家照顾了一周。",
     "her_quote": "还好没大事", "context": "家里的事", "linked_dimension": "妈妈", "days_ago": 120},
    {"title": "喜欢下雨天", "description": "她说最喜欢下雨天待在家里听歌。",
     "context": "随口聊到天气", "linked_dimension": "情绪", "days_ago": 20},
    {"title": "第一次见面", "description": "我们第一次见面是在学校图书馆，她在找一本书。",
     "her_quote": "原来是你", "context": "回忆开端", "linked_dimension": "我们的关系", "days_ago": 200},
    {"title": "半程马拉松", "description": "她报名了城市半程马拉松，周末都在练跑步。",
     "context": "运动目标", "linked_dimension": "健康", "days_ago": 15},
    {"title": "怕黑", "description": "她其实有点怕黑，睡觉要留一盏小灯。",
     "context": "深夜聊天", "linked_dimension": "她的小秘密", "days_ago": 60},
    {"title": "想去日本", "description": "她一直想去日本看樱花，计划明年春天去。",
     "her_quote": "一定要去一次", "context": "聊旅行计划", "linked_dimension": "愿望", "days_ago": 10},
]

_OLD_FIELDS = ("title", "description", "her_quote", "context", "linked_dimension")


def _occurred_at(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed legacy (pre-v1) memory cards for migration testing.")
    ap.add_argument("--count", type=int, default=len(_LEGACY_CARDS),
                    help="how many of the default cards to seed (default: all)")
    ap.add_argument("--type", default="moment",
                    help="memory type: moment/quote/fact/event/insight/reflection (default: moment)")
    args = ap.parse_args()

    if not getattr(c, "_ENCRYPTION_AVAILABLE", False):
        print("ERROR: content_encryption not available — run from the consumer checkout.", file=sys.stderr)
        return 2
    if not c.FEEDLING_API_URL:
        print("ERROR: FEEDLING_API_URL not set — load the consumer env first.", file=sys.stderr)
        return 2
    if not c._refresh_whoami_for_encrypted_reply():
        print("ERROR: whoami refresh failed — check FEEDLING_API_KEY / FEEDLING_ENCLAVE_URL.", file=sys.stderr)
        return 2

    user_id = str(c._whoami_cache.get("user_id") or "").strip()
    user_pk = c._whoami_cache.get("user_pk")
    enc_pk = c._whoami_cache.get("enclave_pk")
    if not (user_id and user_pk and enc_pk):
        print("ERROR: missing user/enclave keys from whoami.", file=sys.stderr)
        return 2

    cards = _LEGACY_CARDS[: max(1, args.count)]
    actions: list[dict] = []
    for card in cards:
        inner = {k: card[k] for k in _OLD_FIELDS if card.get(k)}  # old shape only -> legacy
        occurred_at = _occurred_at(int(card.get("days_ago", 30)))
        envelope = c._build_envelope(
            plaintext=json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            owner_user_id=user_id,
            user_pk_bytes=user_pk,
            enclave_pk_bytes=enc_pk,
            visibility="shared",
        )
        envelope.update({
            "type": args.type,
            "occurred_at": occurred_at,
            "importance": 0.6,
            "pulse": 0.5,
            "anchor_memory_ids": [],
            "source": "legacy_seed",
            "last_referenced_at": occurred_at,
        })
        actions.append({
            "type": "memory.add",
            "envelope": envelope,
            "reason": "legacy seed for migration testing",
        })

    try:
        result = c.execute_memory_actions(actions)
    except Exception as e:  # noqa: BLE001 — surface the HTTP/validation error plainly
        print(f"ERROR: write failed: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nseeded {len(actions)} legacy card(s) as type={args.type!r}.")
    print("verify they are legacy:  POST /v1/memory/legacy_batch")
    print("then trigger migration:  POST /v1/capture/tick   (after the quiet window)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

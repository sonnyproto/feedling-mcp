#!/usr/bin/env python3
"""Recover register-orphaned accounts by consolidating per-public_key lineages.

Root cause (see docs/orphan-account-recovery-plan.md): the client called
/v1/users/register on reinstall/reconnect instead of /v1/access/claim-token,
minting a fresh empty account each time and orphaning the old one's
chat/memory/identity. Because every account in a lineage shares the same
X25519 content public key, the orphaned data is still decryptable by the live
device — it's merely attached to a dead user_id.

This tool re-owns the orphaned content INTO the lineage's currently-active
account (the "survivor"). Safe because:
  - chat_messages PK is (user_id, msg_id); msg_id has 0 cross-user collisions
    and `seq` is a GLOBAL unique sequence, so re-owning preserves chronological
    ORDER BY seq with no collisions.
  - memory_moments PK is (user_id, moment_id); moment_id has 0 cross-user
    collisions.
  - user_blobs PK is (user_id, kind); we only move CONTENT kinds the survivor
    lacks, and never touch device/session state.

Scope (per docs decisions, 2026-06-07):
  - Move chat_messages + memory_moments + content blobs only.
  - Do NOT move frames / user_logs.
  - Do NOT delete any user rows (empty shells kept).
  - Skip lineages listed in --skip (e.g. concurrent multi-device).

Usage:
  python tools/recover_orphan_accounts.py --dry-run [--lineage <public_key>]
  python tools/recover_orphan_accounts.py --apply  --lineage <public_key>
  python tools/recover_orphan_accounts.py --apply            # all eligible

DATABASE_URL is read from deploy/.env (never printed).
"""
from __future__ import annotations
import argparse
import os
import sys

import psycopg

# Content blobs worth bringing to the survivor when it lacks them.
CONTENT_BLOB_KINDS = {"identity", "model_api", "model_api_runtime", "bootstrap"}
CONTENT_BLOB_PREFIXES = ("history_import_job:",)
# Device/session state — always keep the survivor's, never bring the donor's.
DEVICE_STATE_KINDS = {"tokens", "frames_meta", "push_state",
                      "live_activity_state", "onboarding_route"}

# Lineages to skip (public keys). Default: the concurrent multi-device lineage.
DEFAULT_SKIP = {"hmgkw7t6cx5w"}  # prefix-matched against public_key


def _load_database_url() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(here, "deploy", ".env")
    for line in open(env_path):
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DATABASE_URL not found in deploy/.env")


def _is_content_kind(kind: str) -> bool:
    return kind in CONTENT_BLOB_KINDS or any(kind.startswith(p) for p in CONTENT_BLOB_PREFIXES)


def _acct_stats(cur, uid: str) -> dict:
    cur.execute("select count(*), coalesce(max(ts),0) from chat_messages where user_id=%s", (uid,))
    chat, chat_max = cur.fetchone()
    cur.execute("select count(*) from memory_moments where user_id=%s", (uid,))
    mem = cur.fetchone()[0]
    cur.execute("select kind from user_blobs where user_id=%s", (uid,))
    blobs = [r[0] for r in cur.fetchall()]
    cur.execute("select doc from users where user_id=%s", (uid,))
    doc = cur.fetchone()[0]
    binds = doc.get("access_bindings") or []
    last_seen = max([b.get("last_seen_at", "") for b in binds] + [""])
    live_keys = len([k for k in (doc.get("api_keys") or []) if not k.get("revoked_at")])
    if live_keys == 0 and doc.get("api_key_hash"):
        live_keys = 1  # legacy top-level key
    return dict(uid=uid, created=doc.get("created_at", ""), chat=chat,
                chat_max=chat_max or 0, mem=mem, blobs=blobs, last_seen=last_seen,
                live_keys=live_keys)


def _survivor_rank(a: dict) -> tuple:
    """Higher tuple = more likely the lineage's currently-ACTIVE account (the
    one whose API key the app still holds). Priority: has a live (non-revoked)
    api_key — only such accounts can be authenticated against; then most
    recently registered (each reinstall mints a newer active account); then
    most recent binding activity; then chat recency as a last tiebreak.

    Chat presence is deliberately NOT primary: a freshly re-registered active
    account often has no chat yet while the dead orphan holds all the history —
    ranking by chat would move data INTO the dead account."""
    return (1 if a.get("live_keys", 0) > 0 else 0,
            a.get("created", ""), a.get("last_seen", ""), a.get("chat_max", 0.0))


def _pick_survivor(accts: list) -> dict:
    return max(accts, key=_survivor_rank)


def _lineages(cur) -> list[str]:
    cur.execute("""
        select doc->>'public_key' pk
        from users where coalesce(doc->>'public_key','')<>''
        group by doc->>'public_key' having count(*)>1
        order by count(*) desc""")
    return [r[0] for r in cur.fetchall()]


def _plan_lineage(cur, pk: str) -> dict | None:
    cur.execute("select user_id from users where doc->>'public_key'=%s", (pk,))
    accts = [_acct_stats(cur, r[0]) for r in cur.fetchall()]
    if len(accts) < 2:
        return None
    # survivor = the lineage's currently-active account (see _survivor_rank).
    survivor = _pick_survivor(accts)
    donors = [a for a in accts if a["uid"] != survivor["uid"]]
    move_chat = sum(d["chat"] for d in donors)
    move_mem = sum(d["mem"] for d in donors)
    # blob bring-over: content kinds the survivor lacks; if >1 donor has the
    # same kind, take it from the most-recently-active donor.
    surv_kinds = set(survivor["blobs"])
    bring: dict[str, str] = {}  # kind -> donor uid
    for d in sorted(donors, key=_survivor_rank, reverse=True):
        for kind in d["blobs"]:
            if kind in surv_kinds or kind in DEVICE_STATE_KINDS:
                continue
            if not _is_content_kind(kind):
                continue
            bring.setdefault(kind, d["uid"])  # first (best) donor wins
    return dict(pk=pk, survivor=survivor, donors=donors,
                move_chat=move_chat, move_mem=move_mem, bring=bring)


def _print_plan(plan: dict) -> None:
    s = plan["survivor"]
    print(f"\npk={plan['pk'][:14]}..  survivor={s['uid']} "
          f"(created={s['created'][:19]} chat={s['chat']} mem={s['mem']})")
    print(f"  + re-own chat={plan['move_chat']} memory={plan['move_mem']} "
          f"from {len(plan['donors'])} donor(s)")
    if plan["bring"]:
        for kind, donor in plan["bring"].items():
            print(f"  + bring blob '{kind}' from {donor}")
    else:
        print("  (no content blobs to bring; survivor already has them)")


def _apply_lineage(conn, plan: dict) -> None:
    survivor = plan["survivor"]["uid"]
    donor_ids = [d["uid"] for d in plan["donors"]]
    with conn.transaction():
        cur = conn.cursor()
        for donor in donor_ids:
            cur.execute("update chat_messages set user_id=%s where user_id=%s", (survivor, donor))
            cur.execute("update memory_moments set user_id=%s where user_id=%s", (survivor, donor))
        for kind, donor in plan["bring"].items():
            # survivor lacks this kind (verified in plan); move the donor's row.
            cur.execute("update user_blobs set user_id=%s where user_id=%s and kind=%s",
                        (survivor, donor, kind))
    print(f"  applied: lineage {plan['pk'][:14]}.. -> survivor {survivor}")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--lineage", help="restrict to one public_key (exact match)")
    ap.add_argument("--skip", action="append", default=[],
                    help="public_key prefix to skip (repeatable); defaults include the concurrent lineage")
    args = ap.parse_args()

    skip_prefixes = set(DEFAULT_SKIP) | set(args.skip)

    conn = psycopg.connect(_load_database_url(), connect_timeout=15)
    # autocommit=True so planning SELECTs don't hold an outer transaction open;
    # each lineage's `with conn.transaction()` is then its own atomic top-level
    # BEGIN/COMMIT. (With autocommit=False the outer txn from planning SELECTs
    # turns per-lineage transaction() into a mere savepoint that conn.close()
    # would roll back.)
    conn.autocommit = True
    if args.dry_run:
        conn.read_only = True
    cur = conn.cursor()

    pks = [args.lineage] if args.lineage else _lineages(cur)
    eligible, skipped = [], []
    for pk in pks:
        if any(pk.startswith(p) for p in skip_prefixes) and pk != args.lineage:
            skipped.append(pk); continue
        plan = _plan_lineage(cur, pk)
        if plan and (plan["move_chat"] or plan["move_mem"] or plan["bring"]):
            eligible.append(plan)

    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'}  eligible lineages={len(eligible)}  skipped={len(skipped)}")
    tot_c = tot_m = 0
    for plan in eligible:
        _print_plan(plan)
        tot_c += plan["move_chat"]; tot_m += plan["move_mem"]
        if args.apply:
            _apply_lineage(conn, plan)
    print(f"\nTOTAL re-own: chat={tot_c} memory={tot_m} across {len(eligible)} lineage(s)")
    if skipped:
        print(f"skipped (manual): {[p[:14] for p in skipped]}")
    if args.dry_run:
        print("\n(dry-run — no writes performed)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

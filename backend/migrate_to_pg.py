#!/usr/bin/env python3
"""One-shot migration: local-file persistence → PostgreSQL.

Reads the legacy FEEDLING_DATA_DIR tree (users.json + .pepper + per-user
JSON/JSONL files + frame envelopes) and imports it into the PostgreSQL schema
defined in db.py. The filesystem is read-only here; nothing on disk is deleted
(it stays as a rollback copy).

Crypto is untouched: every encrypted field (body_ct / nonce / K_user /
K_enclave) is stored verbatim. Critically, the .pepper bytes are imported so
that existing api_key_hashes keep validating and old api_keys keep working.

Usage:
    DATABASE_URL=postgresql://...?sslmode=require \
        python backend/migrate_to_pg.py --data-dir /data

    # re-check counts after a run without importing again:
    DATABASE_URL=... python backend/migrate_to_pg.py --data-dir /data --verify

Per-user import is idempotent: each user's existing rows are cleared
(delete_user_data) before re-import, so re-running converges to the same state.
Run this during cutover, before the new backend starts serving the user.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# server_config key set once a full migration completes. The one-shot migrate
# container (docker-compose.phala.yaml) is safe to leave in place permanently:
# on every CVM reboot it re-runs, sees this marker, and no-ops instead of
# re-importing stale files over data users have since written to Postgres.
_MIGRATION_DONE_KEY = "migration_done"

# Make `import db` work regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

# file stem (without .json) -> user_blobs.kind
BLOB_FILES = {
    "push_state": "push_state",
    "live_activity_state": "live_activity_state",
    "tokens": "tokens",
    "proactive_settings": "proactive_settings",
    "identity": "identity",
    "bootstrap": "bootstrap",
    "consumer_state": "consumer_state",
    "frames_meta": "frames_meta",
    "onboarding_route": "onboarding_route",
    "model_api": "model_api",
}

# jsonl filename (without .jsonl) -> user_logs.stream
LOG_FILES = [
    "device_events",
    "gate_decisions",
    "gate_reviews",
    "proactive_jobs",
    "bootstrap_events",
    "identity_changes",
    "tracking_events",
    "memory_changes",
    "memory_capture_jobs",
]

# log streams whose entries carry a stable id used for in-place updates.
LOG_ITEM_KEYS = {"proactive_jobs": "job_id", "memory_capture_jobs": "job_id"}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"  ! failed to read {path.name}: {e}")
        return None


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"  ! failed to read {path.name}: {e}")
    return rows


def _entry_epoch(entry: dict):
    raw = entry.get("ts", entry.get("ts_epoch"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def migrate_pepper(data_dir: Path) -> bool:
    pepper_file = data_dir / ".pepper"
    if not pepper_file.exists():
        return False
    db.set_config("pepper", pepper_file.read_bytes())
    print("[pepper] imported from .pepper")
    return True


def migrate_users(data_dir: Path) -> list[dict]:
    users_file = data_dir / "users.json"
    users = _read_json(users_file) if users_file.exists() else []
    if not isinstance(users, list):
        users = []
    imported = 0
    for u in users:
        # Store the full user document verbatim; the backend normalizes the
        # shape (principal_id / api_keys[] / access bindings) on load.
        if isinstance(u, dict) and u.get("user_id"):
            db.insert_user(u)
            imported += 1
    print(f"[users] imported {imported} user(s)")
    return users


def migrate_global_blobs(data_dir: Path) -> None:
    """Global (non-per-user) JSON files → global_blobs."""
    p = data_dir / "access_link_tokens.json"
    if p.exists():
        data = _read_json(p)
        if isinstance(data, list):
            db.set_global_blob("access_link_tokens", data)
            print(f"[global] imported access_link_tokens ({len(data)} row(s))")


def migrate_user_dir(user_dir: Path, merge: bool = False) -> dict:
    user_id = user_dir.name
    counts = {"blobs": 0, "chat": 0, "memory": 0, "frames": 0, "logs": 0}

    # Default: clear any prior import for this user first (delete + reimport),
    # so RDS ends up exactly mirroring the files. In --merge mode we SKIP the
    # delete and only upsert/append — used to backfill missing data WITHOUT
    # wiping rows the live backend has written to RDS since the first migration.
    # Note: in merge mode the row-per-item tables (chat/memory/frames/blobs/
    # users) upsert by id (idempotent, no dupes), but the append-only user_logs
    # streams may gain duplicate rows for already-migrated users.
    if not merge:
        db.delete_user_data(user_id)

    # Singleton blobs.
    for stem, kind in BLOB_FILES.items():
        p = user_dir / f"{stem}.json"
        if p.exists():
            doc = _read_json(p)
            if doc is not None:
                db.set_blob(user_id, kind, doc)
                counts["blobs"] += 1

    # Chat (row-per-message; preserve file order, no trim during import).
    chat_file = user_dir / "chat.json"
    if chat_file.exists():
        msgs = _read_json(chat_file) or []
        for m in msgs:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            ts = m.get("ts")
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = 0.0
            db.chat_append(user_id, str(m["id"]), ts, m, max_messages=0)
            counts["chat"] += 1

    # Memory moments.
    mem_file = user_dir / "memory.json"
    if mem_file.exists():
        moments = _read_json(mem_file) or []
        for m in moments:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            db.memory_upsert(user_id, str(m["id"]), str(m.get("occurred_at") or ""), m)
            counts["memory"] += 1

    # Frame envelopes (ts from frames_meta index when available, else mtime).
    frames_dir = user_dir / "frames"
    if frames_dir.is_dir():
        meta = _read_json(user_dir / "frames_meta.json") if (user_dir / "frames_meta.json").exists() else []
        ts_by_id: dict[str, float] = {}
        if isinstance(meta, list):
            for entry in meta:
                if isinstance(entry, dict) and entry.get("id") is not None:
                    try:
                        ts_by_id[str(entry["id"])] = float(entry.get("ts") or 0.0)
                    except (TypeError, ValueError):
                        pass
        for p in sorted(frames_dir.glob("*.env.json")):
            env = _read_json(p)
            if not isinstance(env, dict) or not env.get("body_ct"):
                continue
            fid = str(env.get("id") or p.stem.split(".")[0])
            ts = ts_by_id.get(fid)
            if ts is None:
                try:
                    ts = p.stat().st_mtime
                except Exception:
                    ts = 0.0
            db.frame_upsert(user_id, fid, ts, env)
            counts["frames"] += 1

    # Append-only logs.
    for stream in LOG_FILES:
        p = user_dir / f"{stream}.jsonl"
        if not p.exists():
            continue
        key_field = LOG_ITEM_KEYS.get(stream)
        for entry in _read_jsonl(p):
            if not isinstance(entry, dict):
                continue
            item_key = (str(entry.get(key_field) or "") or None) if key_field else None
            db.log_append(user_id, stream, entry, ts=_entry_epoch(entry), item_key=item_key)
            counts["logs"] += 1

    # History-import jobs: one .json per job → one blob per job.
    hist_dir = user_dir / "history_import_jobs"
    if hist_dir.is_dir():
        for p in sorted(hist_dir.glob("*.json")):
            job = _read_json(p)
            if isinstance(job, dict) and job.get("job_id"):
                safe = re.sub(r"[^a-zA-Z0-9_-]", "", str(job["job_id"]))
                db.set_blob(user_id, f"history_import_job:{safe}", job)
                counts["blobs"] += 1

    print(
        f"[{user_id}] blobs={counts['blobs']} chat={counts['chat']} "
        f"memory={counts['memory']} frames={counts['frames']} logs={counts['logs']}"
    )
    return counts


def _user_dirs(data_dir: Path) -> list[Path]:
    return sorted(p for p in data_dir.glob("usr_*") if p.is_dir())


def verify(data_dir: Path) -> int:
    """Compare file counts against DB row counts. Returns a process exit code."""
    problems = 0
    for user_dir in _user_dirs(data_dir):
        user_id = user_dir.name
        chat_file = user_dir / "chat.json"
        file_chat = len(_read_json(chat_file) or []) if chat_file.exists() else 0
        db_chat = len(db.chat_load(user_id))
        mem_file = user_dir / "memory.json"
        file_mem = len(_read_json(mem_file) or []) if mem_file.exists() else 0
        db_mem = len(db.memory_load(user_id))
        frames_dir = user_dir / "frames"
        file_frames = len(list(frames_dir.glob("*.env.json"))) if frames_dir.is_dir() else 0
        db_frames = len(db.frame_list_meta(user_id))
        ok = (file_chat == db_chat) and (file_mem == db_mem) and (file_frames <= db_frames)
        flag = "OK " if ok else "!! "
        if not ok:
            problems += 1
        print(
            f"{flag}{user_id}: chat file={file_chat} db={db_chat} | "
            f"memory file={file_mem} db={db_mem} | frames file={file_frames} db={db_frames}"
        )
    print(f"\nverify: {problems} mismatch(es)")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate file persistence to PostgreSQL")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("FEEDLING_DATA_DIR", str(Path.home() / "feedling-data")),
        help="Legacy FEEDLING_DATA_DIR root (default: env or ~/feedling-data)",
    )
    parser.add_argument("--verify", action="store_true", help="Only compare counts; do not import")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if a previous migration already completed (ignores the migration_done marker).",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Backfill mode: do NOT delete existing rows first; only upsert/append. "
             "Use to add missing data without reverting rows the live backend has "
             "written since the first migration. Implies running despite the marker.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_dir():
        print(f"error: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    db.init_schema()

    if args.verify:
        return verify(data_dir)

    # Idempotency guard: once a migration has completed, never re-import on a
    # later run (e.g. CVM reboot re-running the one-shot migrate container) —
    # that would overwrite data users have since written to Postgres.
    if not args.force and not args.merge:
        done = db.get_config(_MIGRATION_DONE_KEY)
        if done:
            print(
                f"[migrate] already migrated ({_MIGRATION_DONE_KEY}="
                f"{done.decode(errors='replace')}) — skipping. Use --force to re-run."
            )
            return 0

    # Abort loudly if there are users but no pepper — importing users without
    # the matching pepper would silently invalidate every api_key.
    users_file = data_dir / "users.json"
    has_users = bool(_read_json(users_file)) if users_file.exists() else False
    pepper_ok = migrate_pepper(data_dir)
    if has_users and not pepper_ok:
        print(
            "error: users.json has entries but .pepper is missing — refusing to "
            "import, as api_key_hashes would no longer validate.",
            file=sys.stderr,
        )
        return 2

    migrate_users(data_dir)
    migrate_global_blobs(data_dir)

    if args.merge:
        print("[migrate] MERGE mode: existing rows kept, only upserting/appending.")
    totals = {"users": 0, "blobs": 0, "chat": 0, "memory": 0, "frames": 0, "logs": 0}
    for user_dir in _user_dirs(data_dir):
        c = migrate_user_dir(user_dir, merge=args.merge)
        totals["users"] += 1
        for k in ("blobs", "chat", "memory", "frames", "logs"):
            totals[k] += c[k]

    # Mark migration complete so future runs no-op (the one-shot migrate
    # container can stay in compose; reboots won't re-import).
    db.set_config(_MIGRATION_DONE_KEY, datetime.now().isoformat().encode())

    print(
        f"\n[done] users={totals['users']} blobs={totals['blobs']} chat={totals['chat']} "
        f"memory={totals['memory']} frames={totals['frames']} logs={totals['logs']}"
    )
    print(f"[migrate] set {_MIGRATION_DONE_KEY} — future runs no-op (use --force to re-run).")
    print("Filesystem left intact for rollback. Run with --verify to reconcile counts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""memory 退避（streak>=3）→ user_notices warning；恢复 resolve（spec Phase C / C3）。
Run:  python -m pytest tests/test_memory_backoff_notice.py -q
"""
from __future__ import annotations
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from core.store import get_store  # noqa: E402
from notices import core as notices_core  # noqa: E402
from proactive import capture_jobs  # noqa: E402
from proactive import capture_scheduler  # noqa: E402
from proactive import dream_scheduler  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _rows(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def test_capture_backoff_emits_only_at_streak_3():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE}
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 1
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 2
    assert "memory_backoff:capture" not in _rows(uid)                          # 前两次不发
    capture_scheduler.record_capture_job_status(store, job, status="failed")  # streak 3
    n = _rows(uid)["memory_backoff:capture"]
    assert n["source"] == "memory" and n["severity"] == "warning"
    assert "capture" in n["user_text"] and "3" in n["user_text"]              # 带 lane + streak


def test_capture_completed_resolves():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "j", "source": capture_jobs.CAPTURE_JOB_SOURCE}
    for _ in range(3):
        capture_scheduler.record_capture_job_status(store, job, status="failed")
    capture_scheduler.record_capture_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:capture"]["resolved"] is True


def test_migrate_backoff_emits_only_at_streak_3_and_resolves():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "m", "migrate_key": "mk", "source": capture_jobs.MIGRATE_JOB_SOURCE}
    capture_scheduler.record_migrate_job_status(store, job, status="failed")  # 1
    capture_scheduler.record_migrate_job_status(store, job, status="failed")  # 2
    assert "memory_backoff:migrate" not in _rows(uid)
    capture_scheduler.record_migrate_job_status(store, job, status="failed")  # 3
    n = _rows(uid)["memory_backoff:migrate"]
    assert n["source"] == "memory" and n["severity"] == "warning"
    assert "migrate" in n["user_text"] and "3" in n["user_text"]
    capture_scheduler.record_migrate_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:migrate"]["resolved"] is True


def test_dream_backoff_emits_only_at_streak_3_and_resolves():
    uid = _uid(); seed_user(uid); store = get_store(uid)
    job = {"job_id": "d", "dream_key": "dk", "source": capture_jobs.DREAM_JOB_SOURCE}
    dream_scheduler.record_dream_job_status(store, job, status="failed")  # 1
    dream_scheduler.record_dream_job_status(store, job, status="failed")  # 2
    assert "memory_backoff:dream" not in _rows(uid)
    dream_scheduler.record_dream_job_status(store, job, status="failed")  # 3
    n = _rows(uid)["memory_backoff:dream"]
    assert n["source"] == "memory" and n["severity"] == "warning"
    assert "dream" in n["user_text"] and "3" in n["user_text"]
    dream_scheduler.record_dream_job_status(store, job, status="completed")
    assert _rows(uid)["memory_backoff:dream"]["resolved"] is True

"""Background slow-path lifecycle for Proactive/Perception Runtime V2.

The worker owns only background computation and inbox re-entry. It has no chat
or push adapter, so completion can only surface as a `background_result` wake.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from proactive.runtime_v2 import (
    BackgroundLeaseRegistryV2,
    LeaseV2,
    RuntimeSpineV2,
    WakeEventV2,
)
from proactive.observability_v2 import (
    METRIC_BACKGROUND_JOB,
    MetricsSinkV2,
    record_metric_v2,
)

BACKGROUND_PENDING = "pending"
BACKGROUND_RUNNING = "running"
BACKGROUND_COMPLETED = "completed"
BACKGROUND_FAILED = "failed"


def _new_background_job_id() -> str:
    return "bg_" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class BackgroundJobV2:
    user_id: str
    request: Mapping[str, Any]
    job_id: str = field(default_factory=_new_background_job_id)
    status: str = BACKGROUND_PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = 0.0
    turn_id: str = ""
    wake_ids: tuple[str, ...] = ()
    origin_refs: tuple[str, ...] = ()
    lease_id: str = ""
    lease_owner_id: str = ""
    lease_expires_at: float = 0.0
    result: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackgroundRunResultV2:
    status: str
    job_id: str = ""
    job: Any | None = None
    lease: LeaseV2 | None = None
    wake_event: WakeEventV2 | None = None


class InMemoryBackgroundJobStoreV2:
    """In-memory background job store for contract tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[tuple[str, str], BackgroundJobV2] = {}

    def create_job(
        self,
        user_id: str,
        request: Mapping[str, Any],
        *,
        turn_id: str = "",
        wake_ids: tuple[str, ...] = (),
        origin_refs: tuple[str, ...] = (),
        now: float | None = None,
        job_id: str | None = None,
    ) -> BackgroundJobV2:
        now = time.time() if now is None else float(now)
        job = BackgroundJobV2(
            user_id=user_id,
            request=dict(request or {}),
            job_id=job_id or _new_background_job_id(),
            created_at=now,
            updated_at=now,
            turn_id=turn_id,
            wake_ids=tuple(wake_ids or ()),
            origin_refs=tuple(origin_refs or wake_ids or ()),
        )
        with self._lock:
            self._jobs[(user_id, job.job_id)] = job
        return job

    def get_job(self, user_id: str, job_id: str) -> BackgroundJobV2 | None:
        with self._lock:
            return self._jobs.get((user_id, job_id))

    def list_jobs(self, user_id: str) -> list[BackgroundJobV2]:
        with self._lock:
            return [job for (uid, _), job in self._jobs.items() if uid == user_id]

    def mark_running(
        self,
        user_id: str,
        job_id: str,
        lease: LeaseV2,
        *,
        now: float | None = None,
    ) -> BackgroundJobV2 | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            job = self._jobs.get((user_id, job_id))
            if job is None:
                return None
            if job.status == BACKGROUND_RUNNING and job.lease_expires_at > now:
                return None
            if job.status not in {BACKGROUND_PENDING, BACKGROUND_RUNNING}:
                return None
            running = replace(
                job,
                status=BACKGROUND_RUNNING,
                updated_at=now,
                lease_id=lease.lease_id,
                lease_owner_id=lease.owner_id,
                lease_expires_at=lease.expires_at,
            )
            self._jobs[(user_id, job_id)] = running
            return running

    def complete_job(
        self,
        user_id: str,
        job_id: str,
        lease: LeaseV2,
        result: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> BackgroundJobV2 | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            job = self._jobs.get((user_id, job_id))
            if job is None or job.status != BACKGROUND_RUNNING or job.lease_id != lease.lease_id:
                return None
            completed = replace(
                job,
                status=BACKGROUND_COMPLETED,
                updated_at=now,
                result=dict(result or {}),
            )
            self._jobs[(user_id, job_id)] = completed
            return completed

    def fail_job(
        self,
        user_id: str,
        job_id: str,
        lease: LeaseV2,
        error: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> BackgroundJobV2 | None:
        now = time.time() if now is None else float(now)
        with self._lock:
            job = self._jobs.get((user_id, job_id))
            if job is None or job.status != BACKGROUND_RUNNING or job.lease_id != lease.lease_id:
                return None
            failed = replace(
                job,
                status=BACKGROUND_FAILED,
                updated_at=now,
                error=dict(error or {}),
            )
            self._jobs[(user_id, job_id)] = failed
            return failed


def background_job_request_v2(job: Any) -> Mapping[str, Any]:
    request = getattr(job, "request", None)
    if isinstance(request, Mapping):
        return dict(request)
    doc = getattr(job, "doc", None)
    if isinstance(doc, Mapping) and isinstance(doc.get("request"), Mapping):
        return dict(doc.get("request") or {})
    return {}


def background_job_origin_refs_v2(job: Any) -> tuple[str, ...]:
    refs = getattr(job, "origin_refs", None)
    if refs:
        return tuple(str(item) for item in refs)
    wake_ids = getattr(job, "wake_ids", None)
    if wake_ids:
        return tuple(str(item) for item in wake_ids)
    doc = getattr(job, "doc", None)
    if isinstance(doc, Mapping):
        origin_refs = doc.get("origin_refs") or doc.get("wake_ids") or ()
        return tuple(str(item) for item in origin_refs)
    return ()


class BackgroundWorkerV2:
    """Run one background job and re-enter the wake inbox on completion."""

    def __init__(
        self,
        spine: RuntimeSpineV2,
        job_store: Any,
        *,
        run_background: Callable[[Any], Mapping[str, Any]] | None = None,
        background_leases: BackgroundLeaseRegistryV2 | None = None,
        metrics_sink: MetricsSinkV2 | None = None,
        lease_ttl_sec: float = 600.0,
        owner_id: str = "background_worker_v2",
    ) -> None:
        self.spine = spine
        self.job_store = job_store
        self.run_background = run_background or (lambda job: {"status": "ok", "request": background_job_request_v2(job)})
        self.background_leases = background_leases or BackgroundLeaseRegistryV2()
        self.metrics_sink = metrics_sink if metrics_sink is not None else getattr(spine, "metrics_sink", None)
        self.lease_ttl_sec = float(lease_ttl_sec)
        self.owner_id = owner_id

    def _record_job_metric(
        self,
        user_id: str,
        job_id: str,
        *,
        status: str,
        now: float,
        wake_submitted: bool = False,
    ) -> None:
        record_metric_v2(
            self.metrics_sink,
            user_id=user_id,
            name=METRIC_BACKGROUND_JOB,
            tags={"status": status},
            data={
                "background_job_id": job_id,
                "wake_submitted": bool(wake_submitted),
            },
            ts=now,
        )

    def run_job(
        self,
        user_id: str,
        job_id: str,
        *,
        now: float | None = None,
        owner_id: str | None = None,
    ) -> BackgroundRunResultV2:
        now = time.time() if now is None else float(now)
        owner = owner_id or self.owner_id
        lease = self.background_leases.try_acquire_job(
            job_id,
            user_id=user_id,
            owner_id=owner,
            now=now,
            ttl_sec=self.lease_ttl_sec,
        )
        if lease is None:
            self._record_job_metric(user_id, job_id, status="busy", now=now)
            return BackgroundRunResultV2(status="busy", job_id=job_id)

        try:
            running = self.job_store.mark_running(user_id, job_id, lease, now=now)
            if running is None:
                self._record_job_metric(user_id, job_id, status="not_claimed", now=now)
                return BackgroundRunResultV2(status="not_claimed", job_id=job_id, lease=lease)
            try:
                result = dict(self.run_background(running) or {})
            except Exception as e:
                failed = self.job_store.fail_job(
                    user_id,
                    job_id,
                    lease,
                    {"error": type(e).__name__, "message": str(e)[:240]},
                    now=now,
                )
                self._record_job_metric(user_id, job_id, status="failed", now=now)
                return BackgroundRunResultV2(status="failed", job_id=job_id, job=failed, lease=lease)

            completed = self.job_store.complete_job(user_id, job_id, lease, result, now=now)
            if completed is None:
                self._record_job_metric(user_id, job_id, status="completion_lost", now=now)
                return BackgroundRunResultV2(status="completion_lost", job_id=job_id, lease=lease)

            event = WakeEventV2(
                user_id=user_id,
                source="background_result",
                trigger="background_result",
                created_at=now,
                origin_refs=background_job_origin_refs_v2(completed),
                background_payload={
                    "background_job_id": job_id,
                    "turn_id": str(getattr(completed, "turn_id", "") or ""),
                    "request": background_job_request_v2(completed),
                    "result": result,
                },
            )
            self.spine.submit(event)
            self._record_job_metric(user_id, job_id, status=BACKGROUND_COMPLETED, now=now, wake_submitted=True)
            return BackgroundRunResultV2(
                status=BACKGROUND_COMPLETED,
                job_id=job_id,
                job=completed,
                lease=lease,
                wake_event=event,
            )
        finally:
            self.background_leases.release(lease)

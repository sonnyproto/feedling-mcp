"""tee-pg: append-only TEE shadow sync-run history (observability)

Revision ID: 0015_tee_sync_runs
Revises: 0014_model_api_profiles
Create Date: 2026-07-13

The TEE shadow sync pipeline (mirror / reconcile / replicate / verify) only ever
emitted its reports to Python logging, and the scheduler compressed each full
report into two or three numbers before dropping it into a log line. Nothing was
persisted, so there was no way to watch convergence, replication lag, dual-write
failures or TEE liveness *over time* — exactly what the cut-read go/no-go
decision (spec §5/§7: consistency verified + soak ≥7 days) needs.

This table gives the in-process auto-sync scheduler (backend/admin/
tee_sync_scheduler.py) one append-only row per tick: flattened metric columns
for cheap trend/alerting queries plus a ``report`` JSONB carrying the full
per-table reconcile/replicate/verify detail for drill-down.

It lives in **RDS, not the TEE db, on purpose**: a row must be recordable even
when the TEE shadow is unreachable ("tee_healthy=false" is itself a data point
that can only be written outside the thing being observed).

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_tee_sync_runs"
down_revision = "0014_model_api_profiles"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS tee_sync_runs (
    id                     BIGSERIAL PRIMARY KEY,
    ran_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    did_reconcile          BOOLEAN NOT NULL DEFAULT false,
    reconcile_ok           BOOLEAN,          -- null = reconcile not attempted this tick
    verify_ran             BOOLEAN NOT NULL DEFAULT false,
    verify_ok              BOOLEAN,          -- null = verify not run this tick
    unconverged_tables     INTEGER,          -- from verify; null when verify didn't run
    unconverged_users      INTEGER,
    requeue_backlog        INTEGER,
    replicate_copied       INTEGER NOT NULL DEFAULT 0,
    replicate_pending      INTEGER NOT NULL DEFAULT 0,
    replicate_errors       INTEGER NOT NULL DEFAULT 0,
    replicate_skipped      INTEGER NOT NULL DEFAULT 0,
    reconcile_copied       INTEGER NOT NULL DEFAULT 0,
    reconcile_pruned       INTEGER NOT NULL DEFAULT 0,
    reconcile_skipped      INTEGER NOT NULL DEFAULT 0,
    mirror_failures        INTEGER NOT NULL DEFAULT 0,   -- cumulative snapshot (zeroes on restart)
    mirror_failures_delta  INTEGER NOT NULL DEFAULT 0,   -- vs previous persisted row, clamped >=0
    tee_healthy            BOOLEAN NOT NULL DEFAULT false,
    tee_probe_ms           DOUBLE PRECISION,
    duration_ms            DOUBLE PRECISION,
    report                 JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS tee_sync_runs_ran_at_idx ON tee_sync_runs (ran_at DESC);
"""

_DROP = """
DROP TABLE IF EXISTS tee_sync_runs;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)

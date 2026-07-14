"""Freeze completed Beijing-day DAU aggregates.

Revision ID: 0017_dau_daily_snapshot
Revises: 0016_tee_sync_table_failures
Create Date: 2026-07-14

DAU used to be recomputed only from surviving per-user rows. Account deletion
therefore changed historical totals. This table stores one immutable row per
completed Beijing day; writers use ``ON CONFLICT DO NOTHING``.
"""

from alembic import op


revision = "0017_dau_daily_snapshot"
down_revision = "0016_tee_sync_table_failures"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS dau_daily_snapshot (
    day              TEXT PRIMARY KEY,
    dau              INTEGER NOT NULL DEFAULT 0,
    chat_dau         INTEGER NOT NULL DEFAULT 0,
    tracking_dau     INTEGER NOT NULL DEFAULT 0,
    active_events    INTEGER NOT NULL DEFAULT 0,
    user_messages    INTEGER NOT NULL DEFAULT 0,
    tracking_events  INTEGER NOT NULL DEFAULT 0,
    session_dau      INTEGER NOT NULL DEFAULT 0,
    avg_session_sec  DOUBLE PRECISION NOT NULL DEFAULT 0,
    foreground_sec   BIGINT NOT NULL DEFAULT 0,
    session_count    INTEGER NOT NULL DEFAULT 0,
    first_ts         DOUBLE PRECISION,
    last_ts          DOUBLE PRECISION,
    frozen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dau_daily_snapshot_day_format
        CHECK (day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
);
CREATE INDEX IF NOT EXISTS dau_daily_snapshot_frozen_at_idx
    ON dau_daily_snapshot (frozen_at DESC);
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dau_daily_snapshot")

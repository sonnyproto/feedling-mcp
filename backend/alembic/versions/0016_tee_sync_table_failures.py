"""tee-pg: count whole-table replicate failures in the sync-run metrics

Revision ID: 0016_tee_sync_table_failures
Revises: 0015_tee_sync_runs
Create Date: 2026-07-14

The first live tee_sync_runs row exposed an under-counting blind spot: when a
whole ciphertext table's replicate raises (e.g. the TEE direct-TLS connection
drops mid-write — "SSL error: unexpected eof" / "the connection is lost"), the
scheduler's generic except swallowed it into a log line only — the table
vanished from the report and was NOT counted in ``replicate_errors`` (which
only sums the per-row ``errors`` field of a *successful* run). So a row showing
``replicate_errors=2`` actually hid two entire tables (chat_messages,
memory_moments) failing outright.

This adds a first-class counter for whole-table replicate failures so the panel
stops undercounting; the per-table error strings go into the ``report`` JSONB
(see tee_sync_scheduler) for drill-down.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_tee_sync_table_failures"
down_revision = "0015_tee_sync_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tee_sync_runs "
        "ADD COLUMN IF NOT EXISTS replicate_table_failures INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tee_sync_runs DROP COLUMN IF EXISTS replicate_table_failures")

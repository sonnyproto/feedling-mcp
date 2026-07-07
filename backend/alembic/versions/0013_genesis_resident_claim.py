"""genesis resident-claim columns (VPS resident distill).

Resident-distill jobs live in status ``awaiting_resident`` (which the CVM worker's
``uploaded`` claim never touches). The resident consumer claims them atomically;
these columns track who claimed, when, last heartbeat, and attempt count so a dead
consumer's job can be reaped / re-queued instead of wedging forever.

Revision ID: 0013_genesis_resident_claim
"""
from alembic import op

revision = "0013_genesis_resident_claim"
down_revision = "0012_per_user_cascade_fk"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE genesis_import_jobs
    ADD COLUMN IF NOT EXISTS resident_consumer_id  TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS resident_claimed_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resident_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resident_attempts     INTEGER NOT NULL DEFAULT 0;
"""

_DOWN = """
ALTER TABLE genesis_import_jobs
    DROP COLUMN IF EXISTS resident_consumer_id,
    DROP COLUMN IF EXISTS resident_claimed_at,
    DROP COLUMN IF EXISTS resident_heartbeat_at,
    DROP COLUMN IF EXISTS resident_attempts;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)

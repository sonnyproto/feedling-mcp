"""extended perception: perception_items collection table

Revision ID: 0002_perception_items
Revises: 0001_baseline
Create Date: 2026-06-08

Extended Perception stores its singleton per-user state in user_blobs
(kinds: perception_state / perception_permissions / perception_config /
perception_user_state) and its change/wake audit trail in user_logs
(stream "perception_events") — neither needs a migration. This revision
adds the one new table the feature needs: a generic per-user collection
for row-per-item perception data (photos, calendar events, workout/sleep
summaries) that needs time-ordering, upsert, and TTL cleanup.

doc is JSONB holding the item's metadata. For photos, doc ALSO carries the
encrypted content envelope (build_envelope output) — the backend never holds
plaintext pixels; only the enclave decrypts when the agent pulls content.

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_perception_items"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS perception_items (
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- 'photo' | 'calendar' | 'workout' | 'sleep' | 'vitals'
    item_id    TEXT NOT NULL,
    ts         DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION,           -- optional TTL; NULL = keep
    doc        JSONB NOT NULL,
    PRIMARY KEY (user_id, kind, item_id)
);
CREATE INDEX IF NOT EXISTS perception_items_user_kind_ts_idx
    ON perception_items (user_id, kind, ts DESC);
CREATE INDEX IF NOT EXISTS perception_items_expires_idx
    ON perception_items (expires_at) WHERE expires_at IS NOT NULL;
"""

_DROP = """
DROP TABLE IF EXISTS perception_items;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)

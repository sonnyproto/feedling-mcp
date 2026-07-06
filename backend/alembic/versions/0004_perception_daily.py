"""quantitative perception history: perception_daily rollup table

Tier 2 of the perception-history spec (docs/PERCEPTION_HISTORY_SPEC, since removed — see git history) — one structured summary-stats doc per
(user, device-local date, signal). The incremental daily aggregation
(backend/perception/history.py) folds each report into the day's row so the
agent can finally sense change-vs-baseline ("RHR up ~14% vs your 30d median")
instead of only the current snapshot.

doc is JSONB holding the per-shape running stats (numeric min/max/sum/count,
cumulative totals, duration-by-state minutes, deduped event lists, …). Never
holds raw samples — only the agent-read-optimized daily aggregate. Stored
plaintext, same as perception_state (see HISTORY_SPEC §8; backend-key
encryption is an open follow-up, not this migration).

DDL is idempotent (IF NOT EXISTS) to match the baseline's safety property.
"""

from alembic import op

revision = "0004_perception_daily"
down_revision = "0003_drop_perception_permissions"
branch_labels = None
depends_on = None


_DDL = """
CREATE TABLE IF NOT EXISTS perception_daily (
    user_id    TEXT NOT NULL,
    date       TEXT NOT NULL,              -- device-local date 'YYYY-MM-DD'
    signal     TEXT NOT NULL,              -- canonical catalog signal key
    doc        JSONB NOT NULL,             -- per-shape running summary stats
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, date, signal)
);
CREATE INDEX IF NOT EXISTS perception_daily_user_signal_date_idx
    ON perception_daily (user_id, signal, date DESC);
"""

_DROP = """
DROP TABLE IF EXISTS perception_daily;
"""


def upgrade() -> None:
    op.execute(_DDL)


def downgrade() -> None:
    op.execute(_DROP)

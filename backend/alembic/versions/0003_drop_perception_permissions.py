"""drop the legacy perception_permissions user_blobs rows

Revision ID: 0003_drop_perception_permissions
Revises: 0002_perception_items
Create Date: 2026-06-09

Extended Perception no longer stores explicit permission toggles. Authorization
is now implicit in reported data (see docs/superpowers/specs/
2026-06-09-perception-permissions-implicit-design.md): a /report capability is
"granted" while its latest reported value is non-null; all other capabilities
are always available. The perception_permissions blob is dead data — remove it.

Idempotent: DELETE on a missing/empty set is a no-op. Irreversible: the toggles
carried no information that the new model needs, so downgrade is a no-op.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_drop_perception_permissions"
down_revision = "0002_perception_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM user_blobs WHERE kind = 'perception_permissions';")


def downgrade() -> None:
    # No-op: the removed toggles carry no information the new model can restore.
    pass

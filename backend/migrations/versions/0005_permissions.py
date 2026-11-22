"""Add per-run write scope and approval mode."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "permission_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default='{"write_scope":"project","approval_mode":"request"}',
        ),
    )
    op.alter_column("runs", "permission_policy", server_default=None)


def downgrade() -> None:
    op.drop_column("runs", "permission_policy")

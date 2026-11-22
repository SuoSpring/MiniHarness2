"""会话工作树、归档状态与可复用工作目录。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("workspace_mode", sa.String(16), nullable=False, server_default="isolated"),
    )
    op.alter_column("sessions", "workspace_mode", server_default=None)
    op.add_column("sessions", sa.Column("working_dir", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("workspace_snapshot", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in ("archived_at", "workspace_snapshot", "working_dir", "workspace_mode"):
        op.drop_column("sessions", name)

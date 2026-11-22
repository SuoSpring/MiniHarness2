"""记录正式记忆最近一次写入时间。"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute(
        "UPDATE memories SET updated_at = runs.finished_at FROM runs "
        "WHERE memories.source_run_id = runs.id AND runs.finished_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("memories", "updated_at")

"""工作台删除标记；保留已经合并的共享快照和验收引用。"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("projects", "sessions"):
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for table in ("sessions", "projects"):
        op.drop_column(table, "deleted_at")

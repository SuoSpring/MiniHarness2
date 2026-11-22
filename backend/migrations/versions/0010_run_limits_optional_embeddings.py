"""逐次运行限制与未配置嵌入模型时的关键词记忆。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("limits", JSONB(), nullable=False, server_default="{}"))
    op.alter_column("memories", "embedding", nullable=True)
    op.alter_column("memory_drafts", "embedding", nullable=True)


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("memories", "memory_drafts"):
        if connection.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE embedding IS NULL")):
            raise RuntimeError("存在关键词记忆，不能在降级时丢弃或伪造其向量。")
    op.alter_column("memory_drafts", "embedding", nullable=False)
    op.alter_column("memories", "embedding", nullable=False)
    op.drop_column("runs", "limits")

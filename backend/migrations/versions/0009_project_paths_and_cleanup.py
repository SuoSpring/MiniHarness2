"""项目目录，以及独立于来源会话保留的共享测试。"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("workspace_path", sa.Text(), nullable=True))
    op.alter_column("acceptance_suites", "run_id", nullable=True)


def downgrade() -> None:
    # 共享测试已经没有原执行记录，不能通过降级悄悄丢弃。
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM acceptance_suites WHERE run_id IS NULL")):
        raise RuntimeError("已有保留的项目测试，无法降级为必须绑定执行记录。")
    op.alter_column("acceptance_suites", "run_id", nullable=False)
    op.drop_column("projects", "workspace_path")

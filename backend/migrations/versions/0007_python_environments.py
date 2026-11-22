"""项目 Python 环境与后端默认环境设置。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("python_environment", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.alter_column("projects", "python_environment", server_default=None)
    op.create_table(
        "application_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("application_settings")
    op.drop_column("projects", "python_environment")

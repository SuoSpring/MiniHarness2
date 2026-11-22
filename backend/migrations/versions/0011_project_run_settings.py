"""项目级预算与单次模型输出上限。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("run_settings", JSONB(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("projects", "run_settings")

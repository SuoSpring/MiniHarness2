"""在当前运行中记住用户明确授权的同类操作。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("approval_grants", JSONB(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("runs", "approval_grants")

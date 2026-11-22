"""Add keyword retrieval and deferred, batched memory preparation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    for table in ("memories", "memory_drafts"):
        op.add_column(
            table,
            sa.Column(
                "keywords",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
        op.alter_column(table, "keywords", server_default=None)
    op.add_column(
        "runs",
        sa.Column(
            "memory_preparation",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("runs", "memory_preparation", server_default=None)
    op.add_column(
        "boundaries",
        sa.Column("candidate_cursor", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.alter_column("boundaries", "candidate_cursor", server_default=None)
    op.create_table(
        "memory_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("session_id", sa.String(32)),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("keywords", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_memory_candidates_run_id", "memory_candidates", ["run_id"])


def downgrade() -> None:
    op.drop_table("memory_candidates")
    op.drop_column("boundaries", "candidate_cursor")
    op.drop_column("runs", "memory_preparation")
    for table in ("memory_drafts", "memories"):
        op.drop_column(table, "keywords")

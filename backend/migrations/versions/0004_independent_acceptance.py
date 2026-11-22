"""Freeze independent acceptance contracts and bind verification to their versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    for table, name, default in (
        ("projects", "acceptance_suite_ids", "[]"),
        ("verifications", "suite_ids", "[]"),
        ("verifications", "scope", "{}"),
    ):
        op.add_column(
            table,
            sa.Column(
                name,
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text(f"'{default}'::jsonb"),
            ),
        )
        op.alter_column(table, name, server_default=None)
    op.add_column(
        "verifications", sa.Column("suite_hash", sa.String(64), nullable=False, server_default="")
    )
    op.alter_column("verifications", "suite_hash", server_default=None)
    op.create_table(
        "acceptance_suites",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("contract", postgresql.JSONB(), nullable=False),
        sa.Column("contract_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("retirement_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_acceptance_suites_project_id", "acceptance_suites", ["project_id"])
    op.create_index("ix_acceptance_suites_run_id", "acceptance_suites", ["run_id"])


def downgrade() -> None:
    op.drop_table("acceptance_suites")
    op.drop_column("projects", "acceptance_suite_ids")
    for name in ("suite_ids", "suite_hash", "scope"):
        op.drop_column("verifications", name)

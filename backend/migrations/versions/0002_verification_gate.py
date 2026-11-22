"""Persist required checks and framework-owned verification results."""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    default = {
        "checks": [
            {"id": "project", "kind": "python", "path": "main.py", "args": [], "min_tests": 1},
            {"id": "tests", "kind": "pytest", "path": "tests", "args": [], "min_tests": 1},
        ],
        "timeout_seconds": 30,
    }
    op.add_column(
        "projects",
        sa.Column(
            "verification_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'" + json.dumps(default) + "'::jsonb"),
        ),
    )
    op.alter_column("projects", "verification_policy", server_default=None)
    op.create_table(
        "verifications",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id")),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("snapshot", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("environment_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("checks", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_verifications_project_id", "verifications", ["project_id"])
    op.create_index("ix_verifications_run_id", "verifications", ["run_id"])


def downgrade() -> None:
    op.drop_table("verifications")
    op.drop_column("projects", "verification_policy")

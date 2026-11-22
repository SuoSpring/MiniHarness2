"""仅将完整旧出厂配置切换为按计划运行入口。"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"

TESTS = {"id": "tests", "kind": "pytest", "path": "tests", "args": [], "min_tests": 1}
OLD = {
    "checks": [
        {"id": "project", "kind": "python", "path": "main.py", "args": [], "min_tests": 1},
        TESTS,
    ],
    "timeout_seconds": 30,
}
NEW = {"checks": [TESTS], "timeout_seconds": 30}


def replace_default(before, after):
    statement = sa.text(
        "UPDATE projects SET verification_policy = CAST(:after AS jsonb), revision = revision + 1 "
        "WHERE verification_policy = CAST(:before AS jsonb)"
    ).bindparams(before=json.dumps(before), after=json.dumps(after))
    op.execute(statement)


def upgrade() -> None:
    replace_default(OLD, NEW)


def downgrade() -> None:
    replace_default(NEW, OLD)

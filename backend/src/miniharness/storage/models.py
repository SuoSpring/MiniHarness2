from datetime import UTC, datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from miniharness.permissions import default_permissions
from miniharness.verification.contracts import default_policy


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workspace_path: Mapped[str | None] = mapped_column(Text)
    public_snapshot: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=0)
    skill_settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    python_environment: Mapped[dict] = mapped_column(JSONB, default=dict)
    run_settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    verification_policy: Mapped[dict] = mapped_column(JSONB, default=default_policy)
    acceptance_suite_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApplicationSetting(Base):
    __tablename__ = "application_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workspace_mode: Mapped[str] = mapped_column(String(16), default="isolated")
    working_dir: Mapped[str | None] = mapped_column(Text)
    workspace_snapshot: Mapped[str | None] = mapped_column(String(64))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    head_snapshot: Mapped[str] = mapped_column(String(64))
    base_snapshot: Mapped[str] = mapped_column(String(64))
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    pinned: Mapped[list] = mapped_column(JSONB, default=list)
    active_run_id: Mapped[str | None] = mapped_column(String(32))
    undo_run_id: Mapped[str | None] = mapped_column(String(32))
    crash_notes: Mapped[list] = mapped_column(JSONB, default=list)
    revision: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    task: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16), default="react")
    permission_policy: Mapped[dict] = mapped_column(JSONB, default=default_permissions)
    approval_grants: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")
    start_snapshot: Mapped[str] = mapped_column(String(64))
    end_snapshot: Mapped[str | None] = mapped_column(String(64))
    start_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    memory_preparation: Mapped[dict] = mapped_column(JSONB, default=dict)
    interrupt: Mapped[dict | None] = mapped_column(JSONB)
    instructions: Mapped[list] = mapped_column(JSONB, default=list)
    token_budget: Mapped[int] = mapped_column(default=0)
    limits: Mapped[dict] = mapped_column(JSONB, default=dict)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    working_dir: Mapped[str] = mapped_column(Text)
    pause_requested: Mapped[bool] = mapped_column(default=False)
    generation: Mapped[int] = mapped_column(default=0)
    last_operation: Mapped[dict] = mapped_column(JSONB, default=dict)
    merged: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Verification(Base):
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    suite_ids: Mapped[list] = mapped_column(JSONB, default=list)
    suite_hash: Mapped[str] = mapped_column(String(64), default="")
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)
    task_revision: Mapped[int] = mapped_column(default=0)
    environment_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="running")
    checks: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AcceptanceSuite(Base):
    __tablename__ = "acceptance_suites"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    # 删除源会话后，被公共版本或其他会话引用的测试转为项目资产。
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    task_revision: Mapped[int] = mapped_column(Integer)
    scope: Mapped[dict] = mapped_column(JSONB)
    scope_key: Mapped[str] = mapped_column(String(64))
    contract: Mapped[dict] = mapped_column(JSONB)
    contract_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="draft")
    active: Mapped[bool] = mapped_column(default=True)
    retirement_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageEntry(Base):
    __tablename__ = "usage_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    estimated: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Boundary(Base):
    __tablename__ = "boundaries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    snapshot: Mapped[str] = mapped_column(String(64))
    state: Mapped[dict] = mapped_column(JSONB)
    draft_cursor: Mapped[int] = mapped_column(BigInteger, default=0)
    candidate_cursor: Mapped[int] = mapped_column(BigInteger, default=0)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    embedding: Mapped[list | None] = mapped_column(Vector(), nullable=True)
    revision: Mapped[int] = mapped_column(default=1)
    source_run_id: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index(
            "uq_memory_scope_key",
            "project_id",
            "session_id",
            "key",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )


class MemoryDraft(Base):
    __tablename__ = "memory_drafts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    memory_id: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str | None] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    embedding: Mapped[list | None] = mapped_column(Vector(), nullable=True)
    expected_revision: Mapped[int] = mapped_column(default=0)
    revision: Mapped[int] = mapped_column(default=1)
    before: Mapped[dict | None] = mapped_column(JSONB)
    published: Mapped[bool] = mapped_column(default=False)


class MemoryCandidate(Base):
    """执行中仅登记意向；嵌入、判重和正式草稿都在调用结束时准备。"""

    __tablename__ = "memory_candidates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)


class Contribution(Base):
    __tablename__ = "contributions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    before_snapshot: Mapped[str] = mapped_column(String(64))
    after_snapshot: Mapped[str] = mapped_column(String(64))
    reverted: Mapped[bool] = mapped_column(default=False)


class Preview(Base):
    __tablename__ = "previews"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    kind: Mapped[str] = mapped_column(String(16))
    project_revision: Mapped[int] = mapped_column(Integer)
    session_revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)


class MCPServer(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(default=True)

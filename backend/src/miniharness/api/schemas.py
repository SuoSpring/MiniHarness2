from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from miniharness.permissions import PermissionPolicy
from miniharness.runtime.environments import EnvironmentSelection
from miniharness.runtime.limits import RunLimits


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    environment: EnvironmentSelection | None = None
    workspace_path: str | None = None


class ProjectPath(Input):
    workspace_path: str = Field(min_length=1)


class SessionCreate(Input):
    title: str = Field(default="新会话", min_length=1, max_length=200)
    workspace_mode: Literal["isolated", "worktree"] = "isolated"


class SessionUpdate(Input):
    pinned: list[str]


class AcceptanceRetirement(Input):
    reason: str = Field(min_length=1, max_length=1000)


class RunCreate(Input):
    task: str = Field(min_length=1)
    mode: Literal["react", "plan"] = "react"
    # 0 表示不限制累计词元；省略或 null 使用项目默认预算。
    token_budget: int | None = Field(default=None, ge=0, le=2147483647)
    permission_policy: PermissionPolicy = Field(default_factory=PermissionPolicy)
    limits: RunLimits | None = None


class Resume(Input):
    answer: Any = True
    token_budget: int | None = Field(default=None, ge=0, le=2147483647)
    limits: RunLimits | None = None


class Approval(Input):
    plan_version: int
    decision: Literal["approve", "changes", "reject"]
    feedback: str = ""


class ToolApproval(Input):
    request_id: str = Field(min_length=64, max_length=64)
    decision: Literal["approve", "approve_similar", "reject"]


class Criterion(Input):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class SpecificationUpdate(Input):
    goal: str | None = Field(default=None, min_length=1)
    criteria: list[Criterion] | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        if self.goal is None and self.criteria is None:
            raise ValueError("请填写新的目标或验收标准。")
        ids = [criterion.id for criterion in self.criteria or []]
        if len(ids) != len(set(ids)) or "goal" in ids:
            raise ValueError("验收标准标识必须唯一，goal 保留给原始目标。")
        return self


class Instruction(Input):
    text: str = Field(min_length=1)
    spec_update: SpecificationUpdate | None = None


class PreviewInput(Input):
    resolutions: dict[str, Any] = Field(default_factory=dict)


class ApplyPreview(PreviewInput):
    token: str
    memory_resolutions: dict[str, Literal["keep", "restore"]] = Field(default_factory=dict)


class Replace(RunCreate):
    token: str | None = None
    resolutions: dict[str, Any] = Field(default_factory=dict)
    memory_resolutions: dict[str, Literal["keep", "restore"]] = Field(default_factory=dict)


class MemoryWrite(Input):
    key: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    session_id: str | None = None
    expected_revision: int | None = None
    keywords: list[str] | None = Field(default=None, max_length=12)


class MCPConfig(Input):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=60)
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class Enabled(Input):
    enabled: bool

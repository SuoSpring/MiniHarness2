import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .acceptance import INDEPENDENT_CHECK


class RequiredCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: Literal["python", "pytest"]
    path: str = Field(min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=50)
    min_tests: int = Field(default=1, ge=1)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        value = value.replace("\\", "/")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or ":" in value or "\0" in value:
            raise ValueError("检查入口必须是项目内的相对路径")
        if not path.parts:
            raise ValueError("请指定具体脚本或测试目录")
        return value

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if self.kind == "pytest" and self.args:
            raise ValueError("测试检查不接受额外参数，避免跳过必需用例")
        if any("\0" in arg for arg in self.args):
            raise ValueError("参数不能包含空字符")
        return self


class VerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[RequiredCheck] = Field(min_length=1, max_length=20)
    timeout_seconds: int = Field(default=30, ge=1, le=300)

    @model_validator(mode="after")
    def required_kinds(self) -> Self:
        if len({check.id for check in self.checks}) != len(self.checks):
            raise ValueError("检查编号必须唯一")
        if not any(check.kind == "pytest" for check in self.checks):
            raise ValueError("至少需要一个 pytest 测试检查")
        return self


def default_policy() -> dict:
    return VerificationPolicy(
        checks=[RequiredCheck(id="tests", kind="pytest", path="tests")]
    ).model_dump()


def legacy_default_policy() -> dict:
    """升级前已保存的出厂值，用于识别仍持有旧配置的检查点。"""
    return VerificationPolicy(
        checks=[
            RequiredCheck(id="project", kind="python", path="main.py"),
            RequiredCheck(id="tests", kind="pytest", path="tests"),
        ],
        timeout_seconds=30,
    ).model_dump()


def policy_hash(policy: dict) -> str:
    canonical = VerificationPolicy.model_validate(policy).model_dump()
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def gate_passed(
    result: dict, snapshot: str, policy: dict, task_revision: int, scope: dict | None = None
) -> bool:
    """不接受空集、缺项、重复项、旧版本或由模型单独宣称的整体通过。"""
    final = scope.get("final", True) if scope else True
    required = (
        {check.id for check in VerificationPolicy.model_validate(policy).checks} if final else set()
    )
    required.add(INDEPENDENT_CHECK)
    checks = result.get("checks", [])
    return (
        result.get("status") == "passed"
        and result.get("snapshot") == snapshot
        and result.get("policy_hash") == policy_hash(policy)
        and result.get("task_revision") == task_revision
        and bool(result.get("suite_ids"))
        and bool(result.get("suite_hash"))
        and (
            result.get("scope") == scope if scope else result.get("scope", {}).get("final") is True
        )
        and len(checks) == len(required)
        and {check.get("id") for check in checks} == required
        and all(check.get("status") == "passed" for check in checks)
    )


def independent_aliases(checks: list[dict]) -> set[str]:
    """A default project entry can reuse the independent result, or fail its own minimum."""
    independent = next((c for c in checks if c.get("id") == INDEPENDENT_CHECK), None)
    if independent is None:
        return set()
    return {
        c["id"]
        for c in checks
        if c.get("id")
        and c.get("origin") == "独立生成的验收测试"
        and all(
            c.get(k) == independent.get(k)
            for k in ("status", "error", "case_failures", "collection_failures")
        )
    }

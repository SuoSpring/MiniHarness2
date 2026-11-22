"""项目保存默认预算，每次运行固定快照；容量与累计限制的 0 均表示不额外设限。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from miniharness.config import Settings


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(default=0, ge=0, le=2147483647)
    max_tool_calls: int = Field(default=0, ge=0, le=2147483647)
    run_timeout: float = Field(default=0, ge=0, allow_inf_nan=False)
    llm_timeout: float = Field(default=180, ge=0, allow_inf_nan=False)
    max_output_tokens: int = Field(default=0, ge=0, le=2147483647)
    max_context_tokens: int = Field(default=262144, ge=0, le=2147483647)
    compression_threshold: float = Field(default=0.95, ge=0, le=1)

    @model_validator(mode="after")
    def validate_window(self) -> "RunLimits":
        if 0 < self.max_context_tokens < 1024:
            raise ValueError("上下文窗口必须为 0（无上限）或至少 1024 个词元。")
        # 部分更新先校验字段本身，合并项目/运行设置后再比较完整窗口。
        if (
            {"max_context_tokens", "max_output_tokens"} <= self.model_fields_set
            and self.max_context_tokens
            and self.max_output_tokens >= self.max_context_tokens
        ):
            raise ValueError("上下文窗口必须大于单次输出上限，或将其中一项设为无上限。")
        return self

    @classmethod
    def resolve(cls, settings: Settings, overrides: dict | None = None) -> "RunLimits":
        return cls.model_validate(
            {**{name: getattr(settings, name) for name in cls.model_fields}, **(overrides or {})}
        )


class ProjectRunSettings(RunLimits):
    token_budget: int = Field(default=0, ge=0, le=2147483647)

    @classmethod
    def resolve(cls, settings: Settings, overrides: dict | None = None) -> "ProjectRunSettings":
        return cls.model_validate(
            {
                **{name: getattr(settings, name) for name in RunLimits.model_fields},
                "token_budget": settings.max_run_tokens,
                **(overrides or {}),
            }
        )

    def run_limits(self) -> dict:
        return self.model_dump(exclude={"token_budget"})

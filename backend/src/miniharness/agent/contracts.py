"""图状态、模型输出与运行服务之间的明确契约。"""

import re
from dataclasses import InitVar, dataclass
from typing import Any, Literal, NotRequired, Protocol, Self, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolCall, messages_to_dict
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from miniharness.context.manager import FileEditGroup, review_turn_history
from miniharness.verification.acceptance import MAX_PLAN_ENTRIES, PlanEntryCheck
from miniharness.verification.contracts import default_policy, independent_aliases
from miniharness.verification.corrections import Finding, IndependentTestFix, ProjectTestFix


def json_response_text(text: str) -> str:
    """兼容解释文字后单个完整的 JSON 代码块，含多个候选时仍由校验器拒绝。"""
    text = text.strip()
    if text.startswith(("{", "[")) or text.count("```") != 2:
        return text
    if text.startswith("```") and text.endswith("```"):
        return re.sub(r"^```(?:json\b)?\s*", "", text[:-3], flags=re.IGNORECASE).strip()
    block = re.search(r"(?im)^\s*```(?:json)?[ \t]*\r?\n([\s\S]+?)\r?\n[ \t]*```\s*$", text)
    return block.group(1).strip() if block else text


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def validation_feedback(error: ValueError) -> str:
    if not isinstance(error, ValidationError):
        return str(error)
    labels = {
        "missing": "缺少必填字段",
        "extra_forbidden": "多余字段，请删除或移入说明",
        "int_type": "需要整数",
        "list_type": "需要数组",
        "string_type": "需要字符串",
        "bool_parsing": "需要布尔值 true 或 false",
    }
    lines = []
    for issue in error.errors(include_url=False, include_input=False):
        path = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in issue["loc"]
        ).lstrip(".")
        lines.append(f"{path or '根对象'} — {labels.get(issue['type'], issue['msg'])}")
    return "\n".join(lines)


class PlanCriterion(Contract):
    id: str = Field(min_length=1)
    description: str = Field(
        default="", description="新增条目写明可检验的要求，已有编号可省略描述以沿用原文。"
    )
    step_ids: list[str] = Field(min_length=1, description="本条标准依赖的全部步骤 ID。")


class PlanStep(Contract):
    id: str = Field(min_length=1)
    title: str = Field(
        min_length=1, description="简短的工作名称，写动作与交付物，如“接入界面错误反馈”。"
    )


class PlanDraft(Contract):
    steps: list[PlanStep] = Field(min_length=1)
    criteria: list[PlanCriterion] = Field(default_factory=list)
    entry_checks: list[PlanEntryCheck] | None = Field(
        default=None,
        max_length=MAX_PLAN_ENTRIES,
        description="有脚本入口时给出按需求可执行的样例，列清参数、退出码和已约定的输出。纯库或无脚本交付可填 []。重规划省略表示沿用同一需求版本的入口样例。",
    )
    rationale: str = ""
    changed_assumption: str = ""
    change_scope: Literal["step", "plan"] = Field(
        default="plan",
        description="step 仅限当前一步的工作说明调整，保持顺序和条目关联。首次计划、重大变更或范围不明用 plan。",
    )


class PlanningOption(Contract):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)


class PlanningQuestion(Contract):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    options: list[PlanningOption] = Field(default_factory=list, max_length=4)
    recommended_option_id: str = ""

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        if len(self.options) == 1:
            raise ValueError("澄清问题可不提供选项；选择题需要 2–4 个选项")
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("同一问题的选项 ID 必须唯一")
        if self.recommended_option_id and self.recommended_option_id not in ids:
            raise ValueError("推荐选项必须属于当前问题")
        return self


class PlanningResponse(PlanDraft):
    task_kind: Literal["answer", "coding"] = "coding"
    steps: list[PlanStep] = Field(
        default_factory=list, description="首次规划填全部步骤；重规划只填待重做的后缀。"
    )
    questions: list[PlanningQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if bool(self.steps) == bool(self.questions):
            raise ValueError(
                "请输出本次规划范围的 steps 或待用户选择的 questions，二者必须且只能有一个"
            )
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("规划问题 ID 必须唯一")
        return self


class UserQuestions(Contract):
    questions: list[PlanningQuestion] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_questions(self) -> Self:
        if len({item.id for item in self.questions}) != len(self.questions):
            raise ValueError("问题 ID 必须唯一")
        return self


class PlanningAnswer(Contract):
    question_id: str = Field(min_length=1)
    option_id: str = ""
    text: str = ""


class PlanningAnswers(Contract):
    request_id: str = Field(min_length=1)
    answers: list[PlanningAnswer] = Field(min_length=1, max_length=3)


class ReviewNotes(Contract):
    # 审查说明可扩展。决策、验收编号、证据仍分别做类型和业务校验。
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def keep_extra_reason(cls, value):
        if isinstance(value, dict) and isinstance(value.get("reason_extra"), str):
            value = value.copy()
            extra = value.pop("reason_extra").strip()
            if extra and isinstance(value.get("reason", ""), str):
                return {**value, "reason": "\n".join(filter(None, (value.get("reason"), extra)))}
        return value


class CriterionCheck(ReviewNotes):
    criterion_id: str = Field(
        description="只能原样引用当前验收标准中已有的 id；不得新增、改名或重复。"
    )
    passed: bool
    evidence_ids: list[str] = Field(
        default_factory=list, description="引用本轮资料中的工具事件编号，按原样填写。"
    )
    reason: str = Field(
        default="",
        description="判定依据与补充说明全部写在这里。",
    )


class Review(ReviewNotes):
    model_config = ConfigDict(extra="forbid")
    task_kind: Literal["answer", "coding"] | None = None
    decision: Literal["complete", "next", "repair", "replan", "blocked"] = Field(
        description="repair=返回执行节点，补齐缺失的实现或修复已发现的问题；"
        "next=当前计划步骤已通过且确有下一步，必须同时 step_complete=true；"
        "complete=全部要求已完成，申请整体验收；replan=现有方案失效，需要重新规划；"
        "blocked=无法自行解决的外部阻塞。"
    )
    summary: str = Field(
        description="给用户的最终答复。用户限定只要某个字段时只交付该字段，不附原始回执、未要求的字段或验收过程。核对依据填在 checks。"
    )
    checks: list[CriterionCheck] = Field(default_factory=list)
    step_complete: bool = Field(
        default=False,
        description="当前计划步骤是否已完成。next 必须显式填 true；未开始、缺文件或缺少验证时填 false 并选择 repair。",
    )
    reason: str = ""


class FailedReview(Review):
    """正式验收失败时，先核对测试，再选择返修对象。"""

    decision: Literal["repair_tests", "repair", "replan", "blocked"]
    findings: list[Finding] = Field(min_length=1)
    correction: IndependentTestFix | ProjectTestFix | None = None

    @staticmethod
    def diagnosis_errors(gate: dict, data: dict) -> list[str]:
        """Check independent facts even if another response field is invalid."""
        failed = {c["id"] for c in gate["checks"] if c["status"] != "passed"}
        aliases = independent_aliases(gate["checks"])
        rows = data.get("findings")
        rows = rows if isinstance(rows, list) else []
        found = {
            f["check_id"]: f.get("cause")
            for f in rows
            if isinstance(f, dict) and isinstance(f.get("check_id"), str)
        }
        errors = []
        if (failed - aliases) - found.keys() or found.keys() - failed or len(found) != len(rows):
            errors.append(
                f"请逐项核查所有失败检查并各填写一次 findings，检查编号为 {sorted(failed - aliases)}。"
                "需求编号填在 checks 中。"
            )
        faulty = {key for key, cause in found.items() if cause in ("test", "mixed")}
        decision, fix = data.get("decision"), data.get("correction")
        if decision == "repair_tests":
            if fix is None:
                errors.append("请给出待修正的独立断言或项目测试片段 correction")
            elif isinstance(fix, dict):
                target = "@independent" if fix.get("kind") == "independent" else fix.get("check_id")
                if not isinstance(target, str) or target not in faulty:
                    errors.append("修正目标必须是已经判定测试有误的失败检查")
        elif fix is not None or faulty:
            errors.append("测试有误时先选择 repair_tests 并提交 correction，修正后复测同一实现")
        elif decision == "blocked" and found and all(c == "implementation" for c in found.values()):
            errors.append("已确认实现缺陷，请交回执行节点修复或在计划模式重规划")
        elif decision in ("repair", "replan") and not all(
            c == "implementation" for c in found.values()
        ):
            errors.append("仅在每项失败都确认测试符合规格、根因在实现时才能交回执行或重规划")
        return errors

    def check_diagnosis(self, gate: dict) -> None:
        if errors := self.diagnosis_errors(gate, self.model_dump()):
            raise ValueError("\n".join(errors))


@dataclass(frozen=True)
class GraphConfig:
    context_tokens: int = 262144
    output_tokens: int = 0
    keep_message_groups: int = 3
    memory_context_tokens: int = 2000
    memory_auto_share: float = 0.4
    anchor_attempts: int = 2
    executor_rounds: InitVar[int | None] = None  # 兼容旧调用方的构造参数，路由以成果报告为准。
    max_tool_calls: int = 0
    max_model_rounds: int = 0
    compression_threshold: float = 0.95
    validation_attempts: int = 3
    no_progress_rounds: int = 3
    repeated_review_rounds: int = 3


class GraphState(TypedDict, total=False):
    continuation: dict[str, Any]
    project_id: str
    session_id: str
    run_id: str
    task: str
    task_revision: int
    spec_history: list[dict[str, Any]]
    mode: str
    permission_policy: dict[str, Any]
    messages: list[dict[str, Any]]
    prior_review_turns: list[dict[str, Any]]
    file_edit_receipts: list[FileEditGroup]
    message_role: str
    summary: str
    pinned: list[str]
    crash_notes: list[str]
    memory_context: list[str]
    recalled_memory_ids: list[str]
    user_updates: list[str]
    applied_instruction_ids: list[str]
    pending_instructions: list[dict[str, Any]]
    pending_requirement_updates: list[dict[str, Any]]
    requirement_base: dict[str, Any]
    requirement_error: str
    protected_ids: list[str]
    goal_message_id: str
    status: str
    summary_output: str
    next_node: str
    task_kind: str
    files_changed: bool
    criteria: list[dict[str, str]]
    plan: dict[str, Any]
    pending_plan: dict[str, Any]
    approved_plan: dict[str, Any]
    step_index: int
    replan_anchor: int | None
    anchor_attempts: dict[str, int]
    failures: list[dict[str, Any]]
    boundaries: dict[str, str] | list[str]
    checkpoint_steps: list[int]
    verified_step: int
    file_revision: int
    snapshot_revision: int
    tool_event_ids: list[int]
    evidence: list[dict[str, Any]]  # 仅供恢复旧检查点；新运行只保存活动分支的事件引用。
    log_outcomes: dict[str, dict[str, Any]]
    snapshot_id: str
    verification_policy: dict[str, Any]
    verification: dict[str, Any]
    verification_issues: list[dict[str, Any]]
    seen_verification_id: str
    verification_requested: bool
    baseline_suite_ids: list[str]
    acceptance_suite_ids: list[str]
    test_contract_floor: int
    test_scope: dict[str, Any]
    test_design_repair: dict[str, Any]
    test_job: dict[str, Any]
    pending_review: dict[str, Any]
    failed_gate_review: dict[str, Any]
    review: dict[str, Any]
    review_messages: list[dict[str, Any]]
    review_summary: str
    review_refresh_notice: str
    report: str
    feedback: str
    model_retry: dict[str, Any]
    repair_watch: dict[str, Any]
    review_watch: dict[str, Any]
    stall: dict[str, Any]
    questions: list[str]
    planning_questions: list[dict[str, Any]]
    planning_request_id: str
    pending_tools: list[ToolCall]
    tool_role: str
    review_runs_tests: bool
    available_tools: list[dict[str, Any]]
    prepared_messages: list[dict[str, Any]]
    model_rounds: int
    tool_calls: int
    sequence: int
    execution_epoch: int
    pause_requested: bool
    input_reason: str
    input_return: str


class GraphInstruction(TypedDict):
    id: str
    text: str
    spec_update: NotRequired[dict[str, object]]
    received_at: NotRequired[float]


class GraphControl(TypedDict, total=False):
    pause: bool
    instructions: list[str | GraphInstruction]


class GraphServices(Protocol):
    async def model(
        self,
        role: str,
        state: GraphState,
        messages: list[BaseMessage],
        *,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage | dict[str, Any]: ...

    async def tools(self, state: GraphState, *, readonly: bool) -> list[dict[str, Any]]: ...

    async def execute_tool_event(
        self,
        name: str,
        arguments: dict[str, Any],
        state: GraphState,
        *,
        readonly: bool = False,
    ) -> tuple[dict[str, Any], int]: ...

    def read_source(self, name: str, arguments: dict) -> dict: ...
    async def record_tool_event(self, payload: dict) -> int: ...
    async def tool_events(self, identifiers: list[int]) -> list[dict]: ...

    async def snapshot(self, state: GraphState) -> str: ...
    async def verify(self, state: GraphState) -> dict[str, Any]: ...
    async def design_tests(self, state: GraphState, scope: dict[str, Any]) -> dict[str, Any]: ...
    async def compile_tests(self, state: GraphState, identifier: str) -> dict[str, Any]: ...
    async def failed_test_sources(self, state: GraphState) -> dict[str, Any]: ...
    async def correct_test(self, state: GraphState) -> dict[str, Any]: ...
    async def save_boundary(self, state: GraphState, *, snapshot: str | None = None) -> str: ...
    async def restore_boundary(self, boundary_id: str, state: GraphState) -> GraphState: ...
    async def control(self, state: GraphState) -> GraphControl: ...
    async def event(self, kind: str, payload: dict[str, object]) -> None: ...


def initial_state(
    *,
    project_id: str,
    session_id: str,
    run_id: str,
    task: str,
    mode: str = "react",
    permission_policy: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    summary: str = "",
    pinned: list[str] | None = None,
    crash_notes: list[str] | None = None,
    memory_context: list[str] | None = None,
    verification_policy: dict[str, Any] | None = None,
    baseline_suite_ids: list[str] | None = None,
) -> GraphState:
    if mode not in {"react", "plan"}:
        raise ValueError("mode 必须是 react 或 plan")
    message = HumanMessage(content=task, id=f"{run_id}:task")
    return {
        "project_id": project_id,
        "session_id": session_id,
        "run_id": run_id,
        "task": task,
        "task_revision": 1,
        "spec_history": [],
        "mode": mode,
        "permission_policy": permission_policy or {},
        "messages": [*(messages or []), *messages_to_dict([message])],
        "prior_review_turns": review_turn_history(messages or []),
        "file_edit_receipts": [],
        "message_role": "planner" if mode == "plan" else "executor",
        "summary": summary,
        "pinned": pinned or [],
        "crash_notes": crash_notes or [],
        "memory_context": memory_context or [],
        "user_updates": [],
        "recalled_memory_ids": [],
        "applied_instruction_ids": [],
        "pending_instructions": [],
        "pending_requirement_updates": [],
        "requirement_base": {},
        "requirement_error": "",
        "protected_ids": [message.id],
        "goal_message_id": message.id,
        "status": "running",
        "summary_output": "",
        "next_node": "planner" if mode == "plan" else "executor",
        "task_kind": "unknown",
        "files_changed": False,
        "criteria": [{"id": "goal", "description": task}],
        "plan": {},
        "pending_plan": {},
        "approved_plan": {},
        "step_index": 0,
        "replan_anchor": None,
        "anchor_attempts": {},
        "failures": [],
        "boundaries": {},
        "checkpoint_steps": [],
        "verified_step": 0,
        "file_revision": 0,
        "snapshot_revision": 0,
        "tool_event_ids": [],
        "log_outcomes": {},
        "snapshot_id": "",
        "verification_policy": verification_policy or default_policy(),
        "verification": {},
        "verification_issues": [],
        "seen_verification_id": "",
        "verification_requested": False,
        "baseline_suite_ids": baseline_suite_ids or [],
        "acceptance_suite_ids": baseline_suite_ids or [],
        "test_contract_floor": 1,
        "test_scope": {},
        "test_design_repair": {},
        "test_job": {},
        "pending_review": {},
        "review": {},
        "review_messages": [],
        "review_summary": "",
        "review_refresh_notice": "",
        "report": "",
        "feedback": "",
        "model_retry": {},
        "repair_watch": {},
        "review_watch": {},
        "stall": {},
        "questions": [],
        "planning_questions": [],
        "planning_request_id": "",
        "pending_tools": [],
        "tool_role": "executor",
        "review_runs_tests": True,
        "available_tools": [],
        "prepared_messages": [],
        "model_rounds": 0,
        "tool_calls": 0,
        "sequence": 0,
        "execution_epoch": 0,
        "pause_requested": False,
        "input_reason": "",
        "input_return": "planner" if mode == "plan" else "executor",
    }

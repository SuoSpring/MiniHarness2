"""记录用户输入；由规划或反思依据用户更新整理有效需求。"""

import json
from typing import Literal

from langchain_core.messages import HumanMessage, messages_to_dict
from pydantic import BaseModel, ConfigDict, Field

from miniharness.milestones import ordered_steps, plan_criteria, rollback_anchor

from .contracts import GraphState


class UserCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class UserRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    goal: str | None = Field(default=None, min_length=1)
    criteria: list[UserCriterion] | None = None


class RequirementResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    goal: str = Field(min_length=1)
    criteria: list[UserCriterion]
    kind: Literal["answer", "coding"]
    replan_anchor: int = Field(default=0, ge=0)


def apply_user_input(
    state: GraphState,
    text: str,
    identifier: str,
    spec_update: dict[str, object] | None = None,
) -> GraphState:
    """普通输入只记录消息，不把自然语言机械地变成额外验收条目。"""
    goal = state["task"]
    criteria = list(state["criteria"])
    pending = list(state.get("pending_requirement_updates", []))
    base = state.get("requirement_base", {})
    if spec_update is None or pending:
        if not pending:
            base = {"goal": goal, "criteria": criteria}
        pending.append({"id": identifier, "text": text, "spec_update": spec_update})
    if spec_update is not None:
        revision = UserRevision.model_validate(spec_update)
        if revision.goal is None and revision.criteria is None:
            raise ValueError("需求修订至少需要 goal 或 criteria")
        goal = revision.goal if revision.goal is not None else goal
        if revision.criteria is not None:
            criteria = [item.model_dump() for item in revision.criteria]
            ids = [item["id"] for item in criteria]
            if len(set(ids)) != len(ids) or "goal" in ids:
                raise ValueError("验收标准 ID 必须唯一；修改目标请使用 goal 字段")
        criteria = [
            {"id": "goal", "description": goal},
            *[item for item in criteria if item["id"] != "goal"],
        ]
    if not text:
        text = "用户已通过需求修订入口更新目标或验收标准。"
    if spec_update is not None:
        # 修订的结构化内容也属于用户要求，收尾折叠后仍须能识别替换了什么。
        text += "\n用户明确修订（替换此前对应目标或验收标准）：\n" + json.dumps(
            revision.model_dump(exclude_none=True), ensure_ascii=False
        )
    message = HumanMessage(content=text, id=f"{state['run_id']}:instruction:{identifier}")
    goal_message_id = state["goal_message_id"]
    protected = list(state["protected_ids"])
    if spec_update is not None:
        if revision.goal is not None:
            goal_message_id = message.id
        # 已被用户替换的目标/标准不再强制保留原文；仍生效的标准在固定区原样保留。
        # 旧消息不删除，继续留在审计历史中，活动上下文可正常将其合入摘要。
        protected = [
            goal_message_id,
            *[
                f"{state['run_id']}:instruction:{item['id'][2:]}"
                for item in criteria
                if item["id"].startswith("u:")
            ],
        ]
    number = state["task_revision"] + 1
    snapshot = {
        "revision": state["task_revision"],
        "goal": state["task"],
        "criteria": state["criteria"],
    }
    return {
        "task": goal,
        "criteria": criteria,
        "task_revision": number,
        "spec_history": [*state["spec_history"], snapshot],
        "messages": [*state["messages"], *messages_to_dict([message])],
        "protected_ids": list(dict.fromkeys([*protected, message.id])),
        "goal_message_id": goal_message_id,
        "user_updates": [*state["user_updates"], text],
        "pending_requirement_updates": pending,
        "requirement_base": base,
        "requirement_error": "",
        "review": {},
        "review_messages": [],
        "review_summary": "",
        "repair_watch": {},
        "review_watch": {},
        "stall": {},
        "model_retry": {},
        "verification_issues": [],
        "tool_event_ids": [],
        **({"evidence": []} if "evidence" in state else {}),
        "verification": {},
        "seen_verification_id": "",
        "verification_requested": False,
        "test_job": {},
        "test_scope": {},
        "test_design_repair": {},
        "pending_review": {},
        "failed_gate_review": {},
        "test_contract_floor": number
        if spec_update is not None
        else state.get("test_contract_floor", 1),
        "pending_plan": {},
        "approved_plan": {},
        "planning_questions": [],
        "planning_request_id": "",
        "anchor_attempts": {},
        "next_node": "planner" if state["mode"] == "plan" else "executor",
        "status": "running",
        "summary_output": "",
        "report": "",
        "replan_anchor": 0
        if spec_update is not None and state["plan"]
        else rollback_anchor(state, state["step_index"])
        if state["plan"]
        else None,
        "feedback": "收到用户更新，请按时间顺序理解最新要求；已被替换的旧要求不再适用。",
    }


def apply_requirement_resolution(
    state: GraphState, resolution: RequirementResolution
) -> GraphState:
    """只能消费尚未整理的用户输入；不接收实现、运行结果或模型自述作为修订依据。"""
    pending = state.get("pending_requirement_updates", [])
    if not pending:
        raise ValueError("没有待处理的用户更新，不能重新定义需求")
    criteria = [item.model_dump() for item in resolution.criteria]
    ids = [item["id"] for item in criteria]
    if len(ids) != len(set(ids)) or "goal" in ids:
        raise ValueError("验收标准 ID 必须唯一；goal 标准由目标自动生成")
    criteria = [{"id": "goal", "description": resolution.goal}, *criteria]
    base = state["requirement_base"]
    before = {item["id"]: item["description"] for item in base["criteria"]}
    after = {item["id"]: item["description"] for item in criteria}
    replaced = {key for key, value in before.items() if after.get(key) != value}
    anchor = None
    if state["mode"] == "plan" and state["plan"]:
        if resolution.replan_anchor > state["step_index"]:
            raise ValueError("重规划锚点不能越过当前步骤")
        anchor = rollback_anchor(state, resolution.replan_anchor)
        retained = {step["id"] for step in state["plan"]["steps"][:anchor]}
        for item in plan_criteria(state["plan"], base["criteria"]):
            if retained.intersection(item["step_ids"]) and item["id"] in replaced:
                raise ValueError("保留步骤的验收依据已改变，请选择更早的重规划锚点")
    goal_id = state["goal_message_id"]
    obsolete = {
        f"{state['run_id']}:instruction:{key[2:]}" for key in replaced if key.startswith("u:")
    }
    if resolution.goal != base["goal"]:
        obsolete.add(goal_id)
        goal_id = f"{state['run_id']}:instruction:{pending[-1]['id']}"
    protected = [key for key in state["protected_ids"] if key not in obsolete]
    return {
        "task": resolution.goal,
        "criteria": criteria,
        "task_kind": "coding" if state.get("files_changed") else resolution.kind,
        "pending_requirement_updates": [],
        "requirement_base": {},
        "requirement_error": "",
        "goal_message_id": goal_id,
        "protected_ids": list(dict.fromkeys([*protected, goal_id])),
        "replan_anchor": anchor,
        "test_contract_floor": state["task_revision"]
        if replaced
        else state.get("test_contract_floor", 1),
        "feedback": "已根据最新用户要求更新有效目标与验收标准。",
    }


def is_local_step_change(state: GraphState) -> bool:
    """只有当前步骤的工作说明可局部调整，步骤顺序、条目及其依赖都必须相同。"""
    if state["replan_anchor"] != state["step_index"]:
        return False
    old, new = state["plan"], state["pending_plan"]
    if old.get("entry_checks", []) != new.get("entry_checks", []):
        return False
    if old.get("task_revision", 1) != state["task_revision"] or len(old["steps"]) != len(
        new["steps"]
    ):
        return False
    old_links = {
        item["id"]: set(item["step_ids"]) for item in plan_criteria(old, state["criteria"])
    }
    if old_links != {item["id"]: set(item["step_ids"]) for item in new["criteria"]}:
        return False
    for index, (before, after) in enumerate(zip(ordered_steps(old), new["steps"], strict=True)):
        if index == state["step_index"]:
            if before["id"] != after["id"]:
                return False
            continue
        if before != after:
            return False
    return True

"""规划中的设计选择：校验当前问题的回答，形成可保留的用户要求。"""

import os
from collections import Counter
from typing import Any

from miniharness.milestones import plan_criteria
from miniharness.verification.programs import contains_number

from .contracts import GraphState, PlanCriterion, PlanDraft, PlanningAnswers


def planning_requirements(state: GraphState) -> list[dict]:
    """尚未规划的任务原文可以同时含功能与流程要求，只把已确定的功能条目交给规划者绑定。"""
    if state["plan"] and any(
        item["id"] == "goal" for item in plan_criteria(state["plan"], state["criteria"])
    ):
        return state["criteria"]
    return [
        item for item in state["criteria"] if item != {"id": "goal", "description": state["task"]}
    ]


def bind_criteria(
    steps: list[dict],
    proposed: list[PlanCriterion],
    required: list[dict],
    *,
    previous: dict,
    anchor: int,
    allow_new: bool,
) -> list[dict]:
    """验证完整的验收表，在重规划时复用已保留步骤及其已验收的条目。"""
    order = {step["id"]: index for index, step in enumerate(steps)}
    retained = {step["id"] for step in steps[:anchor]}
    if len(order) != len(steps):
        counts = Counter(step["id"] for step in steps)
        duplicates = {key for key, count in counts.items() if count > 1}
        reason = f"重复步骤 ID={sorted(duplicates)}"
        if retained & duplicates:
            reason += f"。本次提交重复使用了保留前缀中的 {sorted(retained & duplicates)}；框架已保留这些步骤，steps 只需提交待重做的后缀。"
        raise ValueError(reason)
    descriptions = {item["id"]: item["description"] for item in required}
    old_links = {
        item["id"]: set(item.get("step_ids", [])) for item in plan_criteria(previous, required)
    }
    bindings = {}
    for item in proposed:
        if item.id in bindings:
            raise ValueError(f"重复的验收编号={item.id}")
        if item.id in descriptions:
            if item.description and item.description != descriptions[item.id]:
                raise ValueError(f"规划不能改写已有验收标准 {item.id}；目标修订必须来自用户")
        elif allow_new and item.description:
            descriptions[item.id] = item.description
        else:
            raise ValueError(f"未知验收编号={[item.id]}。新增条目须在首次规划声明具体要求。")
        links = set(item.step_ids)
        if len(links) != len(item.step_ids) or not links <= order.keys():
            raise ValueError(f"条目 {item.id} 的步骤编号重复或不存在：{item.step_ids}")
        old = old_links.get(item.id, set())
        if retained and (
            links & retained != old & retained
            or (links <= retained) != (bool(old) and old <= retained)
        ):
            raise ValueError(f"条目 {item.id} 改变了保留前缀的验收范围，请选择更早的回退边界")
        bindings[item.id] = links
    for key in descriptions.keys() - bindings.keys():
        old = old_links.get(key, set())
        if old and old <= retained:
            bindings[key] = old
    if missing := sorted(descriptions.keys() - bindings.keys()):
        raise ValueError(f"遗漏的验收编号={missing}。请为每个条目列出全部关联步骤 step_ids。")
    if not bindings:
        raise ValueError("完整计划至少需要一条有明确通过条件且关联到步骤的验收条目。")
    return [
        {
            "id": key,
            "description": description,
            "step_ids": sorted(bindings[key], key=order.__getitem__),
        }
        for key, description in descriptions.items()
    ]


def plan_entry_checks(
    draft: PlanDraft, previous: dict, criteria: list[dict], policy: dict
) -> list[dict]:
    approved = previous.get("entry_checks", [])
    entries = (
        approved
        if draft.entry_checks is None
        else [item.for_approval() for item in draft.entry_checks]
    )
    known = {item["id"] for item in criteria}
    fixed_commands = {
        (os.path.normcase(check["path"]), tuple(check.get("args", []))): check["id"]
        for check in policy.get("checks", [])
        if check["kind"] == "python"
    }
    for entry in entries:
        if (
            entry not in approved
            and contains_number(entry.get("stdout"))
            and not entry.get("stdout_reference")
        ):
            raise ValueError(
                "新计划的精确数值输出须用 stdout_reference 计算，或交给独立验收驱动计算"
            )
        if entry["criterion_id"] not in known:
            raise ValueError(f"入口 {entry['path']} 关联了不存在的验收条目 {entry['criterion_id']}")
        args = entry.get("args", [])
        fixed_id = fixed_commands.get((os.path.normcase(entry["path"]), tuple(args)))
        if fixed_id and not entry.get("stdin") and entry["exit_code"] != 0:
            raise ValueError(
                f"入口 {entry['path']} 参数 {args} 与固定检查 {fixed_id} 是同一次调用，"
                f"固定检查要求退出 0，计划期望 {entry['exit_code']}。请对齐调用场景，有需求冲突时请用户选择。"
            )
    return entries


def planning_request(state: GraphState) -> dict[str, Any]:
    return {
        "status": "awaiting_input",
        "kind": "plan_clarification",
        "return_node": state.get("input_return", "planner"),
        "run_id": state["run_id"],
        "request_id": state["planning_request_id"],
        "questions": state["planning_questions"],
        "reason": "请回答当前问题。",
        "feedback": state.get("feedback", ""),
    }


def planning_answer_text(request: dict[str, Any], answer: object) -> str:
    if not isinstance(answer, dict):
        raise ValueError("请回答当前规划问题；可以选择选项或填写自己的方案。")
    parsed = PlanningAnswers.model_validate(answer)
    if parsed.request_id != request["request_id"]:
        raise ValueError("规划问题已变化，请刷新后回答当前问题。")
    questions = {question["id"]: question for question in request["questions"]}
    ids = [item.question_id for item in parsed.answers]
    if len(ids) != len(set(ids)) or set(ids) != set(questions):
        raise ValueError("请逐一回答当前的全部规划问题，不能重复或引用旧问题。")
    answers = {item.question_id: item for item in parsed.answers}
    lines = ["用户对当前问题的回答："]
    for identifier, question in questions.items():
        item = answers[identifier]
        options = {option["id"]: option for option in question["options"]}
        if item.option_id and item.option_id not in options:
            raise ValueError("所选方案不属于当前问题，请刷新后重新选择。")
        if not item.option_id and not item.text:
            raise ValueError("每个问题需要选择一个方案或填写自定义回答。")
        lines.append(f"问题：{question['question']}")
        if item.option_id:
            option = options[item.option_id]
            lines.append(f"用户选择：{option['label']}。{option['description']}")
        if item.text:
            lines.append(f"用户补充：{item.text}")
    return "\n".join(lines)

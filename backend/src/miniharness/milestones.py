"""文件回退点按已完成步数编号；工具活动只使用轻量修订计数。"""

import math


def boundary_map(state: dict) -> dict[str, str]:
    boundaries = state.get("boundaries", {})
    if isinstance(boundaries, list):
        # 兼容旧检查点：旧列表的下标就是当时已完成的步数。
        return {str(index): value for index, value in enumerate(boundaries) if value}
    return dict(boundaries)


def boundary_positions(state: dict) -> list[int]:
    return sorted(int(position) for position in boundary_map(state))


def rollback_anchor(state: dict, requested: int) -> int:
    return max(
        (position for position in boundary_positions(state) if position <= requested), default=0
    )


def checkpoint_schedule(count: int, completed: int = 0) -> list[int]:
    if not count:
        return []
    return sorted(
        {
            math.ceil(count * part / 3)
            for part in (1, 2, 3)
            if math.ceil(count * part / 3) > completed
        }
        | {count}
    )


def planned_checkpoints(state: dict) -> list[int]:
    return state.get("checkpoint_steps") or checkpoint_schedule(
        len(state.get("plan", {}).get("steps", []))
    )


def active_plan(state: dict) -> dict:
    plan = state.get("plan", {})
    if state.get("mode", "plan") == "plan" and plan.get("task_revision", 1) == state.get(
        "task_revision", 1
    ):
        return plan
    return {}


def plan_criteria(plan: dict, requirements: list[dict]) -> list[dict]:
    """读取条目到步骤的关联。旧检查点只在每个步骤上存了反向关联。"""
    criteria = plan.get("criteria", requirements)
    if not plan.get("steps") or all("step_ids" in item for item in criteria):
        return criteria
    return [
        {
            **item,
            "step_ids": [
                step["id"] for step in plan["steps"] if item["id"] in step.get("criterion_ids", [])
            ],
        }
        for item in criteria
    ]


def ordered_steps(plan: dict) -> list[dict]:
    return [
        {"id": step["id"], "title": step.get("title") or step.get("description") or step["id"]}
        for step in plan.get("steps", [])
    ]


def review_criteria(state: dict, *, final: bool = False) -> list[dict]:
    plan = active_plan(state)
    steps = plan.get("steps", [])
    if not steps:
        return state["criteria"]
    criteria = plan_criteria(plan, state["criteria"])
    through = state.get("step_index", 0) + 1
    if final or through >= len(steps):
        return criteria
    if through not in planned_checkpoints(state):
        return []
    completed = {step["id"] for step in steps[:through]}
    new_steps = {step["id"] for step in steps[state.get("verified_step", 0) : through]}
    return [
        item
        for item in criteria
        if completed.issuperset(item["step_ids"]) and new_steps.intersection(item["step_ids"])
    ]


def current_snapshot(state: dict) -> str:
    if state.get("snapshot_revision", 0) == state.get("file_revision", 0):
        return state.get("snapshot_id", "")
    return ""


def evidence_current(state: dict, item: dict) -> bool:
    if item.get("tool") == "verification" and "acceptance_suite_ids" in state:
        # 纠正独立断言会保留产品快照，旧契约的检查只能作为历史证据。
        if not set(item.get("suite_ids", [])).issubset(state["acceptance_suite_ids"]):
            return False
    if "file_revision" in item:
        return item["file_revision"] == state.get("file_revision", 0)
    snapshot = current_snapshot(state)
    return bool(snapshot) and item.get("snapshot") == snapshot


def may_change_files(name: str) -> bool:
    # 未知外部工具、脚本和命令都按可能写入处理，失败也可能已经留下修改。
    return name not in {
        "ask_user",
        "read_file",
        "search_file",
        "list_files",
        "read_log",
        "read_execution_result",
        "list_logs",
        "recall_memory",
        "remember",
        "update_memory",
        "list_skills",
        "get_python_environment",
        "load_skill",
    }

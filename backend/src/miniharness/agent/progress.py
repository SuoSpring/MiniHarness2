"""使用内容与可观察结果判断停滞；请求次数、工具编号和改写措辞都不算进展。"""

import hashlib
import json


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def repair_progress(state: dict, snapshot: str) -> dict:
    gate = state.get("verification", {})
    signature = fingerprint(
        {
            "task_revision": state.get("task_revision"),
            "step_index": state.get("step_index"),
            "snapshot": snapshot,
            "verification": {
                "status": gate.get("status"),
                "suite_hash": gate.get("suite_hash"),
                "checks": [
                    {key: check.get(key) for key in ("id", "status", "exit_code", "case_failures")}
                    for check in gate.get("checks", [])
                ],
            },
        }
    )
    previous = state.get("repair_watch", {})
    rounds = previous.get("rounds", 0) + 1 if previous.get("signature") == signature else 1
    return {"signature": signature, "rounds": rounds, "snapshot": snapshot}


def review_observation(
    previous: dict,
    name: str,
    arguments: dict,
    result: dict,
    *,
    last: bool,
    already_shared: bool = False,
) -> dict:
    # 每个工具批次算一轮；仅 ID/计时变化不能把重复观察伪装成新信息。
    ignored = {"tool_call_id", "operation_id", "log_id", "duration", "log_observed"}
    observation = fingerprint(
        {
            "name": name,
            "arguments": {
                key: value for key, value in arguments.items() if not key.startswith("_")
            },
            "result": {key: value for key, value in result.items() if key not in ignored},
        }
    )
    seen = list(previous.get("seen", []))
    first_read = observation not in seen
    if first_read:
        seen.append(observation)
    has_new = previous.get("has_new", False) or (first_read and not already_shared)
    idle = previous.get("idle_rounds", 0)
    if last:
        idle = 0 if has_new else idle + 1
    return {**previous, "seen": seen, "has_new": False if last else has_new, "idle_rounds": idle}

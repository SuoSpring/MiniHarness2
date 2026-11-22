"""成功日志保留头尾；未解决错误保留正文，真实重试成功后附上解决依据。"""

import hashlib
import json
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict, messages_to_dict

_BODY_KEYS = {"stdout", "stderr", "content"}
PROCESS_TOOLS = {"run_python", "run_command", "install_dependencies"}
TRACE_CHARS = 20
TRACE_MARKER = "\n…（正文已收起，完整内容见日志）…\n"


def execution_failed(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("ok") is False
        or payload.get("timed_out")
        or payload.get("result_unknown")
        or payload.get("error")
        or payload.get("status") in {"failed", "error", "cancelled"}
        or ("exit_code" in payload and payload["exit_code"] != 0)
    )


def unresolved_log(payload: dict[str, Any]) -> bool:
    if payload.get("log_state") == "resolved" and payload.get("log_resolution"):
        return False
    return payload.get("log_state") == "unresolved" or execution_failed(payload)


def execution_metadata(
    name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    *,
    log_id: str,
    run_id: str,
    tool_call_id: str,
) -> dict[str, Any]:
    # 权限声明不改变执行本身；代码/工具实现修复后相同参数仍属于同一项重试。
    identity = (
        {
            "path": str(arguments.get("path", "")).replace("\\", "/"),
            "args": arguments.get("args", []),
        }
        if name == "run_python"
        else {
            "packages": arguments.get("packages", []),
            "requirements": arguments.get("requirements", ""),
            "upgrade": arguments.get("upgrade", False),
        }
        if name == "install_dependencies"
        else {"shell": arguments.get("shell"), "command": arguments.get("command")}
    )
    key = hashlib.sha256(
        json.dumps([name, identity], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {
        **payload,
        "log_id": log_id,
        "log_key": key,
        "log_tool": name,
        "log_arguments": identity,
        "run_id": run_id,
        "tool_call_id": tool_call_id,
        "log_state": "unresolved"
        if execution_failed(payload) or payload.get("exit_code") != 0
        else "success",
    }


def register_log_result(
    outcomes: dict[str, dict[str, Any]],
    name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    *,
    log_id: str,
    run_id: str,
    tool_call_id: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """按运行中的真实结果更新错误状态；不依赖模型宣称已修复。"""
    outcomes = {key: dict(value) for key, value in outcomes.items()}
    resolutions = []
    if name in PROCESS_TOOLS or name == "verification":
        payload = execution_metadata(
            name, arguments, payload, log_id=log_id, run_id=run_id, tool_call_id=tool_call_id
        )
        if payload["log_state"] == "success":
            for error_id, previous in outcomes.items():
                if previous.get("log_state") != "unresolved" or previous.get("log_tool") != name:
                    continue
                explicit = arguments.get("retry_of") == error_id
                same = previous.get("log_key") == payload["log_key"]
                if not (explicit or same):
                    continue
                resolution = {
                    "rule": "explicit_retry" if explicit else "same_tool_arguments",
                    "tool": name,
                    "tool_call_id": tool_call_id,
                    "message_id": log_id,
                    "run_id": run_id,
                    "exit_code": 0,
                    "timed_out": False,
                }
                previous.update(log_state="resolved", log_resolution=resolution)
                resolutions.append({"error_id": error_id, "resolution": resolution})
        outcomes[log_id] = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "log_id",
                "log_key",
                "log_tool",
                "log_arguments",
                "log_state",
                "run_id",
                "tool_call_id",
                "stdout_log",
                "stderr_log",
            }
        }
        payload = compact_log_result(payload)
    elif name == "read_log":
        source = payload.get("log_source")
        if not isinstance(source, dict):
            source = next(
                (
                    entry
                    for entry in outcomes.values()
                    if entry.get("run_id") == payload.get("run_id", run_id)
                    and arguments.get("path") in {entry.get("stdout_log"), entry.get("stderr_log")}
                ),
                None,
            )
        if source and source.get("log_id"):
            origin = source["log_id"]
            outcomes.setdefault(origin, source)
            payload = {
                **payload,
                "log_origin": origin,
                "log_state": outcomes[origin]["log_state"],
                "log_tool": source.get("log_tool"),
                "log_arguments": source.get("log_arguments", {}),
            }
            if outcomes[origin].get("log_resolution"):
                payload["log_resolution"] = outcomes[origin]["log_resolution"]
    return payload, outcomes, resolutions


def verification_metadata(
    gate: dict[str, Any], check: dict[str, Any], run_id: str
) -> dict[str, Any]:
    arguments = {
        "command": json.dumps([gate.get("policy_hash"), gate.get("task_revision"), check.get("id")])
    }
    return execution_metadata(
        "verification",
        arguments,
        {
            **check,
            "exit_code": check.get("exit_code", 0 if check.get("status") == "passed" else None),
        },
        log_id=f"{run_id}:verification:{gate['id']}:{check['id']}",
        run_id=run_id,
        tool_call_id=f"{gate['id']}:{check['id']}",
    )


def register_verification_logs(
    outcomes: dict[str, dict[str, Any]], gate: dict[str, Any], run_id: str
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    resolutions = []
    for check in gate.get("checks", []):
        if check.get("kind") == "independent" or check.get("origin") == "独立生成的验收测试":
            continue
        if not any(check.get(key) for key in ("stdout_log", "stderr_log")):
            continue
        metadata = verification_metadata(gate, check, run_id)
        arguments = {
            "command": json.dumps(
                [gate.get("policy_hash"), gate.get("task_revision"), check.get("id")]
            )
        }
        _, outcomes, resolved = register_log_result(
            outcomes,
            "verification",
            arguments,
            metadata,
            log_id=metadata["log_id"],
            run_id=run_id,
            tool_call_id=metadata["tool_call_id"],
        )
        for item in resolved:
            item["resolution"].update(
                rule="same_verification_check", verification_id=gate["id"], check_id=check["id"]
            )
        resolutions.extend(resolved)
    return outcomes, resolutions


def compact_log_result(payload: dict[str, Any], *, content_log: bool = False) -> dict[str, Any]:
    """每路保留头尾各 20 个 Unicode 字符；失败正文不能因模型读过就清掉。"""
    has_reference = any(payload.get(key) for key in ("stdout_log", "stderr_log")) or (
        content_log and bool(payload.get("path"))
    )
    if (
        unresolved_log(payload)
        or payload.get("log_body_omitted")
        or not has_reference
        or not any(isinstance(payload.get(key), str) and payload[key] for key in _BODY_KEYS)
    ):
        return payload
    retained = {key: value for key, value in payload.items() if key != "log_summary"}
    for key in (*_BODY_KEYS, "error"):
        value = retained.get(key)
        if isinstance(value, str) and len(value) > TRACE_CHARS * 2:
            retained[key] = value[:TRACE_CHARS] + TRACE_MARKER + value[-TRACE_CHARS:]
            retained["log_body_omitted"] = True
    return retained


def apply_log_outcomes(
    history: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    messages = messages_from_dict(history)
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        outcome = outcomes.get(payload.get("log_origin") or payload.get("log_id") or message.id)
        if not outcome:
            continue
        payload["log_state"] = outcome["log_state"]
        if outcome.get("log_resolution"):
            payload["log_resolution"] = outcome["log_resolution"]
            # 显式 read_log 新读取的页面先完整展示一次；之后才收起。
            if message.name != "read_log" or payload.get("log_observed"):
                payload = compact_log_result(payload, content_log=message.name == "read_log")
        payload.pop("log_source", None)
        messages[index] = message.model_copy(
            update={"content": json.dumps(payload, ensure_ascii=False)}
        )
    return messages_to_dict(messages)


def compact_observed_logs(
    history: list[dict[str, Any]], observed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """仅处理本次真正发送过的日志消息；调用方必须在模型成功返回后使用。"""
    seen = {
        (message.id, message.tool_call_id, message.name): message.content
        for message in messages_from_dict(observed)
        if isinstance(message, ToolMessage) and message.id and isinstance(message.content, str)
    }
    requests: dict[str, dict[str, Any]] = {}
    result = []
    for message in messages_from_dict(history):
        if isinstance(message, AIMessage):
            requests.update({call["id"]: call["args"] for call in message.tool_calls})
        if (
            isinstance(message, ToolMessage)
            and message.name in {*PROCESS_TOOLS, "read_log"}
            and (message.id, message.tool_call_id, message.name) in seen
            and isinstance(message.content, str)
        ):
            try:
                payload = json.loads(message.content)
                sent_payload = json.loads(seen[(message.id, message.tool_call_id, message.name)])
            except json.JSONDecodeError:
                payload = None
                sent_payload = None
            if (
                isinstance(payload, dict)
                and isinstance(sent_payload, dict)
                # 超窗预览可能先省略正文；模型尚未真正看到时不能标记为已消费。
                and all(payload.get(key) == sent_payload.get(key) for key in _BODY_KEYS)
            ):
                arguments = requests.get(message.tool_call_id, {})
                if message.name == "read_log":
                    payload.setdefault("path", arguments.get("path"))
                    payload.setdefault("offset", arguments.get("offset", 0))
                # 兼容已有检查点：工具消息 ID 中包含原始 run_id。
                origin, separator, _ = (message.id or "").rpartition(":tool:")
                if separator:
                    payload.setdefault("run_id", arguments.get("run_id") or origin)
                has_reference = any(payload.get(key) for key in ("stdout_log", "stderr_log")) or (
                    message.name == "read_log" and payload.get("path")
                )
                if has_reference:
                    payload["log_observed"] = True
                    compacted = compact_log_result(payload, content_log=message.name == "read_log")
                    message = message.model_copy(
                        update={"content": json.dumps(compacted, ensure_ascii=False)}
                    )
        result.append(message)
    return messages_to_dict(result)

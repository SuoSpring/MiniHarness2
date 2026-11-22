"""把工具实际观察到的事实交给审查者；完整回执和日志由运行轨迹保留。"""

import json
from collections import Counter
from shlex import join

from miniharness.context.logs import compact_log_result
from miniharness.context.manager import token_count
from miniharness.milestones import current_snapshot, evidence_current, may_change_files
from miniharness.tools.files import DEFAULT_DIRECTORY_LIMIT, MAX_DIRECTORY_LIMIT, READ_PAGE_LINES

from .contracts import GraphServices
from .progress import fingerprint

PROCESS = {"run_python", "run_command"}
EDITS = {"write_file", "replace_file", "delete_file"}
READS = {"read_file", "search_file", "list_files", "read_log", "list_logs", "recall_memory"}
FILE_READS = {"read_file", "list_files", "search_file"}
EXECUTION_SOURCES = PROCESS | FILE_READS | {"read_log", "get_python_environment"}


def file_changed(name: str, result: dict) -> bool:
    if not may_change_files(name):
        return False
    if not result.get("result_unknown"):
        if (name in PROCESS or name.startswith("mcp_")) and result.get(
            "workspace_changed"
        ) is False:
            return False
        if name in {"write_file", "replace_file"} and result.get("changed") is False:
            return False
    return True


def tool_event_payload(
    state: dict,
    name: str,
    arguments: dict,
    result: dict,
    *,
    allowed: bool = True,
    source: dict | None = None,
) -> dict:
    call_id = state["pending_tools"][0]["id"]
    changed = allowed and file_changed(name, result)
    return {
        "name": name,
        "arguments": arguments,
        "result": result,
        "role": state["tool_role"],
        "operation_id": f"{state['execution_epoch']}:{state['sequence']}:{call_id}",
        "tool_call_id": call_id,
        "task_revision": state["task_revision"],
        "file_revision": state.get("file_revision", 0) + int(changed),
        "snapshot": "" if changed else current_snapshot(state),
        **({"source": source} if source else {}),
        **({} if allowed else {"denied": True}),
    }


def current_evidence_ids(state: dict, observations: list[dict]) -> set[str]:
    return {
        item["id"]
        for item in observations
        if item["ok"]
        and (
            state.get("task_kind") != "coding"
            or item["tool"] not in {"recall_memory", "list_logs", "read_execution_result"}
        )
        and (item["tool"] != "read_execution_result" or "event_id" in item["result"])
        and evidence_current(state, item)
    }


def excerpt(value: object, limit: int) -> str:
    if not isinstance(value, str):
        value = str(value)
    if len(value) <= limit:
        return value
    head = limit // 2
    return value[:head] + " … " + value[-head:]


def tool_observation(
    name: str, args: dict, result: dict, *, identifier: str, revision: int, snapshot: str, role: str
) -> dict | None:
    external = name.startswith("mcp_")
    if not external and name not in EXECUTION_SOURCES | READS | EDITS | {"read_execution_result"}:
        return None
    ok = result.get("ok", True) and not result.get("result_unknown")
    details = {}
    path = args.get("path")
    if name in PROCESS:
        ok = ok and result.get("exit_code") == 0 and not result.get("timed_out")
        invocation = result.get("log_arguments") or args
        command = invocation.get("command")
        if name == "run_python":
            arguments = invocation.get("args")
            argv = [str(arg) for arg in arguments] if isinstance(arguments, list) else []
            command = join(["python", str(invocation.get("path") or ""), *argv])
        details["invocation"] = excerpt(command or name, 300)
        details["exit_code"] = result.get("exit_code")
        if ok:
            preview = compact_log_result(result)
            details.update(
                {k: preview[k] for k in ("stdout_log", "stderr_log", "run_id") if preview.get(k)}
            )
            for stream in ("stdout", "stderr"):
                if preview.get(stream):
                    details[stream] = excerpt(preview[stream], 360)
            if preview.get("log_body_omitted") or any(
                len(preview.get(k) or "") > 360 for k in ("stdout", "stderr")
            ):
                details["truncated"] = True
    elif external or name == "get_python_environment":
        if ok:
            value = result.get("result", result)
            body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            details.update(excerpt=excerpt(body, 360), truncated=len(body) > 360)
    elif name == "read_execution_result":
        details.update(
            {
                k: result[k]
                for k in (
                    "run_id",
                    "event_id",
                    "tool",
                    "offset",
                    "next_offset",
                    "source_ok",
                    "file_revision",
                    "task_revision",
                    "next_before",
                )
                if k in result
            }
        )
        if "content" in result:
            details["excerpt"] = excerpt(result["content"], 360)
            details["truncated"] = (
                result.get("next_offset") is not None or len(result["content"]) > 360
            )
    elif name == "read_file":
        details["start_line"] = result.get("start_line", args.get("start_line", 1))
        details["end_line"] = args.get("end_line")
        if "cursor" in args:
            details["cursor"] = args["cursor"]
        if ok:
            if result.get("start_column"):
                details["start_column"] = result["start_column"]
            if result.get("next_cursor"):
                details["next_cursor"] = result["next_cursor"]
            details["excerpt"] = excerpt(result.get("content", ""), 480)
            details["total_lines"] = result.get("total_lines")
            details["truncated"] = bool(
                result.get("truncated") or len(result.get("content", "")) > 480
            )
            if (
                result.get("truncated")
                and args.get("end_line") is not None
                and not result.get("next_cursor")
                and len(result.get("content", "")) <= 480
            ):
                details["range_complete"] = True
    elif name == "search_file":
        query = args.get("query", "")
        details["query"] = excerpt(query, 160)
        if query != details["query"]:
            details["query_id"] = fingerprint(query)
        if args.get("cursor"):
            details["cursor"] = excerpt(args["cursor"], 160)
        if ok and "match" in result:
            hit = result["match"]
            details.update(
                found=hit is not None,
                next_cursor=result["next_cursor"],
                truncated="query_id" in details,
            )
            if hit is not None:
                details.update(
                    {
                        k: hit[k]
                        for k in (
                            "term_index",
                            "line",
                            "column",
                            "byte_offset",
                            "length",
                            "read_cursor",
                        )
                    }
                )
                details["excerpt"] = hit["preview"]
    elif name in {"list_files", "list_logs"} and ok:
        entries = result.get("result", []) if name == "list_files" else result.get("logs", [])
        details["count"] = result.get("total", len(entries))
        if name == "list_logs":
            details["run_id"] = result.get("run_id") or args.get("run_id")
        paths = [
            str(entry.get("path", "")) if isinstance(entry, dict) else str(entry)
            for entry in entries[:8]
        ]
        paged = "next_cursor" in result
        if paged:
            details["cursor"] = args.get("cursor", "")
            # 审查者只看到前八项，后续应从实际可见的位置接着翻。
            details["next_cursor"] = (
                paths[-1] if len(entries) > len(paths) else result["next_cursor"]
            )
        details["entries"] = [excerpt(path, 100) for path in paths]
        details["truncated"] = (
            bool(result.get("truncated"))
            or (paged and bool(details["cursor"] or details["next_cursor"]))
            or len(entries) > 8
            or any(len(str(entry)) > 100 for entry in entries[:8])
        )
        if (
            name == "list_files"
            and paged
            and details["truncated"]
            and len(entries) <= 8
            and details["entries"] == entries
        ):
            details["page_complete"] = True
    elif name == "read_log":
        details["offset"] = args.get("offset", 0)
        details["run_id"] = result.get("run_id") or args.get("run_id")
        details.update({key: result[key] for key in ("log_state", "log_tool") if result.get(key)})
        if ok:
            details["excerpt"] = excerpt(result.get("content", ""), 360)
    elif name == "recall_memory":
        details["query"] = excerpt(args.get("query", ""), 160)
        details["matched"] = result.get("matched", len(result.get("memories", [])))
        # 正文由共享记忆区按实际预算提供，不再复制一份。
    elif name in EDITS:
        details.update({k: result[k] for k in ("changed", "deleted", "bytes") if k in result})
    if not ok:
        for key in (
            "error_type",
            "error",
            "stderr",
            "stdout",
            "timed_out",
            "stdout_log",
            "stderr_log",
        ):
            value = result.get(key)
            if value:
                details[key] = excerpt(value, 600) if isinstance(value, str) else value
    return {
        "id": identifier,
        "tool": name,
        "role": role,
        "path": path,
        "file_revision": revision,
        "snapshot": snapshot,
        "ok": bool(ok),
        "result": details,
        **{key: result[key] for key in ("log_id", "log_key") if name in PROCESS and key in result},
    }


def check_summary(check: dict) -> dict:
    return {
        key: check[key]
        for key in ("id", "kind", "path", "args", "status", "tests_passed", "case_names")
        if key in check
    }


def visible_read(item: dict) -> bool:
    """审查输入确实展示了整个请求的范围或整页，才可用于执行前复用。"""
    data = item.get("result", {})
    if not item.get("ok") or data.get("details_omitted"):
        return False
    if item["tool"] == "read_file":
        return "excerpt" in data and (
            not data.get("truncated", True) or data.get("range_complete") is True
        )
    if item["tool"] == "list_files":
        return "entries" in data and (
            not data.get("truncated", True) or data.get("page_complete") is True
        )
    return item["tool"] == "search_file" and "found" in data and data.get("truncated") is False


def check_receipt(check: dict) -> dict:
    receipt = check_summary(check)
    if check.get("status") != "passed":
        failures = check.get("case_failures") or []
        reason = failures[0].get("message") if failures else None
        reason = reason or check.get("error") or check.get("stderr")
        if reason:
            receipt["error"] = excerpt(reason, 400)
        receipt.update({k: check[k] for k in ("stderr_log", "stdout_log") if check.get(k)})
    return receipt


async def event_observations(services: GraphServices, state: dict) -> list[dict]:
    """按活动分支读取真实事件；投影只在准备输入或校验引用时存在。"""
    observations = list(state.get("evidence", []))  # 升级前的运行和边界
    identifiers = state.get("tool_event_ids", [])
    if not identifiers:
        return observations
    sources = {}
    for event in await services.tool_events(identifiers):
        if event.get("denied") or event["task_revision"] != state["task_revision"]:
            continue
        name, args, result = event["name"], event["arguments"], event["result"]
        if not evidence_current(state, event) and name not in PROCESS and name != "verify_project":
            continue
        ref = f"event:{event['id']}"
        version = {"file_revision": event["file_revision"], "snapshot": event["snapshot"]}
        if name == "verify_project":
            for check in result["checks"]:
                observations.append(
                    {
                        "id": f"{ref}:{check['id']}",
                        "tool": "verification",
                        "role": event["role"],
                        "verification_id": result["id"],
                        "suite_ids": result.get("suite_ids", []),
                        "scope_key": args["scope_key"],
                        **version,
                        "path": check.get("path"),
                        "ok": check["status"] == "passed",
                        "result": check_receipt(check),
                    }
                )
            continue
        source = event.get("source", {})
        if name in FILE_READS and source and evidence_current(state, event):
            key = (name, args.get("path", "."))
            if key not in sources:
                sources[key] = services.read_source(name, args)
            if not source.get("stamp") or sources[key].get("stamp") != source["stamp"]:
                continue
        if name == "read_log" and isinstance(result.get("log_source"), dict):
            origin = result["log_source"]
            outcome = state.get("log_outcomes", {}).get(origin.get("log_id"), origin)
            result = {
                **result,
                **{k: outcome[k] for k in ("log_state", "log_tool") if k in outcome},
            }
        item = tool_observation(
            name,
            {**args, "path": source.get("path", args.get("path"))},
            result,
            identifier=ref,
            revision=event["file_revision"],
            snapshot=event["snapshot"],
            role=event["role"],
        )
        if item is not None:
            observations.append(item)
    return observations


def covers_read(event: dict, name: str, args: dict) -> bool:
    """调用方已确认来源、版本及可见正文。此处只比较请求的范围与分页语义。"""
    previous, result = event["arguments"], event["result"]
    if name == "search_file":
        return (
            args.keys() <= {"path", "query", "cursor"}
            and args.get("query") == previous.get("query")
            and args.get("cursor") == previous.get("cursor")
        )
    if name == "list_files":
        limit = args.get("limit", DEFAULT_DIRECTORY_LIMIT)
        return (
            args.keys() <= {"path", "limit", "cursor"}
            and type(limit) is int
            and 1 <= limit <= MAX_DIRECTORY_LIMIT
            and args.get("cursor", "") == previous.get("cursor", "")
            and (
                limit == previous.get("limit", DEFAULT_DIRECTORY_LIMIT)
                or (not result.get("next_cursor") and limit >= len(result.get("result", [])))
            )
        )
    if name != "read_file" or not args.keys() <= {"path", "cursor", "start_line", "end_line"}:
        return False
    if args.get("cursor") is not None or previous.get("cursor") is not None:
        return (
            args.get("cursor") == previous.get("cursor")
            and args.get("start_line") is None
            and args.get("end_line") is None
        )
    first = 1 if args.get("start_line") is None else args["start_line"]
    last = args.get("end_line")
    if (
        type(first) is not int
        or first < 1
        or (
            last is not None
            and (type(last) is not int or not first <= last < first + READ_PAGE_LINES)
        )
    ):
        return False
    start = result.get("start_line") or previous.get("start_line") or 1
    if first == start and last == previous.get("end_line"):
        return True
    end = start + len(result.get("content", "").splitlines()) - 1
    if not start <= first <= end:
        return False
    # 到达真实文件末尾时，越过末尾的行仍由此回执覆盖。
    if not result.get("truncated") and result.get("total_lines") == end:
        return True
    return last is not None and last <= end


def _legacy_tools(state: dict) -> dict:
    """旧检查点的读取正文和命令仍可能在已配对的工具消息中。"""
    receipts = {}
    for channel, default_role in (("messages", "executor"), ("review_messages", "reflection")):
        calls = {}
        role = default_role
        for message in state.get(channel, []):
            data = message.get("data", {})
            if message.get("type") == "ai":
                role = (
                    "planner"
                    if str(data.get("id", "")).startswith(f"{state['run_id']}:planner:")
                    else default_role
                )
                calls = {call["id"]: call.get("args", {}) for call in data.get("tool_calls", [])}
            if message.get("type") == "tool" and data.get("id"):
                receipts[data["id"]] = (role, calls.get(data.get("tool_call_id"), {}), data)
    return receipts


def _legacy_observation(state: dict, item: dict, receipts: dict) -> dict:
    number = item["id"].removeprefix("e")
    if not number.isdecimal():
        return item
    message_id = f"{state['run_id']}:tool:{number}"
    archived = receipts.get(message_id)
    result = {**item.get("result", {}), **state.get("log_outcomes", {}).get(message_id, {})}
    role, args = "", {}
    if archived:
        role, args, message = archived
        try:
            body = json.loads(message.get("content"))
        except (TypeError, ValueError):
            body = None
        if isinstance(body, dict):
            result.update(body)
    if not archived and not result.get("log_arguments"):
        return item
    entry = tool_observation(
        item["tool"],
        {"path": item.get("path"), **args},
        {**result, "ok": item["ok"]},
        identifier=item["id"],
        revision=item.get("file_revision", 0),
        snapshot=item.get("snapshot", ""),
        role=role,
    )
    if entry:
        return {
            **item,
            **{k: entry[k] for k in ("result", "role", "log_key", "log_id") if k in entry},
        }
    return item


def review_evidence(
    state: dict, observations: list[dict], max_tokens: int = 2400
) -> tuple[list[dict], list[dict]]:
    """为成功检查、失败诊断、读取回执和旧版本历史分别预留上下文。"""
    current, history = [], []
    seen = set()
    counts = Counter()
    limits = {"checks": 16, "failures": 6, "observations": 12, "history": 8}
    remaining = {"checks": max_tokens // 4, "failures": max_tokens // 3, "history": max_tokens // 8}
    remaining["observations"] = max_tokens - sum(remaining.values())
    legacy = None
    for item in reversed(observations):
        valid_version = evidence_current(state, item)
        tool, data = item["tool"], item.get("result", {})
        is_check = tool in PROCESS or tool == "verification"
        if not valid_version and not is_check:
            continue
        if "role" not in item and tool != "verification":
            if legacy is None:
                legacy = _legacy_tools(state)
            item = _legacy_observation(state, item, legacy)
            data = item.get("result", {})
        if state.get("log_outcomes", {}).get(item.get("log_id"), {}).get("log_state") == "resolved":
            continue
        group = (
            "history"
            if not valid_version
            else "failures"
            if not item["ok"]
            else "checks"
            if is_check
            else "observations"
        )
        if counts[group] >= limits[group]:
            continue
        identity = item.get("log_key") or (
            item.get("path"),
            data.get("id"),
            data.get("start_line"),
            data.get("end_line"),
            data.get("run_id"),
            data.get("offset"),
            data.get("cursor"),
            data.get("next_cursor"),
            data.get("query_id", data.get("query")),
            item.get("scope_key"),
        )
        # 旧命令证据可能只有日志路径，不能把不同命令合并为同一项。
        if tool.startswith("mcp_") or tool == "read_execution_result":
            identity = item["id"]
        elif tool in PROCESS and "log_key" not in item:
            identity = data.get("stdout_log") or item["id"]
        # 同一版本上同一检查先通过又失败时，两种结果都需要交给审查者。
        key = (tool, item["ok"], json.dumps(identity, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        if valid_version:
            view = {
                k: item[k] for k in ("id", "tool", "role", "path", "ok") if item.get(k) is not None
            }
            view["result"] = check_receipt(data) if tool == "verification" else data
        else:
            test = check_summary(data) if tool == "verification" else data.get("invocation")
            view = {
                "file_revision": item.get("file_revision"),
                "tool": tool,
                "test": test or item.get("path") or item["id"],
                "ok": item["ok"],
            }
            if not item["ok"]:
                view["error"] = excerpt(
                    data.get("error") or data.get("stderr") or data.get("stdout") or "", 280
                )
                view["log"] = data.get("stderr_log") or data.get("stdout_log")
        size = token_count(view)
        if size > remaining[group] and valid_version:
            brief = {
                k: v
                for k, v in view["result"].items()
                if k not in {"stdout", "stderr", "excerpt", "entries"}
            }
            if tool in {"list_files", "list_logs", "read_file", "search_file"}:
                brief.pop("next_cursor", None)  # 本页正文或名称已省略，保留起始位置以补读。
            view = {**view, "result": {**brief, "details_omitted": True}}
            size = token_count(view)
        if size <= remaining[group]:
            remaining[group] -= size
            counts[group] += 1
            (current if valid_version else history).append(view)
    return current[::-1], history[::-1]

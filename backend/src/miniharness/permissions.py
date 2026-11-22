"""执行前的权限与人工审批策略；不冒充操作系统沙箱。"""

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from .workspace.paths import resolve_path


class PermissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    write_scope: Literal["none", "project", "all"] = "project"
    approval_mode: Literal["request", "full_access"] = "request"

    @model_validator(mode="after")
    def normalize(self) -> Self:
        if self.approval_mode == "full_access":
            self.write_scope = "all"
        return self


def default_permissions() -> dict[str, str]:
    return PermissionPolicy().model_dump()


def _major_invocation(words: list[str], shell: str) -> bool:
    program = Path(words[0]).name.removesuffix(".exe")
    args = words[1:]
    options = args[: args.index("--")] if "--" in args else args
    if program in {
        "rm",
        "del",
        "erase",
        "rd",
        "rmdir",
        "remove-item",
        "clear-disk",
        "format-volume",
    }:
        return shell != "powershell" or "-whatif" not in options
    if program == "git" and options:
        action, *flags = options
        if action == "reset":
            return "--hard" in flags
        if action == "push":
            return not {"--dry-run", "-n"}.intersection(flags)
        if action == "clean":
            short = "".join(
                flag[1:] for flag in flags if flag.startswith("-") and not flag.startswith("--")
            )
            return (
                "--dry-run" not in flags
                and "n" not in short
                and ("f" in short or "--force" in flags)
            )
    if program.startswith("python") and options[:2] == ["-m", "pip"]:
        program, options = "pip", options[2:]
    return (
        program in {"pip", "pip3", "npm", "npm.cmd", "conda"}
        and bool(options)
        and options[0] in {"install", "uninstall", "remove", "update"}
        and "--dry-run" not in options
    )


def major_command(command: str, shell: str) -> bool:
    """识别常见的删除、依赖变更与破坏性 Git 命令；任意脚本仍依赖副作用声明。"""
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    words: list[str] = []
    try:
        tokens = list(lexer)
    except ValueError:
        return False
    for token in [*tokens, "\n"]:
        if token.strip(";&|\n"):
            words.append(token.strip("\"'").casefold())
            continue
        if words and _major_invocation(words, shell):
            return True
        words = []
    return False


def similar_approval(request: dict[str, Any]) -> dict[str, str] | None:
    args = request["arguments"]
    reads, writes = request["read_paths"], request["write_paths"]
    if (
        args.get("_permission", {}).get("high_risk")
        or args.get("_environment_hash") == "unavailable"
    ):
        return None
    name = request["tool"]
    if name in {"write_file", "replace_file"}:
        directory = str(Path(writes[0]).parent)
        operation = directory
        name, writes = "write_text", []
        label = f"新建或修改 {directory} 的直属文本文件"
    elif name == "run_python":
        operation = [request["script"]["path"], request["script"]["sha256"], args["args"]]
        label = "以相同参数和读写路径运行同一版本的脚本"
    elif name == "run_command" and not major_command(args["command"], args["shell"]):
        operation = [args["shell"], args["command"]]
        label = f"在 {args['shell']} 中以相同读写路径执行上面这条完整命令"
    else:
        return None
    scope = [
        name,
        operation,
        sorted(set(reads)),
        sorted(set(writes)),
        request["permission_policy"],
        request["workdir"],
        args.get("_environment"),
        args.get("_environment_hash"),
    ]
    key = hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"key": key, "description": label}


def permission_request(
    policy: PermissionPolicy,
    workdir: Path,
    name: str,
    arguments: dict[str, Any],
    operation_id: str,
    *,
    snapshot: str = "",
    workspace_fingerprint: str = "",
    external: bool = False,
    external_readonly: bool = False,
    legacy: bool = False,
) -> dict[str, Any] | None:
    """解析实际目标，再决定是否中断；缺少声明不能被当作只读。"""
    metadata = arguments.get("_permission", {})
    if not isinstance(metadata, dict) or set(metadata) - {"high_risk", "reason"}:
        raise ValueError("_permission 只能包含 high_risk 和 reason")
    if not isinstance(metadata.get("high_risk", False), bool) or not isinstance(
        metadata.get("reason", ""), str
    ):
        raise ValueError("high_risk 必须为布尔值，reason 必须为字符串")
    writes: list[str] = []
    reads: list[str] = []
    reasons: list[str] = []
    if name in {"write_file", "replace_file", "delete_file"}:
        if not isinstance(arguments.get("path"), str):
            raise ValueError(f"{name} 必须提供显式 path 字符串")
        writes = [arguments["path"]]
    elif name == "load_skill":
        # 加载技能会复制整个包，不能成为禁止写入时的旁路。
        if not isinstance(arguments.get("name"), str):
            raise ValueError("load_skill 必须提供 name 字符串")
        writes = [f".harness-skills/{arguments['name']}"]
    elif name in {"run_python", "run_command"}:
        for key in ("read_paths", "write_paths"):
            paths = arguments.get(key)
            if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
                raise ValueError(f"{name} 必须显式提供 {key} 路径列表；无路径时传 []")
        reads, writes = list(arguments["read_paths"]), list(arguments["write_paths"])
        if name == "run_python":
            if not isinstance(arguments.get("path"), str):
                raise ValueError("run_python 必须提供显式 path 字符串")
            if not isinstance(arguments.get("args"), list) or any(
                not isinstance(arg, str) for arg in arguments["args"]
            ):
                raise ValueError("run_python 必须显式提供 args 字符串列表，路径参数在此传入")
            reads.insert(0, arguments["path"])
        elif arguments.get("shell") not in {"bash", "cmd", "powershell"} or not isinstance(
            arguments.get("command"), str
        ):
            raise ValueError("run_command 必须提供有效的 shell 和 command 字符串")
        if legacy:
            reasons.append("尚无系统沙箱，无法仅凭路径声明确认脚本或命令的全部副作用。")
        elif name == "run_command" and major_command(arguments["command"], arguments["shell"]):
            reasons.append("该命令会执行删除、依赖变更或版本库发布等重大操作。")
    elif name == "verify_project" and legacy:
        reasons.append("自动验收会执行项目入口与测试代码，需要批准本次文件版本的程序执行。")
    elif name == "configure_python_environment":
        reasons.append("修改项目的 Python 环境配置；创建环境会写入依赖目录，配置不随文件撤回。")
        if arguments.get("mode") == "managed":
            writes = [arguments["_environment_target"]]
    elif name == "install_dependencies":
        reasons.append(
            "安装依赖会改变当前项目选定的 Python 环境，可能执行包构建代码；共享该环境的项目也受影响。"
        )
        writes = [arguments["_environment"]["prefix"]]
        if arguments.get("requirements"):
            reads = [arguments["requirements"]]
    elif external and (legacy or not external_readonly):
        reasons.append("外部工具的实际写入范围无法由本地权限检查确认。")

    resolved_reads = [str(resolve_path(workdir, path)) for path in reads]
    resolved_writes = [str(resolve_path(workdir, path)) for path in writes]
    outside = [path for path in resolved_writes if not Path(path).is_relative_to(workdir.resolve())]
    if writes and policy.write_scope == "none":
        reasons.append("当前设置禁止自动写入；本次操作需要写入文件。")
    elif outside and policy.write_scope == "project":
        reasons.append("写入目标超出当前项目工作副本。")
    if name == "delete_file":
        reasons.append("删除文件属于高风险操作。")
    if metadata.get("high_risk") and (
        legacy
        or name
        not in {
            "read_file",
            "search_file",
            "list_files",
            "read_log",
            "list_logs",
            "read_execution_result",
        }
    ):
        reasons.append(metadata.get("reason") or "模型将此操作标记为高风险。")
    if not reasons or policy.approval_mode == "full_access":
        return None
    payload = {
        "status": "awaiting_tool_approval",
        "kind": "tool_permission",
        "tool": name,
        "arguments": arguments,
        "workdir": str(workdir.resolve()),
        "read_paths": resolved_reads,
        "write_paths": resolved_writes,
        "outside_paths": outside,
        "reasons": reasons,
        "permission_policy": policy.model_dump(),
        "snapshot": snapshot,
        "workspace_fingerprint": workspace_fingerprint,
        "operation_id": operation_id,
        "notice": "批准仅适用于本次操作。项目外修改及外部副作用不随项目撤回。路径声明不是系统沙箱。",
    }
    if name == "run_python":
        script = resolve_path(workdir, arguments["path"])
        source = script.read_bytes()
        payload["script"] = {
            "path": str(script),
            "sha256": hashlib.sha256(source).hexdigest(),
            "content": source[:32000].decode("utf-8", errors="replace"),
            "truncated": len(source) > 32000,
        }
    payload["request_id"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if not legacy:
        payload["approval_version"] = 2
    if not legacy and not external and (rule := similar_approval(payload)):
        payload["similar_approval"] = rule
        payload["notice"] = "路径声明不是系统沙箱。项目外修改及外部副作用不随项目撤回。"
    return payload

import asyncio
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, message_to_dict
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import interrupt
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select

from miniharness.agent.contracts import GraphControl, GraphState, UserQuestions
from miniharness.agent.evidence import tool_event_payload
from miniharness.agent.specification import RequirementResolution
from miniharness.context.logs import (
    PROCESS_TOOLS,
    execution_failed,
    execution_metadata,
    verification_metadata,
)
from miniharness.integrations.mcp import MCPToolError, discover_server, invoke_tool
from miniharness.integrations.skills import Skills
from miniharness.memory import MemoryService
from miniharness.milestones import boundary_map, current_snapshot
from miniharness.permissions import PermissionPolicy, permission_request
from miniharness.runtime.environments import DependencyInstall, EnvironmentSelection
from miniharness.runtime.errors import BudgetExceeded
from miniharness.runtime.limits import RunLimits
from miniharness.runtime.timeline import execution_result_page
from miniharness.storage.models import (
    AcceptanceSuite,
    Boundary,
    Event,
    MCPServer,
    MemoryCandidate,
    MemoryDraft,
    Message,
    Project,
    Run,
    Session,
    Verification,
    new_id,
)
from miniharness.tools import (
    delete_file,
    list_files,
    read_file,
    replace_file,
    run_command,
    run_python,
    search_file,
    write_file,
)
from miniharness.tools.files import (
    DEFAULT_DIRECTORY_LIMIT,
    MAX_DIRECTORY_LIMIT,
    EditRejected,
    read_source,
)
from miniharness.verification.acceptance import digest
from miniharness.verification.corrections import (
    IndependentTestFix,
    ProjectTestFix,
    corrected_test_text,
    failed_targets,
)
from miniharness.workspace.paths import safe_path, workspace_fingerprint

if TYPE_CHECKING:
    from miniharness.runtime.manager import Runtime


def _fingerprint_if_readable(directory: Path) -> str | None:
    try:
        return workspace_fingerprint(directory) if directory.is_dir() else None
    except OSError:
        return None


def tool_schema(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]],
    required: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


STRING = {"type": "string"}
PATHS = {"type": "array", "items": STRING, "description": "显式声明全部路径；无路径时传 []"}
RUN_PURPOSE = {
    "type": "string",
    "enum": ["task_result", "project_test"],
    "description": "task_result 表示取得用户要的数据或运行结果。project_test 表示验证项目，交给反思节点。",
}
PERMISSION = {
    "type": "object",
    "properties": {
        "high_risk": {"type": "boolean"},
        "reason": {"type": "string", "description": "高风险操作的目的、风险与影响范围"},
    },
    "additionalProperties": False,
    "description": "高风险操作必须主动标记，由框架请求人工审批；不是授权凭据。",
}
LOCAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "缺少关键信息时向用户提问并等待回答。自由澄清用 options=[]，选择题提供 2–4 个选项。必须单独调用，不得与其他工具放在同一批次。",
            "parameters": UserQuestions.model_json_schema(),
        },
    },
    tool_schema(
        "get_python_environment",
        "查看当前项目 Python 环境、后端默认环境以及已发现的环境名称和解释器路径。只读。",
        {},
    ),
    tool_schema(
        "configure_python_environment",
        "修改并持久保存当前项目的 Python 环境，立即用于 run_python、run_command 和自动验收。mode=default 使用当前后端默认环境；existing 选择已有环境；managed 为项目创建或复用独立 venv。python 接受已发现的环境名称、解释器或环境目录绝对路径；managed 时表示基础 Python，省略则使用后端默认环境。独立环境不继承后端依赖，随后用 install_dependencies 安装 pytest 和项目依赖。环境配置不随文件撤回。",
        {
            "mode": {"type": "string", "enum": ["default", "existing", "managed"]},
            "python": STRING,
            "name": STRING,
        },
        ["mode"],
    ),
    tool_schema(
        "install_dependencies",
        "在当前项目选定的 Python 环境中用 python -m pip 安装依赖。packages 为包说明列表，例如 ['pytest','numpy>=2']；requirements 可指定当前项目的依赖清单路径；upgrade 可升级。不要把 pip 选项放在 packages。环境可能由多个项目共享，安装影响使用它的项目；不随文件撤回。返回安装日志。",
        {
            "packages": {"type": "array", "items": STRING},
            "requirements": STRING,
            "upgrade": {"type": "boolean"},
            "retry_of": STRING,
        },
    ),
    tool_schema(
        "list_files",
        "仅列出 path 的直接子项，默认当前工作副本，也可传绝对路径。result 里是名字，目录以 / 结尾。"
        "进入子目录或读取文件时把名字接在 path 后。翻页时保持 path 相同，将 next_cursor 原样传给 cursor，null 表示末页。",
        {
            "path": STRING,
            "cursor": {"type": "string", "description": "第一页省略，后续使用上一页的 next_cursor"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DIRECTORY_LIMIT,
                "default": DEFAULT_DIRECTORY_LIMIT,
            },
        },
    ),
    tool_schema(
        "read_file",
        "按行流式读取 UTF-8 文件，接受项目相对路径或绝对路径。每页最多 400 行、16000 字符。"
        "续读只传原 path 和 next_cursor 作为 cursor，可从行内继续。显式 end_line 限定整个读取范围。"
        "next_cursor 为 null 表示范围结束，total_lines 在读到文件末尾时给出。",
        {
            "path": STRING,
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "cursor": {"type": "string", "description": "上页的 next_cursor，与行号参数择一使用"},
        },
        ["path"],
    ),
    tool_schema(
        "search_file",
        r"在单个 UTF-8 文件中按大小写搜索字面文本。query 用 | 表示任一词，如 错误|失败；\| 搜竖线，\\ 搜反斜杠。"
        "按文件顺序返回一处 match，含行号（从 1）、字符列（从 0）和预览。"
        "用 match.read_cursor 调 read_file 可从命中处读正文。需要下一处时沿用 path、query，将 next_cursor 传给 cursor；null 表示没有下一处。",
        {"path": STRING, "query": STRING, "cursor": STRING},
        ["path", "query"],
    ),
    tool_schema(
        "write_file",
        "创建或覆盖文本文件。长文件分段写，后续段明确指定 append=true。",
        {
            "path": STRING,
            "content": STRING,
            "append": {"type": "boolean", "description": "将这一段加到文件末尾，默认 false"},
        },
        ["path", "content"],
    ),
    tool_schema(
        "replace_file",
        "替换唯一匹配的文本片段。",
        {"path": STRING, "old": STRING, "new": STRING},
        ["path", "old", "new"],
    ),
    tool_schema("delete_file", "删除指定文件；请求批准模式下先审批。", {"path": STRING}, ["path"]),
    tool_schema(
        "run_python",
        "运行显式指定的 Python 脚本以获取任务所需的数据；真实路径参数由 args 传入，并声明 read_paths/write_paths。项目测试在反思节点的隔离副本运行。返回输出预览和完整日志路径。",
        {
            "purpose": RUN_PURPOSE,
            "path": STRING,
            "args": {"type": "array", "items": STRING},
            "retry_of": STRING,
            "read_paths": PATHS,
            "write_paths": PATHS,
        },
        ["purpose", "path", "args", "read_paths", "write_paths"],
    ),
    tool_schema(
        "run_command",
        "在当前工作副本中使用 bash、cmd 或 powershell 执行命令（解释器需已安装，CMD 仅限 Windows，Windows Bash 使用本机 Git Bash）。支持管道、重定向和多行命令；每次启动独立非交互进程，不保留终端状态。返回退出码和简短输出预览，完整输出见 stdout_log/stderr_log，可用 read_log 读取。PowerShell 错误会终止执行，原生程序退出码会保留。CMD 使用批处理语法（例如循环变量 %%i）。",
        {
            "purpose": RUN_PURPOSE,
            "command": {"type": "string", "minLength": 1},
            "shell": {"type": "string", "enum": ["bash", "cmd", "powershell"]},
            "retry_of": STRING,
            "read_paths": PATHS,
            "write_paths": PATHS,
        },
        ["purpose", "command", "shell", "read_paths", "write_paths"],
    ),
    tool_schema(
        "list_logs",
        "按路径分页列出本会话某次运行的日志，省略 run_id 使用当前运行。"
        "将日志 path 原样传给 read_log。翻页保持 run_id 相同，将 next_cursor 传给 cursor，null 表示末页。",
        {
            "run_id": STRING,
            "cursor": {"type": "string", "description": "第一页省略，后续传上一页的 next_cursor"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_DIRECTORY_LIMIT,
                "default": DEFAULT_DIRECTORY_LIMIT,
            },
        },
    ),
    tool_schema(
        "read_log",
        "按字节偏移读取日志（默认 16000 字节）。path 必须为 list_logs 返回的路径或 stdout_log/stderr_log 字段的值，例如 logs/abc/stdout.log，不能使用字段名本身。run_id 指定来源运行，省略使用当前运行；只允许本会话。next_offset 可继续翻页。",
        {"path": STRING, "offset": {"type": "integer"}, "run_id": STRING},
        ["path"],
    ),
    tool_schema(
        "read_execution_result",
        "只读本会话一次运行中执行节点的原始工具回执。省略 run_id 读本轮，历史轮次用其来源运行。先不填 event_id 列目录，用 next_before 翻页；再按整数 event_id 读正文，用 next_offset 续读。命令回执含完整日志路径。读取不会重放原操作。",
        {
            "run_id": STRING,
            "event_id": {"type": "integer", "minimum": 1},
            "before": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0},
        },
    ),
    tool_schema(
        "recall_memory",
        "结合语义与关键词主动召回项目及本会话记忆。",
        {"query": STRING, "keywords": {"type": "array", "items": STRING}},
        ["query"],
    ),
    tool_schema(
        "remember",
        "登记长期记忆候选；调用结束后批量判断更新或新增，现在不发布、不调用整理模型。",
        {
            "key": STRING,
            "content": STRING,
            "keywords": {"type": "array", "items": STRING},
            "scope": {"type": "string", "enum": ["project", "session"]},
            "expected_revision": {"type": "integer"},
        },
        ["key", "content", "keywords"],
    ),
    tool_schema(
        "update_memory",
        "登记记忆修订候选；使用稳定名称和主题关键词，结束时统一与相关旧记忆比较。",
        {
            "key": STRING,
            "content": STRING,
            "keywords": {"type": "array", "items": STRING},
            "scope": {"type": "string", "enum": ["project", "session"]},
            "expected_revision": {"type": "integer"},
        },
        ["key", "content", "keywords"],
    ),
    tool_schema(
        "list_skills",
        "列出启用的 Skill 名称与描述，最多20条；可用 query 按名称或描述过滤，再 load_skill 读取正文。",
        {"query": STRING},
    ),
    tool_schema(
        "load_skill",
        "按需加载 Skill 正文或其中的参考资料。",
        {"name": STRING, "resource": STRING},
        ["name"],
    ),
]
READONLY = {
    "list_files",
    "read_file",
    "search_file",
    "read_log",
    "read_execution_result",
    "list_logs",
    "recall_memory",
    "list_skills",
    "get_python_environment",
}


class ApprovalDenied(PermissionError):
    pass


class RunServices:
    legacy_approval_operation: str | None = None
    resumed_approval_operation: str | None = None

    def __init__(self, runtime: "Runtime", run: Run) -> None:
        self.runtime, self.run_id, self.project_id = runtime, run.id, run.project_id
        self.limits = run.limits
        self.session_id, self.generation = run.session_id, run.generation
        self.working_dir = Path(run.working_dir)
        self.legacy_intent = any(
            run.state.get(key) == "intent" for key in ("next_node", "input_return", "tool_role")
        )
        self.gateway = runtime.gateway_factory(run.id)
        self.memory = MemoryService(runtime.database, self.gateway)
        self.remote_tools: dict[str, BaseTool] = {}
        self.discovered = False
        self.discovery_errors: list[str] = []

    async def current(self) -> Run:
        async with self.runtime.database.session() as db:
            run = await db.get(Run, self.run_id)
            if run.generation != self.generation or run.status in {"rolled_back", "crashed"}:
                raise asyncio.CancelledError
            session = await db.get(Session, self.session_id)
            if session.workspace_mode == "worktree" and session.active_run_id != self.run_id:
                raise asyncio.CancelledError
            return run

    async def model(
        self,
        role: str,
        state: GraphState,
        messages: list[BaseMessage],
        *,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage | dict[str, Any]:
        run = await self.current()
        isolated = (
            role in {"test_designer", "test_designer_summary"} or schema is RequirementResolution
        )
        if (
            not isolated
            and role in {"executor", "planner", "reflection"}
            and messages
            and isinstance(messages[0], SystemMessage)
        ):
            location = f"\n当前工作副本绝对路径：{run.working_dir}。各节点的相对路径均从这里解析，项目根目录用 .，项目文件优先用相对路径。"
            if role != "reflection":
                environment = asdict(await self.runtime.environments.get(self.project_id))
                location += "\n文件权限：" + json.dumps(run.permission_policy, ensure_ascii=False)
                location += "\n项目 Python 运行环境：" + json.dumps(environment, ensure_ascii=False)
            messages = [
                messages[0].model_copy(update={"content": str(messages[0].content) + location}),
                *messages[1:],
            ]
        if self.discovery_errors and not isolated:
            messages = [
                SystemMessage(
                    content="外部工具当前不可用，不能假设已调用成功："
                    + "；".join(self.discovery_errors)
                ),
                *messages,
            ]
        response = await self.gateway.model(role, messages, schema=schema, tools=tools)
        await self.current()
        content = response if isinstance(response, dict) else message_to_dict(response)
        async with self.runtime.database.session.begin() as db:
            db.add(
                Message(session_id=self.session_id, run_id=self.run_id, role=role, content=content)
            )
        await self.event("usage", {"role": role, "usage": (await self.current()).usage})
        return response

    async def skills(self) -> Skills:
        async with self.runtime.database.session() as db:
            project = await db.get(Project, self.project_id)
        roots = [
            *self.runtime.settings.skill_dirs,
            self.runtime.settings.data_dir / "projects" / self.project_id / "skills",
        ]
        return Skills(
            roots, {name for name, enabled in project.skill_settings.items() if not enabled}
        )

    async def tools(self, state: GraphState, *, readonly: bool) -> list[dict[str, Any]]:
        if not readonly and not self.discovered:
            async with self.runtime.database.session() as db:
                servers = list(
                    await db.scalars(
                        select(MCPServer).where(
                            MCPServer.project_id == self.project_id, MCPServer.enabled.is_(True)
                        )
                    )
                )
            for server in servers:
                try:
                    async with asyncio.timeout(self.runtime.settings.command_timeout):
                        for tool in await discover_server(server.name, server.config):
                            self.remote_tools[tool.name] = tool
                except Exception as error:
                    self.discovery_errors.append(f"{server.name}: {type(error).__name__}: {error}")
                    await self.event(
                        "error", {"source": "mcp", "server": server.name, "error": str(error)}
                    )
            self.discovered = True
        local = [
            item for item in LOCAL_TOOLS if not readonly or item["function"]["name"] in READONLY
        ]
        definitions = deepcopy(
            local
            + (
                []
                if readonly
                else [convert_to_openai_tool(tool) for tool in self.remote_tools.values()]
            )
        )
        if not readonly:
            for definition in definitions:
                if definition["function"]["name"] in READONLY | {"ask_user"}:
                    continue
                parameters = definition["function"]["parameters"]
                parameters.setdefault("properties", {})["_permission"] = PERMISSION
        return definitions

    async def _authorize(
        self, name: str, arguments: dict[str, Any], state: GraphState, run: Run, operation_id: str
    ) -> None:
        policy = PermissionPolicy.model_validate(run.permission_policy or {})
        if name in {
            "run_python",
            "run_command",
            "verify_project",
            "install_dependencies",
            "configure_python_environment",
        }:
            environment = await self.runtime.environments.get(self.project_id)
            arguments["_environment"] = asdict(environment)
            if name == "configure_python_environment":
                selection = EnvironmentSelection.model_validate(
                    {k: v for k, v in arguments.items() if not k.startswith("_")}
                )
                if (
                    selection.mode == "default"
                    or not selection.python
                    and selection.mode == "managed"
                ):
                    target = await self.runtime.environments.defaults()
                else:
                    target = await self.runtime.environments.locate(
                        selection.python, selection.name, project_id=self.project_id
                    )
                # 固定本次审批实际使用的解释器，避免等待期间默认设置变化。
                arguments["python"] = target["python"]
                if selection.mode == "default":
                    arguments.update(mode="existing", name=target["name"])
                arguments["_environment_target"] = (
                    str(
                        self.runtime.settings.data_dir
                        / "environments"
                        / self.project_id
                        / (selection.name or "venv")
                    )
                    if selection.mode == "managed"
                    else target["prefix"]
                )
        remote = self.remote_tools.get(name)
        metadata = getattr(remote, "metadata", None) or {}
        annotations = metadata.get("mcp", {}).get("tool", {}).get("annotations", {})
        options = {
            "external": remote is not None,
            "external_readonly": annotations.get("read_only_hint", annotations.get("readOnlyHint"))
            is True,
            "legacy": self.legacy_approval_operation == operation_id,
        }
        request = permission_request(
            policy,
            Path(run.working_dir),
            name,
            arguments,
            operation_id,
            **options,
        )
        if request is None:
            if self.resumed_approval_operation == operation_id:
                raise ApprovalDenied("操作内容或路径已变化，原审批失效，请重新提交操作。")
            return
        if "_environment" in arguments and name != "configure_python_environment":
            try:
                arguments["_environment_hash"] = await asyncio.to_thread(environment.fingerprint)
            except ValueError:
                arguments["_environment_hash"] = "unavailable"
        request = permission_request(
            policy,
            Path(run.working_dir),
            name,
            arguments,
            operation_id,
            snapshot=current_snapshot(state),
            workspace_fingerprint=await asyncio.to_thread(
                workspace_fingerprint, Path(run.working_dir)
            ),
            **options,
        )
        if request is None:
            raise ApprovalDenied("操作内容或路径已变化，原审批失效，请重新提交操作。")
        rule = request.get("similar_approval")
        grants = run.approval_grants
        source = grants.get(rule["key"]) if rule else None
        if source and self.resumed_approval_operation != operation_id:
            decision = "auto_approved"
        else:
            answer = interrupt(request)
            # 恢复时先核对这次操作的完整指纹，再记住授权。
            if (
                not isinstance(answer, dict)
                or answer.get("request_id") != request["request_id"]
                or answer.get("decision") not in {"approve", "approve_similar", "reject"}
                or (answer["decision"] == "approve_similar" and not rule)
            ):
                raise ApprovalDenied("操作内容或路径已变化，原审批失效，请重新提交操作。")
            decision = answer["decision"]
            if decision == "approve_similar":
                async with self.runtime.database.session.begin() as db:
                    row = await db.get(Run, self.run_id, with_for_update=True)
                    if row.generation != self.generation or row.status != "running":
                        raise asyncio.CancelledError
                    row.approval_grants = {
                        **row.approval_grants,
                        rule["key"]: request["request_id"],
                    }
        await self.event(
            "permission_decision",
            {
                "request_id": request["request_id"],
                "tool": name,
                "decision": decision,
                **({"approved_by": source} if source else {}),
                "write_paths": request["write_paths"],
            },
        )
        if decision == "reject":
            raise ApprovalDenied("用户拒绝本次操作。请调整方案，不要换工具执行相同操作。")

    async def execute_tool(
        self, name: str, arguments: dict[str, Any], state: GraphState, *, readonly: bool = False
    ) -> dict[str, Any]:
        result, _ = await self.execute_tool_event(name, arguments, state, readonly=readonly)
        return result

    def read_source(self, name: str, arguments: dict) -> dict:
        return read_source(self.working_dir, name, arguments)

    async def execute_tool_event(
        self, name: str, arguments: dict[str, Any], state: GraphState, *, readonly: bool = False
    ) -> tuple[dict[str, Any], int]:
        run = await self.current()
        arguments = {
            key: value for key, value in arguments.items() if not key.startswith("_environment")
        }
        call_id = state["pending_tools"][0]["id"]
        operation_id = f"{state['execution_epoch']}:{state['sequence']}:{call_id}"
        if (
            not (readonly and name not in READONLY)
            and run.last_operation.get("operation_id") != operation_id
        ):
            # 必须在预算记账、日志创建和工具副作用之前中断。
            await self._authorize(name, arguments, state, run, operation_id)
        previous: dict[str, Any] | None = None
        result: dict[str, Any]
        async with self.runtime.database.session.begin() as db:
            row = await db.get(Run, self.run_id, with_for_update=True)
            if row.generation != self.generation or row.status != "running":
                raise asyncio.CancelledError
            if row.last_operation.get("operation_id") == operation_id:
                previous = row.last_operation
                if previous.get("event_id") and previous["result"] == "pending":
                    # 原事件是已完成操作的凭据。恢复时先核对归属，再补齐中断的状态。
                    event = await db.get(Event, previous["event_id"])
                    previous = dict(previous)
                    if (
                        event
                        and event.run_id == self.run_id
                        and event.kind == "tool"
                        and event.payload.get("operation_id") == operation_id
                    ):
                        previous["result"] = event.payload["result"]
                    else:
                        previous.pop("event_id")
                    row.last_operation = previous
            else:
                usage = dict(row.usage)
                limits = RunLimits.resolve(self.runtime.settings, row.limits)
                if limits.max_tool_calls and usage.get("tool_calls", 0) >= limits.max_tool_calls:
                    raise BudgetExceeded("达到工具调用次数上限。")
                usage["tool_calls"] = usage.get("tool_calls", 0) + 1
                operation = {
                    "kind": "tool",
                    "operation_id": operation_id,
                    "name": name,
                    "arguments": arguments,
                    "result": "pending",
                }
                if name in PROCESS_TOOLS:
                    operation["log_directory"] = f"logs/{new_id()}"
                row.usage, row.last_operation = usage, operation
                run = row
        if previous is not None:
            if previous["result"] != "pending":
                result = previous["result"]
                if previous.get("event_id"):
                    return result, previous["event_id"]
                # 恢复升级前已执行的只写回执，不重放工具，也不把现在的文件戳记为旧观察。
                event = tool_event_payload(state, name, arguments, result)
                return result, await self.record_tool_event(event)
            # 无完成结果的工具可能已产生副作用。恢复只补观察，不再执行或重复计费。
            result = {
                "ok": False,
                "error_type": "InterruptedToolCall",
                "result_unknown": True,
                "error": "此工具在上次请求取消时尚未保存完成结果，未自动重新执行。",
                "advice": "先检查文件、外部状态及日志，再决定下一步。",
            }
            if previous.get("log_directory"):
                result.update(
                    run_id=run.id,
                    stdout_log=f"{previous['log_directory']}/stdout.log",
                    stderr_log=f"{previous['log_directory']}/stderr.log",
                )
            result = self._log_metadata(name, arguments, result, state)
            return await self._record_tool(state, operation_id, name, arguments, result)
        source = self.read_source(name, arguments)
        if readonly and name not in READONLY:
            result = {
                "ok": False,
                "error": "当前只读节点不能调用写入或执行工具。",
                "error_type": "ReadOnlyViolation",
            }
        else:
            before = None
            if name in {"run_python", "run_command"} or name in self.remote_tools:
                before = await asyncio.to_thread(_fingerprint_if_readable, Path(run.working_dir))
            try:
                value = await self._execute(name, arguments, state, run)
                result = (
                    {"ok": True, **value}
                    if isinstance(value, dict)
                    else {"ok": True, "result": value}
                )
            except (BudgetExceeded, asyncio.CancelledError):
                raise
            except Exception as error:
                unknown = name in self.remote_tools and not isinstance(error, MCPToolError)
                result = {
                    "ok": False,
                    "error": str(error),
                    "error_type": type(error).__name__,
                    "result_unknown": unknown,
                    "advice": "结果未知时先检查外部状态，不要自动重放。"
                    if unknown
                    else "根据错误修正参数、代码或当前方法。",
                }
            if before is not None:
                after = await asyncio.to_thread(_fingerprint_if_readable, Path(run.working_dir))
                if after is not None:
                    result["workspace_changed"] = before != after
        result = self._log_metadata(name, arguments, result, state)
        if source and self.read_source(name, arguments) != source:
            source = {"path": source["path"]}  # 读取中途变化的文件不可复用。
        if source and "path" in result:
            result = {**result, "path": source["path"]}
        return await self._record_tool(state, operation_id, name, arguments, result, source)

    def _log_metadata(
        self, name: str, arguments: dict[str, Any], result: dict[str, Any], state: GraphState
    ) -> dict[str, Any]:
        if name not in PROCESS_TOOLS:
            return result
        return execution_metadata(
            name,
            arguments,
            result,
            log_id=f"{self.run_id}:tool:{state['tool_calls'] + 1}",
            run_id=self.run_id,
            tool_call_id=state["pending_tools"][0]["id"],
        )

    async def _record_tool(
        self,
        state: GraphState,
        operation_id: str,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        source: dict | None = None,
    ) -> tuple[dict[str, Any], int]:
        """原始结果与取消后的未知结果共用一处存储，迟到的旧代次不能覆盖现场。"""
        async with self.runtime.database.session.begin() as db:
            row = await db.get(Run, self.run_id, with_for_update=True)
            if (
                row.generation != self.generation
                or row.last_operation.get("operation_id") != operation_id
            ):
                raise asyncio.CancelledError
            if row.last_operation.get("event_id") and isinstance(
                row.last_operation["result"], dict
            ):
                return row.last_operation["result"], row.last_operation["event_id"]
            event = Event(
                run_id=self.run_id,
                kind="tool",
                payload=tool_event_payload(state, name, arguments, result, source=source),
            )
            db.add(event)
            await db.flush()
            row.last_operation = {**row.last_operation, "result": result, "event_id": event.id}
            db.add(
                Message(
                    session_id=self.session_id,
                    run_id=self.run_id,
                    role="tool",
                    content={
                        "name": name,
                        "arguments": arguments,
                        "result": result,
                        "operation_id": operation_id,
                    },
                )
            )
        return result, event.id

    async def record_tool_event(self, payload: dict) -> int:
        return await self._operation_event("tool", payload)

    async def _operation_event(self, kind: str, payload: dict) -> int:
        """检查点重试或批准恢复时，复用同一操作已提交的事件编号。"""
        async with self.runtime.database.session.begin() as db:
            run = await db.get(Run, self.run_id, with_for_update=True)
            if run.generation != self.generation or run.status != "running":
                raise asyncio.CancelledError
            existing = await db.scalar(
                select(Event)
                .where(
                    Event.run_id == self.run_id,
                    Event.kind == kind,
                    Event.payload["operation_id"].astext == payload["operation_id"],
                )
                .order_by(Event.id.desc())
                .limit(1)
            )
            if existing:
                if kind == "tool" and "file_revision" not in existing.payload:
                    existing.payload = {**payload, **existing.payload}
                return existing.id
            event = Event(run_id=self.run_id, kind=kind, payload=payload)
            db.add(event)
            await db.flush()
            return event.id

    async def tool_events(self, identifiers: list[int]) -> list[dict]:
        if not identifiers:
            return []
        async with self.runtime.database.session() as db:
            rows = await db.execute(
                select(Event.id, Event.payload).where(
                    Event.run_id == self.run_id,
                    Event.kind == "tool",
                    Event.id.in_(identifiers),
                )
            )
            by_id = dict(rows.all())
        return [{**by_id[key], "id": key} for key in identifiers if key in by_id]

    async def _execute(
        self, name: str, args: dict[str, Any], state: GraphState, run: Run
    ) -> Mapping[str, object] | list[str]:
        expected_environment = args.get("_environment")
        args = {key: value for key, value in args.items() if not key.startswith("_")}
        workdir = Path(run.working_dir)
        if name == "ask_user":
            return {"answer": args["answer"]}
        if name == "get_python_environment":
            return {
                "current": asdict(await self.runtime.environments.get(self.project_id)),
                "default": await self.runtime.environments.defaults(),
                "environments": await self.runtime.environments.choices(),
            }
        if name == "configure_python_environment":
            value = await self.runtime.environments.configure(
                self.project_id,
                EnvironmentSelection.model_validate(args),
                run_id=run.id,
                expected=expected_environment,
            )
            return {
                "environment": value,
                "notice": "项目环境已保存，后续执行和验收使用此环境。新建虚拟环境请安装 pytest 和所需依赖。",
            }
        file_tools = {
            "list_files": list_files,
            "read_file": read_file,
            "search_file": search_file,
            "write_file": write_file,
            "replace_file": replace_file,
            "delete_file": delete_file,
        }
        if name in file_tools:
            operation = asyncio.create_task(asyncio.to_thread(file_tools[name], workdir, **args))
            try:
                return await asyncio.shield(operation)
            except EditRejected as error:
                return {
                    "ok": False,
                    "changed": False,
                    "error_type": "ValueError",
                    "error": str(error),
                }
            except asyncio.CancelledError:
                # 文件系统线程不能被强行取消；等此次短操作结束后才能冻结差异预览。
                await operation
                raise
        if name in PROCESS_TOOLS:
            logdir = self.runtime.run_dir(run.id) / run.last_operation["log_directory"]
            if name == "install_dependencies":
                result = await self.runtime.environments.install(
                    self.project_id,
                    DependencyInstall.model_validate(
                        {k: v for k, v in args.items() if k != "retry_of"}
                    ),
                    workdir,
                    logdir,
                    run_id=run.id,
                    expected=expected_environment,
                )
            else:
                async with self.runtime.environments.use(self.project_id) as environment:
                    if (
                        expected_environment is not None
                        and asdict(environment) != expected_environment
                    ):
                        raise ValueError("项目环境已变化，请重新提交执行操作。")
                    options = {
                        "timeout": self.runtime.settings.command_timeout,
                        "preview_bytes": self.runtime.settings.tool_output_preview_bytes,
                        "python_environment": environment,
                    }
                    if name == "run_python":
                        result = await run_python(
                            workdir, args["path"], args.get("args", []), logdir, **options
                        )
                    else:
                        result = await run_command(
                            workdir, args["command"], args["shell"], logdir, **options
                        )
                    result["environment"] = asdict(environment)
            for key in ("stdout_log", "stderr_log"):
                result[key] = str(
                    Path(result[key]).relative_to(self.runtime.run_dir(run.id))
                ).replace("\\", "/")
            if execution_failed(result):
                # 常见错误全文进入排错结果；巨量日志有界读取，保留真正头尾与完整文件入口。
                for stream in ("stdout", "stderr"):
                    path = result[f"{stream}_log"]
                    page = self.runtime.read_log(run.id, path)
                    result[stream] = page["content"]
                    if page["next_offset"] < page["total_bytes"]:
                        head = self.runtime.read_log(run.id, path, limit=8000)
                        tail = self.runtime.read_log(
                            run.id, path, max(head["next_offset"], page["total_bytes"] - 8000)
                        )
                        result[stream] = (
                            head["content"]
                            + "\n…（诊断正文超长，中间部分可用 read_log 读取）…\n"
                            + tail["content"]
                        )
            return {**result, "run_id": run.id}
        if name == "read_execution_result":
            source_id = args.get("run_id") or run.id
            async with self.runtime.database.session() as db:
                event_ids, revision = state.get("tool_event_ids", []), state["task_revision"]
                if source_id != run.id:
                    source = (
                        await db.execute(
                            select(
                                Run.state["tool_event_ids"], Run.state["task_revision"].as_integer()
                            ).where(
                                Run.id == source_id,
                                Run.session_id == run.session_id,
                                Run.finished_at.is_not(None),
                                Run.status != "rolled_back",
                            )
                        )
                    ).one_or_none()
                    if source is None:
                        raise ValueError("只能回查本会话已结束且仍有效的运行")
                    event_ids, revision = source
                return await execution_result_page(db, source_id, event_ids or [], revision, args)
        if name in {"read_log", "list_logs"}:
            source_run_id = args.get("run_id") or run.id
            if source_run_id != run.id:
                source = await self.runtime.get_run(source_run_id)
                if source.session_id != run.session_id:
                    raise ValueError("只能读取本会话的运行日志。")
            if name == "list_logs":
                page = await asyncio.to_thread(
                    self.runtime.list_log_page,
                    source_run_id,
                    cursor=args.get("cursor", ""),
                    limit=args.get("limit", DEFAULT_DIRECTORY_LIMIT),
                )
                return {"run_id": source_run_id, **page}
            offset = max(0, args.get("offset", 0))
            result = {
                **self.runtime.read_log(source_run_id, args["path"], offset),
                "run_id": source_run_id,
                "path": args["path"],
                "offset": offset,
            }
            # read_log 成功只代表读到了文件，不代表源程序的错误已修复。
            async with self.runtime.database.session() as db:
                path = args["path"].replace("\\", "/")
                source = await db.scalar(
                    select(Message)
                    .where(
                        Message.run_id == source_run_id,
                        Message.role == "tool",
                        or_(
                            Message.content["result"]["stdout_log"].astext == path,
                            Message.content["result"]["stderr_log"].astext == path,
                        ),
                    )
                    .order_by(Message.id)
                    .limit(1)
                )
                metadata = None
                if source and source.content.get("name") in PROCESS_TOOLS:
                    original = source.content["result"]
                    metadata = execution_metadata(
                        source.content["name"],
                        source.content["arguments"],
                        original,
                        log_id=original.get("log_id", f"{source_run_id}:archived:{source.id}"),
                        run_id=source_run_id,
                        tool_call_id=original.get("tool_call_id", ""),
                    )
                elif path.startswith("verification/"):
                    parts = path.split("/")
                    if len(parts) == 5:
                        verification = await db.get(Verification, parts[1])
                        if verification and verification.run_id == source_run_id:
                            check = next(
                                (
                                    item
                                    for item in verification.checks
                                    if item.get("id") == parts[2]
                                ),
                                None,
                            )
                            if check:
                                metadata = verification_metadata(
                                    {
                                        "id": verification.id,
                                        "policy_hash": verification.policy_hash,
                                        "task_revision": verification.task_revision,
                                    },
                                    check,
                                    source_run_id,
                                )
                if metadata:
                    event = await db.scalar(
                        select(Event)
                        .where(
                            Event.kind == "log_resolution",
                            Event.payload["error_id"].astext == metadata["log_id"],
                        )
                        .order_by(Event.id.desc())
                        .limit(1)
                    )
                    if event:
                        metadata.update(
                            log_state="resolved", log_resolution=event.payload["resolution"]
                        )
                    result["log_source"] = {
                        key: value
                        for key, value in metadata.items()
                        if key
                        in {
                            "log_arguments",
                            "log_id",
                            "log_key",
                            "log_tool",
                            "log_state",
                            "log_resolution",
                            "run_id",
                            "stdout_log",
                            "stderr_log",
                        }
                    }
            return result
        if name == "recall_memory":
            return {
                "retrieval_mode": "hybrid" if self.gateway.embedding_enabled else "keyword",
                "memories": await self.memory.recall(
                    run.project_id,
                    run.session_id,
                    args["query"],
                    run.id,
                    keywords=args.get("keywords"),
                ),
            }
        if name in {"remember", "update_memory"}:
            return await self.memory.stage(
                run.id,
                args["key"],
                args["content"],
                run.session_id if args.get("scope") == "session" else None,
                keywords=args.get("keywords"),
                expected_revision=args.get("expected_revision"),
            )
        if name == "list_skills":
            catalog = await asyncio.to_thread((await self.skills()).catalog)
            query = args.get("query", "").casefold()
            matched = [
                {"name": item["name"], "description": item["description"]}
                for item in catalog
                if item["enabled"]
                and query in (item["name"] + " " + item["description"]).casefold()
            ]
            return {"skills": matched[:20], "matched": len(matched), "truncated": len(matched) > 20}
        if name == "load_skill":
            return await asyncio.to_thread(
                (await self.skills()).load, args["name"], workdir, args.get("resource", "SKILL.md")
            )
        if name in self.remote_tools:
            async with asyncio.timeout(self.runtime.settings.command_timeout):
                value = await invoke_tool(self.remote_tools[name], args)
            if hasattr(value, "model_dump"):
                return {"result": value.model_dump(mode="json")}
            return {"result": value}
        raise ValueError(f"未知工具：{name}")

    async def snapshot(self, state: GraphState) -> str:
        run = await self.current()
        return await asyncio.to_thread(
            self.runtime.workspace.snapshot, run.project_id, Path(run.working_dir)
        )

    async def verify(self, state: GraphState) -> dict[str, Any]:
        run = await self.current()
        try:
            await self._authorize(
                "verify_project",
                {
                    "checks": state["verification_policy"],
                    "acceptance_suite_ids": state.get("acceptance_suite_ids", []),
                    "scope": state.get("test_scope", {}),
                },
                state,
                run,
                f"verification:{state['execution_epoch']}:{state['sequence']}:{state['task_revision']}",
            )
        except ApprovalDenied as error:
            return {
                "status": "failed",
                "checks": [],
                "error": str(error),
                "permission_denied": True,
            }
        snapshot = await self.snapshot(state)
        expected = state.get("failed_gate_review", {}).get("retest_snapshot")
        if expected and snapshot != expected:
            return {
                "status": "error",
                "checks": [],
                "snapshot": snapshot,
                "error": "测试修正后候选文件再次变化，无法在同一实现上复测。",
            }
        await self.event("verification_started", {"snapshot": snapshot})
        result = await self.runtime.verification.run(
            self.project_id,
            snapshot,
            state["verification_policy"],
            run_id=self.run_id,
            task_revision=state["task_revision"],
            suite_ids=state.get("acceptance_suite_ids", []),
            scope=state.get("test_scope") or None,
        )
        await self.current()
        return result

    async def design_tests(self, state: GraphState, scope: dict[str, Any]) -> dict[str, Any]:
        await self.current()
        result = await self.runtime.acceptance.prepare(self, state, scope)
        await self.current()
        return result

    async def compile_tests(self, state: GraphState, identifier: str) -> dict[str, Any]:
        await self.current()
        result = await self.runtime.acceptance.compile(state, identifier)
        async with self.runtime.database.session() as db:
            result["suite_ids"] = await self.runtime.acceptance.required_ids(
                db, state, phase=state["test_scope"]["phase"]
            )
        await self.current()
        await self.event(
            "test_compiled", {"id": identifier, "cases": result["cases"], "context_isolated": True}
        )
        return result

    async def failed_test_sources(self, state: GraphState) -> dict[str, Any]:
        return await self.runtime.acceptance.failed_sources(state)

    async def correct_test(self, state: GraphState) -> dict[str, Any]:
        from .verification import verification_result

        pending, gate = state["failed_gate_review"], state["verification"]
        if pending["gate_id"] != gate["id"] or gate["task_revision"] != state["task_revision"]:
            raise ValueError("测试修正所依据的验收记录已过期")
        async with self.runtime.database.session() as db:
            record = await db.get(Verification, gate["id"])
            if (
                not record
                or record.run_id != self.run_id
                or record.status != "failed"
                or record.snapshot != gate["snapshot"]
                or record.scope != state["test_scope"]
                or record.task_revision != state["task_revision"]
            ):
                raise ValueError("没有与此次测试修正对应的真实失败记录")
            gate = verification_result(record)
        state = {**state, "verification": gate}
        edit = pending["correction"]
        before = gate["snapshot"]
        snapshot = await self.snapshot(state)
        workspace = self.runtime.workspace
        action_id = f"test-correction:{gate['id']}:{digest(edit)}"
        result = {"snapshot": snapshot, "suite_ids": state["acceptance_suite_ids"]}
        if edit["kind"] == "independent":
            if snapshot != before:
                raise ValueError("独立测试修正时实现快照已变化")
            result["suite_ids"] = await self.runtime.acceptance.correct_contract(
                state, IndependentTestFix.model_validate(edit), self.generation
            )
        else:
            fix = ProjectTestFix.model_validate(edit)
            _, files = failed_targets(gate)
            if fix.path not in files.get(fix.check_id, set()):
                raise ValueError("只能修改本次失败检查定位到的项目测试文件")
            check = next(c for c in gate["checks"] if c["id"] == fix.check_id)
            failed_names = {
                f["id"].partition("::")[2].split("[", 1)[0].rsplit("::", 1)[-1]
                for f in check.get("case_failures", [])
            }
            async with self.runtime.database.session() as db:
                suites = await self.runtime.acceptance.load(db, self.project_id, gate["suite_ids"])
            probe_entry = any(
                probe.get("target") == fix.path
                for suite in suites
                for case in suite["contract"]["cases"]
                for probe in case["probe"].get("steps", [case["probe"]])
            )
            script_entry = any(
                c["kind"] == "python" and c["path"] == fix.path for c in gate["checks"]
            )
            target = safe_path(self.working_dir, fix.path)
            if target != self.working_dir / fix.path:
                raise ValueError("测试修正不能沿链接写入其他文件")
            original = await asyncio.to_thread(workspace.read, self.project_id, before, fix.path)
            text = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            updated = corrected_test_text(
                text,
                fix,
                product_entry=probe_entry or script_entry,
                failed_functions=failed_names,
                standalone_assertions=check["kind"] == "python" and not probe_entry,
            ).encode("utf-8")
            args = {"path": fix.path, "old": fix.old, "new": fix.new}
            call = {"id": action_id, "name": "replace_file", "args": args}
            tool_state = {**state, "tool_role": "reflection", "pending_tools": [call]}
            if snapshot != before:
                changed = await asyncio.to_thread(
                    workspace.changed_paths, self.project_id, before, snapshot
                )
                last = (await self.current()).last_operation
                operation = f"{state['execution_epoch']}:{state['sequence']}:{action_id}"
                if (
                    last.get("operation_id") != operation
                    or changed != [fix.path]
                    or target.read_bytes() != updated
                ):
                    raise ValueError("诊断后的文件已变化，不能在该快照上应用修正")
            saved, event = await self.execute_tool_event("replace_file", args, tool_state)
            if not saved.get("ok") and not (snapshot != before and saved.get("result_unknown")):
                raise ValueError(str(saved.get("error", "测试文件修正失败")))
            result["tool_event"] = event
            snapshot = await self.snapshot(state)
            changed = await asyncio.to_thread(
                workspace.changed_paths, self.project_id, before, snapshot
            )
            if changed != [fix.path] or target.read_bytes() != updated:
                raise ValueError("复核发现测试文件以外的变化，已停止复测")
            result["snapshot"] = snapshot
        await self._operation_event(
            "test_correction",
            {
                "operation_id": action_id,
                "verification_id": gate["id"],
                "findings": pending["findings"],
                "correction": edit,
                "before": before,
                "after": result["snapshot"],
                "before_suite_ids": gate["suite_ids"],
                "after_suite_ids": result["suite_ids"],
            },
        )
        return result

    async def save_boundary(self, state: GraphState, *, snapshot: str | None = None) -> str:
        snapshot = snapshot or await self.snapshot(state)
        async with self.runtime.database.session.begin() as db:
            cursor = await db.scalar(
                select(func.max(MemoryDraft.id)).where(MemoryDraft.run_id == self.run_id)
            )
            candidate_cursor = await db.scalar(
                select(func.max(MemoryCandidate.id)).where(MemoryCandidate.run_id == self.run_id)
            )
            row = Boundary(
                id=new_id(),
                run_id=self.run_id,
                snapshot=snapshot,
                state={
                    **state,
                    "snapshot_id": snapshot,
                    "snapshot_revision": state.get("file_revision", 0),
                },
                draft_cursor=cursor or 0,
                candidate_cursor=candidate_cursor or 0,
            )
            db.add(row)
            return row.id

    async def restore_boundary(self, boundary_id: str, state: GraphState) -> GraphState:
        async with self.runtime.database.session() as db:
            boundary = await db.get(Boundary, boundary_id)
            if boundary is None or boundary.run_id != self.run_id:
                raise ValueError("步骤边界不属于当前调用。")
            session_id = (await db.get(Run, self.run_id)).session_id
        async with self.runtime.database.session.begin() as db:
            session = await db.get(Session, session_id, with_for_update=True)
            run = await db.get(Run, self.run_id, with_for_update=True)
            if (
                run.generation != self.generation
                or session.active_run_id != self.run_id
                or run.finished_at is not None
            ):
                raise asyncio.CancelledError
            if session.workspace_mode == "worktree":
                target = await self.runtime.session_workspaces.restore(
                    session, boundary.snapshot, committed=False
                )
            else:
                target = self.runtime.run_dir(self.run_id) / f"work-{new_id()}"
                await asyncio.to_thread(
                    self.runtime.workspace.materialize, self.project_id, boundary.snapshot, target
                )
            run.working_dir = str(target)
            anchor = boundary.state.get("step_index", 0)
            discarded = [
                identifier
                for position, identifier in boundary_map(state).items()
                if int(position) > anchor
            ]
            if discarded:
                await db.execute(
                    delete(Boundary).where(
                        Boundary.run_id == self.run_id, Boundary.id.in_(discarded)
                    )
                )
            # 旧后缀的测试仍可审计，但不再作为活动阶段契约；最终仍检查保留前缀与新后缀。
            for suite in await db.scalars(
                select(AcceptanceSuite).where(
                    AcceptanceSuite.run_id == self.run_id, AcceptanceSuite.active.is_(True)
                )
            ):
                if suite.scope.get("through_step", 0) > anchor:
                    suite.active = False
            await db.execute(
                delete(MemoryDraft).where(
                    MemoryDraft.run_id == self.run_id, MemoryDraft.id > boundary.draft_cursor
                )
            )
            await db.execute(
                delete(MemoryCandidate).where(
                    MemoryCandidate.run_id == self.run_id,
                    MemoryCandidate.id > boundary.candidate_cursor,
                )
            )
        self.working_dir = target
        return cast(GraphState, {**boundary.state, "snapshot_id": boundary.snapshot})

    async def control(self, state: GraphState) -> GraphControl:
        run = await self.current()
        return {"pause": run.pause_requested, "instructions": run.instructions}

    async def event(self, kind: str, payload: dict[str, object]) -> None:
        if kind in {"tool_reused", "verification"} and "operation_id" in payload:
            await self._operation_event(kind, payload)
            return
        # 普通过程事件用于展示与续订。审查引用的工具结果另由事务与检查点关联。
        async with self.runtime.database.session.begin() as db:
            db.add(
                Event(
                    run_id=self.run_id,
                    kind=kind,
                    payload=json.loads(json.dumps(payload, default=str)),
                )
            )

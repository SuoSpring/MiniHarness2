"""MCP 发现和调用的唯一适配入口，保留错误语义与连接生命周期。"""

import re
from contextvars import ContextVar
from typing import Any, Literal, NotRequired, Protocol, TypedDict
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.elicitation import ElicitRequestParams, ElicitResult
from langchain.mcp import MCPAdapter
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import FunctionDescription, convert_to_openai_tool
from mcp.client import ClientRequestContext, InputRequiredRoundsExceededError

from miniharness.tools.asyncio_bridge import with_subprocess_loop

_interaction_errors: ContextVar[list[str] | None] = ContextVar(
    "mcp_interaction_errors", default=None
)


class MCPToolError(RuntimeError):
    """服务端已执行工具并明确报告失败。"""


class MCPInteractionRequired(RuntimeError):
    """首版不支持 MCP 调用中途交互，不自动重放可能已产生副作用的调用。"""


class MCPServerConfig(TypedDict):
    transport: Literal["stdio", "http", "streamable_http"]
    command: NotRequired[str | None]
    args: NotRequired[list[str]]
    env: NotRequired[dict[str, str]]
    url: NotRequired[str | None]
    headers: NotRequired[dict[str, str]]


class MCPServerDescription(TypedDict):
    tools: list[FunctionDescription]


class InvokableTool(Protocol):
    @property
    def name(self) -> str: ...

    async def ainvoke(self, call: ToolCall, /) -> object: ...


async def _decline_interaction(
    message: str,
    response_type: type[object] | None,
    params: ElicitRequestParams,
    context: ClientRequestContext,
) -> ElicitResult[None]:
    reason = (
        f"MCP 服务请求中途用户交互：{message}。当前不支持此交互，请先检查外部状态，不要自动重试。"
    )
    errors = _interaction_errors.get()
    if errors is not None:
        errors.append(reason)
    return ElicitResult(action="decline")


async def discover_server(name: str, config: MCPServerConfig) -> list[BaseTool]:
    """返回可在发现连接关闭后使用的 LangChain 工具，名称带服务器前缀。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("MCP 服务器名称只能包含字母、数字、下划线和连字符")
    if config["transport"] == "stdio":
        target = {
            "command": config["command"],
            "args": config.get("args", []),
            "env": config.get("env", {}),
        }
    elif config["transport"] in {"http", "streamable_http"}:
        target = {"url": config["url"], "transport": "http", "headers": config.get("headers", {})}
    else:
        raise ValueError("MCP 仅支持 stdio 和 HTTP 传输")

    async def discover() -> list[BaseTool]:
        # 自己提供交互处理器，防止适配器在没有图上下文的线程里调用 interrupt。
        client = Client(
            {"mcpServers": {name: target}},
            elicitation_handler=_decline_interaction,
            input_required_max_rounds=0,
        )
        async with MCPAdapter(client) as adapter:
            tools = await adapter.list_tools()
        for tool in tools:
            tool.name = f"mcp_{name}__{tool.name}"
        return tools

    return await with_subprocess_loop(discover)


async def invoke_tool(tool: InvokableTool, arguments: dict[str, Any]) -> object:
    """返回内容与结构化结果；服务端错误抛异常，避免被运行时误记为成功。"""

    async def invoke() -> object:
        interactions: list[str] = []
        token = _interaction_errors.set(interactions)
        try:
            try:
                result = await tool.ainvoke(
                    {
                        "type": "tool_call",
                        "id": uuid4().hex,
                        "name": tool.name,
                        "args": arguments,
                    }
                )
            except InputRequiredRoundsExceededError as error:
                raise MCPInteractionRequired(
                    "MCP 服务需要中途交互或继续请求；当前不支持，请检查外部状态，不要自动重试。"
                ) from error
            if interactions:
                raise MCPInteractionRequired("\n".join(interactions))
            if isinstance(result, ToolMessage):
                if result.status == "error":
                    raise MCPToolError(str(result.content))
                return {"content": result.content, "artifact": result.artifact}
            return result
        finally:
            _interaction_errors.reset(token)

    return await with_subprocess_loop(invoke)


async def describe_server(name: str, config: MCPServerConfig) -> MCPServerDescription:
    return {
        "tools": [
            convert_to_openai_tool(tool)["function"] for tool in await discover_server(name, config)
        ]
    }

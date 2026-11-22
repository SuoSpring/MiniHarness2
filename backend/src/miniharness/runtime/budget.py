import json
import logging
from asyncio import sleep, timeout_at, to_thread
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, TypeVar

import tiktoken
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, messages_to_dict
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, LengthFinishReasonError
from pydantic import BaseModel

from miniharness.config import Settings
from miniharness.runtime.chat import ReasoningChatOpenAI, is_deepseek, reasoning_fields
from miniharness.runtime.errors import BudgetExceeded, HarnessError
from miniharness.runtime.limits import RunLimits
from miniharness.storage import Database
from miniharness.storage.models import Run, UsageEntry

_encoding = tiktoken.get_encoding("cl100k_base")
logger = logging.getLogger(__name__)
_T = TypeVar("_T")
NETWORK_RETRIES = 3
# 跟随当前运行的异步上下文，收尾阶段新建的网关也继承剩余总时限。
model_request_deadline: ContextVar[float | None] = ContextVar(
    "model_request_deadline", default=None
)


def retryable_network_error(error: BaseException) -> bool:
    return isinstance(error, APIConnectionError) or (
        isinstance(error, APIStatusError)
        and (error.status_code in {408, 429} or error.status_code >= 500)
    )


def count_tokens(value: object) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return len(_encoding.encode(text, disallowed_special=()))


class ModelGateway:
    """显式重试并逐次计量；中途失败仅记日志，不进入模型上下文。"""

    def __init__(self, settings: Settings, database: Database, run_id: str | None = None) -> None:
        self.settings, self.database, self.run_id = settings, database, run_id

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.settings.embedding_model)

    async def limits(self) -> RunLimits:
        if not self.run_id:
            return RunLimits.resolve(self.settings)
        async with self.database.session() as db:
            run = await db.get(Run, self.run_id)
            return RunLimits.resolve(self.settings, run.limits)

    async def reserve(self, role: str, tokens: int, *, model: bool = True) -> None:
        if not self.run_id:
            return
        async with self.database.session.begin() as db:
            run = await db.get(Run, self.run_id, with_for_update=True)
            usage = dict(run.usage)
            limits = RunLimits.resolve(self.settings, run.limits)
            finishing = role in {
                "reflection",
                "test_designer",
                "test_designer_summary",
                "memory_consolidation",
            }
            reserve = 0 if finishing else limits.max_output_tokens * 2
            if (
                run.token_budget > 0
                and usage.get("total_tokens", 0) + tokens + reserve > run.token_budget
            ):
                raise BudgetExceeded("本次调用的令牌预算不足，已停止新增工作。")
            if model and limits.max_rounds and usage.get("rounds", 0) >= limits.max_rounds:
                raise BudgetExceeded(
                    f"达到模型轮数上限（已请求 {usage.get('rounds', 0)} 次，上限 {limits.max_rounds} 次；包含规划、摘要、反思等请求）。"
                )
            if limits.run_timeout and usage.get("active_seconds", 0) >= limits.run_timeout:
                raise BudgetExceeded("达到执行时间上限。")
            if model:
                usage["rounds"] = usage.get("rounds", 0) + 1
            run.usage = usage
            child = {"kind": "model" if model else "embedding", "role": role}
            if (
                run.last_operation.get("kind") == "tool"
                and run.last_operation.get("result") == "pending"
            ):
                # 记忆工具内部的嵌入请求不能覆盖外层工具启动凭据，取消恢复仍需识别该副作用。
                run.last_operation = {**run.last_operation, "child_operation": child}
            else:
                run.last_operation = child

    async def charge(
        self, role: str, input_tokens: int, output_tokens: int = 0, estimated: bool = False
    ) -> None:
        if not self.run_id:
            return
        async with self.database.session.begin() as db:
            run = await db.get(Run, self.run_id, with_for_update=True)
            usage = dict(run.usage)
            for key, amount in (
                ("input_tokens", input_tokens),
                ("output_tokens", output_tokens),
                ("total_tokens", input_tokens + output_tokens),
            ):
                usage[key] = usage.get(key, 0) + amount
            usage["estimated"] = usage.get("estimated", False) or estimated
            run.usage = usage
            db.add(
                UsageEntry(
                    run_id=self.run_id,
                    role=role,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated=estimated,
                )
            )

    async def _request(
        self,
        role: str,
        invoke: Callable[[], Awaitable[_T]],
        estimated_input: int,
        *,
        model: bool = True,
        max_output_tokens: int = 0,
    ) -> _T:
        async with timeout_at(model_request_deadline.get()):
            for attempt in range(NETWORK_RETRIES + 1):
                # 每次实际发送都检查预算并记账；SDK 重试保持关闭，避免重复重试和漏账。
                await self.reserve(role, estimated_input + max_output_tokens, model=model)
                try:
                    return await invoke()
                except BaseException as error:
                    await self.charge(role, estimated_input, estimated=True)
                    # 用户取消和任务总时限也会到这里，必须立即传播，不得重新发请求。
                    if not retryable_network_error(error) or attempt == NETWORK_RETRIES:
                        raise
                    delay = 2**attempt
                    logger.warning(
                        "模型请求暂时失败，%s 秒后重试 %s/%s (run=%s, role=%s, error=%s)",
                        delay,
                        attempt + 1,
                        NETWORK_RETRIES,
                        self.run_id,
                        role,
                        type(error).__name__,
                    )
                    await sleep(delay)
        raise AssertionError("不可达的模型重试状态")

    async def model(
        self,
        role: str,
        messages: list[BaseMessage],
        *,
        schema: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AIMessage | dict[str, Any]:
        if not self.settings.llm_model:
            raise HarnessError("请先配置 LLM_MODEL 与模型服务地址。")
        messages = deepcopy(messages)
        structured = schema is not None and not tools
        # DeepSeek 思考模式不接受强制指定工具；纯结构化输出改用 JSON 模式并校验模型。
        json_mode = structured and is_deepseek(self.settings.llm_model, self.settings.llm_base_url)
        if schema and (tools or json_mode):
            tool_guidance = ""
            if tools:
                tool_guidance = (
                    "先依据已有工具事件核对；资料充足时直接提交审查结果。"
                    if role == "reflection"
                    else "可先使用只读工具核对事实。"
                )
            messages = [
                SystemMessage(
                    content=tool_guidance
                    + "最终请输出符合此结构的 JSON："
                    + json.dumps(schema.model_json_schema(), ensure_ascii=False)
                ),
                *messages,
            ]
        estimated_input = count_tokens(messages_to_dict(messages)) + count_tokens(tools or [])
        if structured and not json_mode:
            estimated_input += count_tokens(schema.model_json_schema())
        limits = await self.limits()
        if (
            limits.max_context_tokens
            and estimated_input + limits.max_output_tokens > limits.max_context_tokens
        ):
            raise BudgetExceeded("固定上下文与工具定义超过模型窗口，请缩短固定信息。")
        client = ReasoningChatOpenAI(
            model=self.settings.llm_model,
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            max_retries=0,
            timeout=limits.llm_timeout or None,
            max_tokens=limits.max_output_tokens or None,
        )
        runnable = client
        if structured:
            runnable = client.with_structured_output(
                schema, method="json_mode" if json_mode else "function_calling", include_raw=True
            )
        elif tools:
            runnable = client.bind_tools(deepcopy(tools))

        async def invoke():
            try:
                # 始终重发原始输入，隔离客户端的潜在原地修改，不添加重试提示或错误消息。
                return await runnable.ainvoke(deepcopy(messages))
            except LengthFinishReasonError as exc:
                # 已有完整响应和真实用量；输出截断沿用原恢复流程，不触发网络重试。
                raw = client.recover_truncated_completion(exc.completion)
                return {"raw": raw, "parsed": None, "parsing_error": exc} if structured else raw

        response = await self._request(
            role, invoke, estimated_input, max_output_tokens=limits.max_output_tokens
        )
        raw = response["raw"] if structured else response
        usage = raw.usage_metadata
        await self.charge(
            role,
            usage["input_tokens"] if usage else estimated_input,
            usage["output_tokens"]
            if usage
            else count_tokens(
                {
                    "content": raw.content,
                    "tool_calls": raw.tool_calls,
                    "invalid_tool_calls": raw.invalid_tool_calls,
                    **reasoning_fields(raw),
                }
            ),
            not bool(usage),
        )
        if raw.response_metadata.get("finish_reason") == "length":
            # 即使截断响应恰好能被解析，也保留原始结束原因，交给图节点处理。
            return raw
        if structured:
            if response["parsing_error"] or response["parsed"] is None:
                return raw  # 图节点把格式错误反馈给模型，纠错请求仍经过统一记账。
            return response["parsed"].model_dump()
        return response

    async def embed(self, text: str) -> list[float]:
        if not self.settings.embedding_model:
            raise HarnessError("请配置独立的 EMBEDDING_MODEL 后使用向量记忆。")
        estimate = count_tokens(text)
        if self.settings.embedding_model.startswith("local:"):
            from miniharness.memory.local_embeddings import encode

            directory = self.settings.embedding_model.removeprefix("local:")
            vector, tokens = await self._request(
                "embedding",
                lambda: to_thread(encode, directory, text),
                estimate,
                model=False,
            )
            if len(vector) != self.settings.embedding_dimensions:
                raise HarnessError(
                    f"本地嵌入维度为 {len(vector)}，与 EMBEDDING_DIMENSIONS 不一致。"
                )
            await self.charge("embedding", tokens)
            return vector
        limits = await self.limits()
        async with AsyncOpenAI(
            api_key=self.settings.embedding_api_key,
            base_url=self.settings.embedding_base_url,
            max_retries=0,
            timeout=limits.llm_timeout or None,
        ) as client:
            result = await self._request(
                "embedding",
                lambda: client.embeddings.create(model=self.settings.embedding_model, input=text),
                estimate,
                model=False,
            )
        usage = getattr(result, "usage", None)
        await self.charge(
            "embedding", usage.total_tokens if usage else estimate, estimated=usage is None
        )
        vector = result.data[0].embedding
        if len(vector) != self.settings.embedding_dimensions:
            raise HarnessError(f"嵌入维度为 {len(vector)}，与 EMBEDDING_DIMENSIONS 不一致。")
        return vector

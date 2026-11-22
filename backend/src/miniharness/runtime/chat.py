"""OpenAI 兼容服务的消息适配，保留思考模式工具循环所需的原始推理字段。"""

from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from openai import BaseModel as OpenAIModel

REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_details")


def reasoning_fields(message: AIMessage) -> dict[str, Any]:
    return {
        key: deepcopy(message.additional_kwargs[key])
        for key in REASONING_FIELDS
        if message.additional_kwargs.get(key) is not None
    }


def merge_reasoning_details(parts: list[dict]) -> list[dict]:
    """按原顺序组装流式片段；只拼正文/签名，不能把重复的类型、格式或 ID 拼坏。"""
    result: list[dict] = []
    for part in parts:
        identity = part.get("index", part.get("id"))
        previous = result[-1] if result else None
        if (
            previous is None
            or identity is None
            or identity != previous.get("index", previous.get("id"))
        ):
            result.append(deepcopy(part))
            continue
        for key, value in part.items():
            if value is None:
                previous.setdefault(key, None)
            elif key in {"text", "summary", "data", "signature"} and isinstance(value, str):
                previous[key] = (previous.get(key) or "") + value
            elif previous.get(key) is None:
                previous[key] = deepcopy(value)
            elif previous[key] != value:
                raise ValueError(f"推理片段的 {key} 不一致，无法无损回传")
    return result


def is_deepseek(model: str, base_url: str) -> bool:
    if urlsplit(base_url).hostname == "openrouter.ai":
        return False  # OpenRouter 使用自己的字段与参数协议，不按底层模型名套 DeepSeek 协议。
    model_name = model.lower().rsplit("/", 1)[-1]
    return urlsplit(base_url).hostname == "api.deepseek.com" or model_name.startswith("deepseek-")


class ReasoningChatOpenAI(ChatOpenAI):
    """保存兼容接口的推理扩展；正文、签名与加密块按原字段回传。"""

    def recover_truncated_completion(self, completion: OpenAIModel) -> AIMessage:
        """SDK 的结构化解析可能提前抛出长度异常；还原其中已有的响应和真实用量。"""
        result = self._create_chat_result(completion)
        generation = result.generations[0]
        message = generation.message
        if not isinstance(message, AIMessage):
            raise TypeError("截断的模型响应必须是助手消息")
        message.response_metadata = {
            **(result.llm_output or {}),
            **(generation.generation_info or {}),
            **message.response_metadata,
        }
        return message

    def _create_chat_result(
        self, response: dict | OpenAIModel, generation_info: dict | None = None
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data["choices"], strict=True):
            if isinstance(generation.message, AIMessage):
                for key in REASONING_FIELDS:
                    if choice["message"].get(key) is not None:
                        generation.message.additional_kwargs[key] = deepcopy(choice["message"][key])
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        choices = chunk.get("choices") or []
        if result and isinstance(result.message, AIMessageChunk) and choices:
            delta = choices[0].get("delta") or {}
            for key in REASONING_FIELDS:
                if delta.get(key) is not None:
                    result.message.additional_kwargs[key] = deepcopy(delta[key])
        return result

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        details: list[dict] = []
        for chunk in super()._stream(*args, **kwargs):
            details.extend(chunk.message.additional_kwargs.pop("reasoning_details", []) or [])
            yield chunk
        if details:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_details": merge_reasoning_details(details)},
                )
            )

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        # 标准 AIMessageChunk 合并器会拼接重复的 format 等字符串，细节块集中组装后发出。
        details: list[dict] = []
        async for chunk in super()._astream(*args, **kwargs):
            details.extend(chunk.message.additional_kwargs.pop("reasoning_details", []) or [])
            yield chunk
        if details:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_details": merge_reasoning_details(details)},
                )
            )

    def _get_request_payload(
        self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "messages" not in payload:
            return payload
        deepseek = is_deepseek(self.model_name, self.openai_api_base or "")
        openrouter = urlsplit(self.openai_api_base or "").hostname == "openrouter.ai"
        messages = self._convert_input(input_).to_messages()
        for original, outgoing in zip(messages, payload["messages"], strict=True):
            if isinstance(original, AIMessage):
                fields = reasoning_fields(original)
                if openrouter and fields.get("reasoning_details"):
                    # 有结构化细节时完整回传，避免把同一段明文与细节重复发送。
                    fields = {"reasoning_details": fields["reasoning_details"]}
                outgoing.update(fields)
                if deepseek and "reasoning_content" not in fields:
                    # 框架生成的收尾摘要及旧消息没有推理正文；显式为空，不伪造推理。
                    outgoing["reasoning_content"] = ""
        if deepseek and "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        if (
            urlsplit(self.openai_api_base or "").hostname == "api.deepseek.com"
            and self.model_name in {"deepseek-flash", "deepseek-v4-pro"}
            and 0 < (payload.get("max_tokens") or 0) <= 2048
        ):
            # 该服务将推理与工具参数计入同一额度。短输出时给可执行内容留空间。
            payload.setdefault("reasoning_effort", "low")
        return payload

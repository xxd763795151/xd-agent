from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from xd_agent.agent.models import Message, ModelReply, ToolCall, Usage
from xd_agent.core.config import AgentSettings


class ProviderError(RuntimeError):
    """模型请求失败或响应结构无效时抛出的异常。"""

    pass


class ModelProvider(Protocol):
    """模型提供方接口；generate 返回统一格式的回复和用量。"""

    async def generate(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        """根据上下文和工具定义生成一次模型回复。"""
        ...


class HttpModelProvider:
    """通过 HTTP 适配 OpenAI 兼容与 Anthropic 兼容接口。"""

    def __init__(self, settings: AgentSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        if settings.api_key is None or not settings.api_key.get_secret_value():
            raise ValueError("APP_AGENT__API_KEY is required to run the agent")
        self.key = settings.api_key.get_secret_value()

    async def generate(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        """根据配置选择模型协议并生成统一格式的回复。"""
        if self.settings.provider == "openai_compatible":
            return await self._openai(messages, tools)
        return await self._anthropic(messages, tools)

    async def _post(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        """发送模型请求，并将网络或 JSON 错误转换为 ProviderError。"""
        try:
            response = await self.client.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"model request failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict):
            raise ProviderError("model returned an invalid JSON object")
        return body

    async def _openai(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        """转换消息与工具定义，调用 OpenAI 兼容接口并解析回复。"""
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            elif message.role == "assistant" and message.tool_calls:
                converted.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
            else:
                converted.append({"role": message.role, "content": message.content})
        payload: dict[str, Any] = {"model": self.settings.model, "messages": converted}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": definition} for definition in tools
            ]
        base = self.settings.base_url or "https://open.bigmodel.cn/api/paas/v4"
        body = await self._post(
            f"{base.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {self.key}"},
            payload,
        )
        try:
            answer = body["choices"][0]["message"]
            calls = tuple(
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=json.loads(call["function"]["arguments"]),
                )
                for call in answer.get("tool_calls") or []
            )
            usage = body.get("usage") or {}
            return ModelReply(
                message=Message(
                    role="assistant", content=answer.get("content") or "", tool_calls=calls
                ),
                usage=Usage(
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                ),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("model returned an invalid completion") from exc

    async def _anthropic(self, messages: list[Message], tools: list[dict[str, Any]]) -> ModelReply:
        """转换消息与工具定义，调用 Anthropic 兼容接口并解析回复。"""
        system = "\n\n".join(message.content for message in messages if message.role == "system")
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content,
                                "is_error": message.is_error,
                            }
                        ],
                    }
                )
            elif message.role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                converted.append({"role": "assistant", "content": blocks})
            else:
                converted.append({"role": message.role, "content": message.content})
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": 4096,
            "system": system,
            "messages": converted,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": definition["name"],
                    "description": definition["description"],
                    "input_schema": definition["parameters"],
                }
                for definition in tools
            ]
        base = self.settings.base_url or "https://open.bigmodel.cn/api/anthropic"
        body = await self._post(
            f"{base.rstrip('/')}/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            payload,
        )
        try:
            blocks = body["content"]
            content = "".join(block.get("text", "") for block in blocks if block["type"] == "text")
            calls = tuple(
                ToolCall(id=block["id"], name=block["name"], arguments=block["input"])
                for block in blocks
                if block["type"] == "tool_use"
            )
            usage = body.get("usage") or {}
            return ModelReply(
                message=Message(role="assistant", content=content, tool_calls=calls),
                usage=Usage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("model returned an invalid message") from exc

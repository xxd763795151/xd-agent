from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True, frozen=True)
class ToolCall:
    """模型请求的一次工具调用；id 关联结果，name 和 arguments 指定操作。"""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """将工具调用转换为可持久化的字典。"""
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ToolCall:
        """从持久化字典还原工具调用。"""
        return cls(id=str(value["id"]), name=str(value["name"]), arguments=value["arguments"])


@dataclass(slots=True, frozen=True)
class Message:
    """跨模型提供方通用的消息结构。

    role 表示发言方，content 为文本。tool_calls 为助手发起的工具调用；
    tool_call_id 将工具消息关联到原调用；is_error 标记工具失败。
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        """将消息及嵌套工具调用序列化为字典。"""
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "is_error": self.is_error,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Message:
        """从会话存储中的字典还原消息。"""
        return cls(
            role=value["role"],
            content=value.get("content", ""),
            tool_calls=tuple(ToolCall.from_dict(call) for call in value.get("tool_calls", [])),
            tool_call_id=value.get("tool_call_id"),
            is_error=bool(value.get("is_error", False)),
        )


@dataclass(slots=True, frozen=True)
class Usage:
    """一次模型回复消耗的输入和输出 token 数。"""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True, frozen=True)
class ModelReply:
    """模型回复消息及对应的 token 用量。"""

    message: Message
    usage: Usage = field(default_factory=Usage)


@dataclass(slots=True, frozen=True)
class ToolResult:
    """工具调用结果；call_id 关联原调用，name 标识工具，is_error 表示失败。"""

    call_id: str
    name: str
    output: str
    is_error: bool = False

    def as_message(self) -> Message:
        """转换为关联原调用 ID 的工具角色消息。"""
        return Message(
            role="tool", content=self.output, tool_call_id=self.call_id, is_error=self.is_error
        )

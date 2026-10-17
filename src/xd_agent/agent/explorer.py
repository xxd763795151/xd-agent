from __future__ import annotations

from typing import Any

from xd_agent.agent.context import compose_context
from xd_agent.agent.models import Message, Usage
from xd_agent.agent.provider import ModelProvider
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import ToolRegistry
from xd_agent.core.config import AgentSettings


class ExploreWorkspace:
    """供主 Agent 调用的只读探索工具，最多进行十轮模型交互。"""

    name = "explore_workspace"
    read_only = True
    description = "让只读探索助手检查较多文件，并返回简短调查结果。"
    parameters = {
        "type": "object",
        "properties": {"task": {"type": "string"}},
        "required": ["task"],
    }

    def __init__(
        self,
        provider: ModelProvider,
        registry: ToolRegistry,
        settings: AgentSettings,
        store: SessionStore,
        session_id: str,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.settings = settings
        self.store = store
        self.session_id = session_id

    async def execute(self, arguments: dict[str, Any]) -> str:
        """用只读工具调查指定任务，并返回模型的简要结论。"""
        task = arguments["task"]
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be nonempty text")
        system = Message(
            role="system",
            content=(
                "你负责只读调查。只使用提供的文件工具核实信息，找到依据后以中文简要汇报。"
                "如果证据不足，应说明缺口。"
            ),
        )
        history = [Message(role="user", content=task)]
        for _ in range(10):
            context = compose_context(system, history, task, self.settings)
            reply = await self.provider.generate(context, self.registry.definitions())
            self._record_usage(reply.usage)
            history.append(reply.message)
            if not reply.message.tool_calls:
                return reply.message.content
            for call in reply.message.tool_calls:
                result = await self.registry.execute(call)
                history.append(result.as_message())
        raise RuntimeError("exploration exceeded the 10 turn limit")

    def _record_usage(self, usage: Usage) -> None:
        """将探索模型的 token 用量及估算成本计入主会话。"""
        cost = (
            usage.input_tokens * self.settings.input_price_per_million
            + usage.output_tokens * self.settings.output_price_per_million
        ) / 1_000_000
        self.store.record_usage(self.session_id, usage, cost)

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from xd_agent.agent.context import build_system_prompt, compose_context
from xd_agent.agent.models import Message, Usage
from xd_agent.agent.provider import ModelProvider
from xd_agent.agent.reporting import AgentEvent, Reporter
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import ToolRegistry
from xd_agent.core.config import AgentSettings

logger = logging.getLogger(__name__)


class MaxTurnsExceeded(RuntimeError):
    """模型循环达到配置的最大轮数时抛出的异常。"""

    pass


class AgentEngine:
    """驱动模型与工具交替执行，持久化消息、用量和运行追踪。"""

    def __init__(
        self,
        provider: ModelProvider,
        store: SessionStore,
        registry: ToolRegistry,
        settings: AgentSettings,
    ) -> None:
        self.provider = provider
        self.store = store
        self.registry = registry
        self.settings = settings

    async def run(
        self,
        session_id: str,
        workdir: Path,
        prompt: str,
        reporter: Reporter,
        *,
        plan_mode: bool = False,
    ) -> str:
        """恢复会话并执行模型循环，返回没有工具调用时的最终回复。"""
        snapshot = self.store.get_or_create(session_id, workdir)
        user_message = Message(role="user", content=prompt)
        self.store.append(session_id, user_message)
        history = [*snapshot.messages, user_message]
        system = build_system_prompt(
            workdir,
            plan_mode=plan_mode,
            max_chars=min(30_000, self.settings.max_context_chars // 3),
        )
        started = time.perf_counter()
        trace: dict[str, Any] = {
            "session_id": session_id,
            "model": self.settings.model,
            "turns": [],
        }
        last_failure: str | None = None
        consecutive_failures = 0
        try:
            for turn in range(1, self.settings.max_turns + 1):
                context = compose_context(system, history, prompt, self.settings)
                reply = await self.provider.generate(context, self.registry.definitions())
                cost = self._cost(reply.usage)
                self.store.record_usage(session_id, reply.usage, cost)
                logger.info(
                    "model turn=%s session=%s input_tokens=%s output_tokens=%s cost=%.6f %s",
                    turn,
                    session_id,
                    reply.usage.input_tokens,
                    reply.usage.output_tokens,
                    cost,
                    self.settings.currency,
                )
                message = reply.message
                if message.role != "assistant":
                    raise ValueError("provider must return an assistant message")
                self.store.append(session_id, message)
                history.append(message)
                turn_trace: dict[str, Any] = {"turn": turn, "tools": []}
                trace["turns"].append(turn_trace)
                if message.content:
                    await reporter.emit(AgentEvent("message", message.content))
                if not message.tool_calls:
                    return message.content
                for call in message.tool_calls:
                    await reporter.emit(AgentEvent("tool_start", "", tool_name=call.name))
                    result = await self.registry.execute(call)
                    turn_trace["tools"].append({"name": call.name, "error": result.is_error})
                    self.store.append(session_id, result.as_message())
                    history.append(result.as_message())
                    await reporter.emit(
                        AgentEvent("tool_result", result.output[:200], call.name, result.is_error)
                    )
                    encoded = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
                    fingerprint = f"{call.name}:{encoded}"
                    if result.is_error:
                        consecutive_failures = (
                            consecutive_failures + 1 if fingerprint == last_failure else 1
                        )
                        last_failure = fingerprint
                    else:
                        last_failure = None
                        consecutive_failures = 0
                    if consecutive_failures >= 3:
                        reminder = Message(
                            role="user",
                            content=(
                                f"工具 {call.name} 用相同参数已连续失败三次。"
                                "请停止重复调用，检查报错并换一种方法；无法解决时向用户说明。"
                            ),
                        )
                        self.store.append(session_id, reminder)
                        history.append(reminder)
                        consecutive_failures = 0
            await reporter.emit(
                AgentEvent("notice", f"任务达到最大轮数 {self.settings.max_turns}，已停止。")
            )
            raise MaxTurnsExceeded(f"任务超过最大轮数 {self.settings.max_turns}")
        except Exception as exc:
            trace["error_type"] = type(exc).__name__
            raise
        finally:
            trace["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self._save_trace(workdir, trace)

    def _cost(self, usage: Usage) -> float:
        """按输入、输出 token 单价估算本轮成本。"""
        return (
            usage.input_tokens * self.settings.input_price_per_million
            + usage.output_tokens * self.settings.output_price_per_million
        ) / 1_000_000

    @staticmethod
    def _save_trace(workdir: Path, trace: dict[str, Any]) -> None:
        """将简要运行追踪写入工作区；写入失败只记录日志。"""
        try:
            directory = workdir / ".xd-agent" / "traces"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"trace_{uuid4().hex}.json"
            path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("failed to save agent trace")

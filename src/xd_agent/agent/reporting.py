from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

import typer

EventKind = Literal["message", "tool_start", "tool_result", "notice"]


@dataclass(slots=True, frozen=True)
class AgentEvent:
    """Agent 运行事件；tool_name 和 is_error 用于工具进度及结果。"""

    kind: EventKind
    text: str
    tool_name: str | None = None
    is_error: bool = False


class Reporter(Protocol):
    """进度输出接口；emit 将 Agent 事件发送给具体渠道。"""

    async def emit(self, event: AgentEvent) -> None:
        """向目标渠道发送运行事件。"""
        ...


class NullReporter:
    """丢弃运行事件，供不需要实时输出的场景使用。"""

    async def emit(self, event: AgentEvent) -> None:
        """忽略事件。"""
        return None


class TerminalReporter:
    """以文本或 JSON 格式向终端输出运行事件。"""

    def __init__(self, *, json_output: bool = False) -> None:
        self.json_output = json_output

    async def emit(self, event: AgentEvent) -> None:
        """按事件类型输出消息、工具状态或通知。"""
        if self.json_output:
            typer.echo(json.dumps(asdict(event), ensure_ascii=False))
            return
        if event.kind == "message":
            typer.echo(event.text)
        elif event.kind == "tool_start":
            typer.echo(f"→ {event.tool_name}", err=True)
        elif event.kind == "tool_result":
            status = "失败" if event.is_error else "完成"
            typer.echo(f"← {event.tool_name}: {status}", err=True)
            if event.is_error:
                typer.echo(event.text, err=True)
        else:
            typer.echo(event.text, err=True)


logger = logging.getLogger(__name__)

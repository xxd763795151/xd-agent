from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from xd_agent.agent.approval import LocalApproval, TerminalApproval
from xd_agent.agent.conversations import ConversationControl
from xd_agent.agent.provider import ProviderError
from xd_agent.agent.reporting import AgentEvent, TerminalReporter
from xd_agent.cli.context import run_with_runtime
from xd_agent.cli.errors import ExitCode
from xd_agent.core.config import resolve_project_path
from xd_agent.core.runtime import Runtime

logger = logging.getLogger(__name__)


def run(
    prompt: Annotated[str, typer.Argument(help="交给 Agent 的任务")],
    directory: Annotated[Path | None, typer.Option("--dir", help="工作区目录")] = None,
    session: Annotated[str | None, typer.Option("--session", help="可复用的会话 ID")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="维护 PLAN.md 和 TODO.md")] = False,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="显式自动批准有副作用的工具调用")
    ] = False,
    debug: Annotated[bool, typer.Option("--debug", help="模型服务错误时打印异常堆栈")] = False,
) -> None:
    """执行一次 Agent 任务，可指定工作区、会话及计划模式。"""

    async def action(runtime: Runtime) -> None:
        """在选定工作区和会话中执行单次任务。"""
        workdir = (
            directory.resolve()
            if directory
            else resolve_project_path(runtime.settings.agent.workspace_dir)
        )
        reporter = TerminalReporter()
        chat_id = session or uuid4().hex
        conversations = ConversationControl(runtime.sessions, "cli")
        notice = conversations.handle_command(prompt, chat_id)
        if notice is not None:
            await reporter.emit(AgentEvent("notice", notice))
            return
        await runtime.agent.run(
            prompt,
            session_id=conversations.session_id(chat_id),
            workdir=workdir,
            reporter=reporter,
            approval=LocalApproval() if auto_approve else TerminalApproval(runtime.sessions),
            plan_mode=plan,
        )

    try:
        run_with_runtime(action)
    except ProviderError as exc:
        logger.error("agent_error: %s", exc, exc_info=debug)
        raise typer.Exit(ExitCode.UNAVAILABLE) from exc
    except ValueError as exc:
        typer.echo(f"invalid_request: {exc}", err=True)
        raise typer.Exit(ExitCode.INVALID_USAGE) from exc
    except KeyboardInterrupt as exc:
        raise typer.Exit(ExitCode.INTERRUPTED) from exc
    except Exception as exc:
        logger.exception("agent run failed")
        typer.echo(f"agent_error: {exc}", err=True)
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from exc


def chat(
    directory: Annotated[Path | None, typer.Option("--dir", help="工作区目录")] = None,
    session: Annotated[str | None, typer.Option("--session", help="可复用的会话 ID")] = None,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="显式自动批准有副作用的工具调用")
    ] = False,
    debug: Annotated[bool, typer.Option("--debug", help="模型服务错误时打印异常堆栈")] = False,
) -> None:
    """在固定会话中循环接收终端输入，直到用户退出。"""

    async def action(runtime: Runtime) -> None:
        """在同一会话中持续处理终端输入。"""
        workdir = (
            directory.resolve()
            if directory
            else resolve_project_path(runtime.settings.agent.workspace_dir)
        )
        chat_id = session or uuid4().hex
        conversations = ConversationControl(runtime.sessions, "cli")
        reporter = TerminalReporter()
        typer.echo(
            f"会话 {conversations.session_id(chat_id)}；输入 /new 开启新会话，/exit 退出。",
            err=True,
        )
        while True:
            prompt = typer.prompt("你", prompt_suffix="> ").strip()
            if prompt.lower() in {"/exit", "/quit"}:
                return
            notice = conversations.handle_command(prompt, chat_id)
            if notice is not None:
                await reporter.emit(AgentEvent("notice", notice))
                continue
            if prompt:
                await runtime.agent.run(
                    prompt,
                    session_id=conversations.session_id(chat_id),
                    workdir=workdir,
                    reporter=reporter,
                    approval=(
                        LocalApproval() if auto_approve else TerminalApproval(runtime.sessions)
                    ),
                )

    try:
        run_with_runtime(action)
    except ProviderError as exc:
        logger.error("agent_error: %s", exc, exc_info=debug)
        raise typer.Exit(ExitCode.UNAVAILABLE) from exc
    except ValueError as exc:
        typer.echo(f"invalid_request: {exc}", err=True)
        raise typer.Exit(ExitCode.INVALID_USAGE) from exc
    except (KeyboardInterrupt, EOFError, typer.Abort):
        return
    except Exception as exc:
        logger.exception("agent chat failed")
        typer.echo(f"agent_error: {exc}", err=True)
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from exc

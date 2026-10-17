from __future__ import annotations

import asyncio
from pathlib import Path

from xd_agent.agent.approval import ApprovalGate
from xd_agent.agent.engine import AgentEngine
from xd_agent.agent.explorer import ExploreWorkspace
from xd_agent.agent.provider import ModelProvider
from xd_agent.agent.reporting import Reporter
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import (
    EditFile,
    ListFiles,
    ReadFile,
    ReadToolOutput,
    RunCommand,
    SearchText,
    ToolRegistry,
    WorkspaceFiles,
    WriteFile,
)
from xd_agent.core.config import AgentSettings
from xd_agent.core.logging import session_id_context


class AgentService:
    """Agent 入口服务，按会话串行运行并组装工作区工具。

    属性：_locks 为进程内会话锁，防止同一会话并发写入。
    """

    def __init__(
        self, provider: ModelProvider | None, store: SessionStore, settings: AgentSettings
    ) -> None:
        self.provider = provider
        self.store = store
        self.settings = settings
        self._locks: dict[str, asyncio.Lock] = {}

    async def run(
        self,
        prompt: str,
        *,
        session_id: str,
        workdir: Path,
        reporter: Reporter,
        approval: ApprovalGate | None = None,
        chat_id: str | None = None,
        plan_mode: bool = False,
    ) -> str:
        """校验任务与工作区，创建工具和引擎后执行指定会话。"""
        if approval is None:
            raise ValueError("approval policy is required; select an explicit approval strategy")
        if self.provider is None:
            raise ValueError("APP_AGENT__API_KEY is required to run the agent")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(prompt) > self.settings.max_context_chars // 2:
            raise ValueError("prompt exceeds half of the configured context budget")
        root = workdir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            token = session_id_context.set(session_id)
            try:
                files = WorkspaceFiles(
                    root,
                    min(self.settings.max_tool_output_chars, self.settings.max_context_chars // 4),
                )
                read_only = ToolRegistry(
                    [
                        ReadFile(files),
                        ListFiles(files),
                        SearchText(files),
                        ReadToolOutput(files.outputs),
                    ],
                    outputs=files.outputs,
                )
                registry = ToolRegistry(
                    [
                        ReadFile(files),
                        ReadToolOutput(files.outputs),
                        ListFiles(files),
                        SearchText(files),
                        WriteFile(files),
                        EditFile(files),
                        RunCommand(files, self.settings),
                        ExploreWorkspace(
                            self.provider,
                            read_only,
                            self.settings,
                            self.store,
                            session_id,
                            outputs=files.outputs,
                        ),
                    ],
                    approval=approval,
                    reporter=reporter,
                    chat_id=chat_id,
                    store=self.store,
                    session_id=session_id,
                    outputs=files.outputs,
                )
                engine = AgentEngine(self.provider, self.store, registry, self.settings)
                return await engine.run(
                    session_id,
                    root,
                    prompt,
                    reporter,
                    plan_mode=plan_mode,
                )
            finally:
                session_id_context.reset(token)

from __future__ import annotations

import asyncio
import copy
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from xd_agent.agent.approval import ApprovalDecision, ApprovalGate, ApprovalRequest
from xd_agent.agent.models import ToolCall, ToolResult
from xd_agent.agent.reporting import NullReporter, Reporter
from xd_agent.agent.sessions import SessionStore
from xd_agent.core.config import AgentSettings


class AgentTool(Protocol):
    """工具接口；只有无副作用的工具才能额外声明 read_only = True 以免审。"""

    name: str
    description: str
    parameters: dict[str, Any]

    async def execute(self, arguments: dict[str, Any]) -> str:
        """根据参数执行工具并返回可供模型读取的文本。"""
        ...


class WorkspaceFiles:
    """管理工作区路径及工具输出长度上限。"""

    def __init__(self, root: Path, output_limit: int) -> None:
        self.root = root.resolve()
        self.output_limit = output_limit

    def resolve(self, raw: str) -> Path:
        """解析相对路径，并拒绝绝对路径及解析后越界的路径。"""
        if not raw or Path(raw).is_absolute():
            raise ValueError("path must be relative to the workspace")
        candidate = (self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path leaves the workspace")
        return candidate


class ReadFile:
    """读取工作区内的 UTF-8 文本文件。"""

    name = "read_file"
    read_only = True
    description = "读取工作区内一个文本文件的内容。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def execute(self, arguments: dict[str, Any]) -> str:
        """读取文件，并在超过输出上限时截断。"""
        path = self.files.resolve(str(arguments["path"]))
        with path.open("r", encoding="utf-8") as stream:
            content = stream.read(self.files.output_limit + 1)
        if len(content) > self.files.output_limit:
            return content[: self.files.output_limit] + "\n[文件内容已截断]"
        return content


class WriteFile:
    """在工作区中原子创建或覆盖文本文件。"""

    name = "write_file"
    description = "创建或覆盖工作区内的文本文件。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def execute(self, arguments: dict[str, Any]) -> str:
        """校验文本大小后写入文件，最多接受 200 万字符。"""
        path = self.files.resolve(str(arguments["path"]))
        content = arguments["content"]
        if not isinstance(content, str):
            raise ValueError("content must be text")
        if len(content) > 2_000_000:
            raise ValueError("file content exceeds the write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)
        return f"已写入 {path.relative_to(self.files.root)}"


class EditFile:
    """对工作区文本文件执行唯一匹配的替换。"""

    name = "edit_file"
    description = "在工作区文件中唯一匹配旧文本，并进行一次替换。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def execute(self, arguments: dict[str, Any]) -> str:
        """唯一定位旧文本，必要时兼容换行符差异，再原子写回。"""
        path = self.files.resolve(str(arguments["path"]))
        old = arguments["old_text"]
        new = arguments["new_text"]
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ValueError("old_text must be nonempty and new_text must be text")
        with path.open("r", encoding="utf-8", newline="") as stream:
            original = stream.read()
        count = original.count(old)
        if count == 0:
            line_ending = "\r\n" if "\r\n" in original else "\n"
            normalized = original.replace("\r\n", "\n")
            normalized_old = old.replace("\r\n", "\n")
            count = normalized.count(normalized_old)
            if count == 1:
                replaced = normalized.replace(normalized_old, new.replace("\r\n", "\n"), 1)
                updated = replaced.replace("\n", line_ending)
            else:
                raise ValueError(
                    f"old_text matched {count} locations; provide exact unique context"
                )
        elif count == 1:
            updated = original.replace(old, new, 1)
        else:
            raise ValueError(f"old_text matched {count} locations; provide exact unique context")
        _atomic_write(path, updated)
        return f"已修改 {path.relative_to(self.files.root)}"


def _atomic_write(path: Path, content: str) -> None:
    """在同目录写临时文件后替换目标文件，并保留已有权限。"""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            stream.write(content)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


class ListFiles:
    """递归列出工作区文件，跳过内部目录并限制结果数量。"""

    name = "list_files"
    read_only = True
    description = "列出工作区内的文件，可指定相对目录。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }

    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def execute(self, arguments: dict[str, Any]) -> str:
        """按目录和文件名排序返回最多 500 个相对路径。"""
        root = self.files.resolve(str(arguments.get("path") or "."))
        if not root.is_dir():
            raise ValueError("path is not a directory")
        names: list[str] = []
        for directory, subdirs, filenames in os.walk(root):
            subdirs[:] = sorted(
                name for name in subdirs if name not in {".git", ".venv", ".xd-agent"}
            )
            for name in sorted(filenames):
                names.append(str((Path(directory) / name).relative_to(self.files.root)))
                if len(names) >= 500:
                    break
            if len(names) >= 500:
                break
        return "\n".join(names[:500]) or "目录为空"


class SearchText:
    """在工作区内搜索文本并返回匹配位置。"""

    name = "search_text"
    read_only = True
    description = "在工作区文本文件中搜索字符串，返回文件名、行号和匹配行。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["query"],
    }

    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def execute(self, arguments: dict[str, Any]) -> str:
        """搜索小型 UTF-8 文件，限制扫描文件数及匹配结果数。"""
        query = arguments["query"]
        if not isinstance(query, str) or not query:
            raise ValueError("query must be nonempty text")
        root = self.files.resolve(str(arguments.get("path") or "."))
        if not root.is_dir():
            raise ValueError("path is not a directory")
        matches: list[str] = []
        inspected = 0
        for directory, subdirs, filenames in os.walk(root):
            subdirs[:] = sorted(
                name for name in subdirs if name not in {".git", ".venv", ".xd-agent"}
            )
            for name in sorted(filenames):
                path = Path(directory) / name
                if path.is_symlink() or path.stat().st_size > 1_000_000:
                    continue
                inspected += 1
                if inspected > 300:
                    break
                try:
                    with path.open("r", encoding="utf-8") as stream:
                        for line_number, line in enumerate(stream, 1):
                            if query in line:
                                label = path.relative_to(self.files.root)
                                matches.append(f"{label}:{line_number}: {line.rstrip()[:200]}")
                                if len(matches) >= 100:
                                    return "\n".join(matches)
                except (OSError, UnicodeError):
                    continue
            if inspected > 300:
                break
        return "\n".join(matches) or "未找到匹配内容"


class RunCommand:
    """在工作区执行宿主机命令，并限制运行时间及输出长度。"""

    name = "run_command"
    description = "在工作区运行命令，返回退出状态和输出。命令可以访问系统资源。"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def __init__(self, files: WorkspaceFiles, settings: AgentSettings) -> None:
        self.files = files
        self.settings = settings

    async def execute(self, arguments: dict[str, Any]) -> str:
        """调用当前平台的 shell，收集输出并报告超时或非零退出码。"""
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be nonempty text")
        if sys.platform == "win32":
            shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
            command = (
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                "$ErrorActionPreference='Stop'; " + command
            )
        else:
            shell = ["/bin/bash", "-lc"]
        process = await asyncio.create_subprocess_exec(
            *shell,
            command,
            cwd=self.files.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def collect_output() -> bytes:
            """持续排空子进程输出，仅保留配置允许的前缀。"""
            assert process.stdout is not None
            captured = bytearray()
            while chunk := await process.stdout.read(4096):
                remaining = self.files.output_limit - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
            await process.wait()
            return bytes(captured)

        try:
            output = await asyncio.wait_for(
                collect_output(), timeout=self.settings.command_timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RuntimeError("command timed out") from exc
        text = output.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"command exited with status {process.returncode}: {text}")
        return text or "命令执行成功，无输出"


class ToolRegistry:
    """统一校验、审批和审计工具调用；只有显式声明只读的工具可免审。"""

    def __init__(
        self,
        tools: list[AgentTool],
        *,
        approval: ApprovalGate | None = None,
        reporter: Reporter | None = None,
        chat_id: str | None = None,
        store: SessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self.approval = approval
        self.reporter = reporter if reporter is not None else NullReporter()
        self.chat_id = chat_id
        self.store = store
        self.session_id = session_id

    def definitions(self) -> list[dict[str, Any]]:
        """生成发送给模型的工具名称、说明和参数模式。"""
        return [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in self._tools.values()
        ]

    async def execute(self, call: ToolCall) -> ToolResult:
        """在实际执行入口审批，缺少策略时拒绝有副作用或未分类的工具。"""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, f"未知工具：{call.name}", True)
        approval_id: str | None = None
        executing = False
        try:
            if not isinstance(call.arguments, dict):
                raise ValueError("tool arguments must be an object")
            required = tool.parameters.get("required", [])
            missing = [key for key in required if key not in call.arguments]
            if missing:
                raise ValueError("missing required arguments: " + ", ".join(missing))
            call = ToolCall(call.id, call.name, copy.deepcopy(call.arguments))
            if getattr(tool, "read_only", False) is not True:
                approval_id = uuid4().hex[:12]
                if self.store is not None:
                    self.store.create_approval(
                        approval_id,
                        call,
                        session_id=self.session_id,
                        chat_id=self.chat_id,
                        policy=(
                            type(self.approval).__name__ if self.approval is not None else "none"
                        ),
                    )
                if self.approval is None:
                    decision = ApprovalDecision(False, "rejected", "未配置审批策略，工具未执行")
                else:
                    decision = await self.approval.authorize(
                        ApprovalRequest(approval_id, copy.deepcopy(call), self.chat_id),
                        reporter=self.reporter,
                    )
                if self.store is not None:
                    self.store.record_approval_decision(
                        approval_id, decision.status, decision.reason, user_id=decision.user_id
                    )
                if not decision.allowed:
                    return ToolResult(call.id, call.name, decision.reason, True)
                if self.store is not None:
                    self.store.record_approval_execution(approval_id, "running")
                executing = True
            output = await tool.execute(call.arguments)
            if approval_id is not None and self.store is not None:
                self.store.record_approval_execution(approval_id, "succeeded")
            return ToolResult(call.id, call.name, output, False)
        except asyncio.CancelledError:
            if approval_id is not None and self.store is not None:
                if executing:
                    self.store.record_approval_execution(
                        approval_id, "cancelled", "等待工具结果被取消，实际副作用未知，请检查状态"
                    )
                else:
                    self.store.record_approval_decision(approval_id, "cancelled", "审批等待被取消")
                    record = self.store.get_approval(approval_id)
                    if record is not None and record["status"] == "approved":
                        self.store.record_approval_execution(
                            approval_id, "cancelled", "已批准，但在工具开始执行前取消"
                        )
            raise
        except Exception as exc:
            if approval_id is not None and self.store is not None:
                error = f"{type(exc).__name__}: {exc}"
                if executing:
                    self.store.record_approval_execution(approval_id, "failed", error)
                else:
                    self.store.record_approval_decision(approval_id, "error", error)
            return ToolResult(call.id, call.name, f"{type(exc).__name__}: {exc}", True)

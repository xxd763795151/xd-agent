from __future__ import annotations

import asyncio
import codecs
import copy
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from xd_agent.agent.approval import (
    MAX_APPROVAL_BYTES,
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
)
from xd_agent.agent.command_review import CommandReviewer
from xd_agent.agent.models import ToolCall, ToolResult
from xd_agent.agent.outputs import ToolOutputStore, page_arguments, read_page
from xd_agent.agent.reporting import AgentEvent, NullReporter, Reporter
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
        self.outputs = ToolOutputStore(self.root, output_limit)

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
    description = "分页读取工作区文本文件；offset 是字符偏移，按续读提示取回后续内容。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    }

    def __init__(self, files: WorkspaceFiles) -> None:
        self.files = files

    async def execute(self, arguments: dict[str, Any]) -> str:
        """分页读取文件，将续读提示也纳入工具输出预算。"""
        path = self.files.resolve(str(arguments["path"]))
        offset, limit = page_arguments(arguments, self.files.output_limit)
        return read_page(
            path,
            offset,
            limit,
            lambda next_offset: f"\n[read_file: 同一 path, offset={next_offset}]",
        )


class ReadToolOutput:
    """按字符分页取回已存档的长输出，不能访问工作区外的文件。"""

    name = "read_tool_output"
    read_only = True
    description = "分页读取工具输出存档；使用输出提示中的 output_id 和 offset。"
    parameters = {
        "type": "object",
        "properties": {
            "output_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["output_id"],
    }

    def __init__(self, outputs: ToolOutputStore) -> None:
        self.outputs = outputs

    async def execute(self, arguments: dict[str, Any]) -> str:
        offset, limit = page_arguments(arguments, self.outputs.limit)
        return self.outputs.read(str(arguments["output_id"]), offset, limit)


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
        output_id, output_path = self.files.outputs.create()
        process = await asyncio.create_subprocess_exec(
            *shell,
            command,
            cwd=self.files.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def collect_output() -> None:
            """流式存档完整输出，内存只保留一个数据块。"""
            assert process.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            with output_path.open("w", encoding="utf-8", newline="") as stream:
                while chunk := await process.stdout.read(4096):
                    stream.write(decoder.decode(chunk))
                stream.write(decoder.decode(b"", final=True))
            await process.wait()

        try:
            await asyncio.wait_for(collect_output(), timeout=self.settings.command_timeout_seconds)
        except TimeoutError as exc:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise RuntimeError(f"command timed out; output_id={output_id}") from exc
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        text = self.files.outputs.read(output_id)
        if process.returncode != 0:
            raise RuntimeError(f"command exited with status {process.returncode}: {text}")
        return text or "命令执行成功，无输出"


class ToolRegistry:
    """统一校验、审批和审计工具调用，支持对命令进行独立的只读判断。"""

    def __init__(
        self,
        tools: list[AgentTool],
        *,
        approval: ApprovalGate | None = None,
        reporter: Reporter | None = None,
        chat_id: str | None = None,
        store: SessionStore | None = None,
        session_id: str | None = None,
        outputs: ToolOutputStore | None = None,
        command_reviewer: CommandReviewer | None = None,
        auto_approve_read_only_commands: bool = True,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self.approval = approval
        self.reporter = reporter if reporter is not None else NullReporter()
        self.chat_id = chat_id
        self.store = store
        self.session_id = session_id
        self.outputs = outputs
        self.command_reviewer = command_reviewer
        self.auto_approve_read_only_commands = auto_approve_read_only_commands

    def _result(self, call: ToolCall, output: str, is_error: bool) -> ToolResult:
        """所有成功、拒绝和异常结果均在同一出口限长。"""
        if self.outputs is not None:
            output = self.outputs.bound(output)
        elif len(output) > 8000:
            output = output[:7980] + "\n[工具输出已截断]"
        return ToolResult(call.id, call.name, output, is_error)

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
            return self._result(call, f"未知工具：{call.name}", True)
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
                description = ""
                read_only_command = False
                if call.name == "run_command" and self.command_reviewer is not None:
                    command = call.arguments.get("command")
                    if not isinstance(command, str) or not command.strip():
                        raise ValueError("command must be nonempty text")
                    # 与人工审批相同的硬限制，不将超大命令发给模型或自动执行。
                    if len(json.dumps(call.arguments, ensure_ascii=False).encode("utf-8")) > (
                        MAX_APPROVAL_BYTES
                    ):
                        raise ValueError("审批参数超过 64KB，请拆分工具调用后重试")
                    assessment = await self.command_reviewer.assess(command)
                    description = assessment.describe()
                    read_only_command = assessment.read_only
                    if self.store is not None:
                        self.store.record_command_assessment(approval_id, description)
                if self.approval is None:
                    decision = ApprovalDecision(False, "rejected", "未配置审批策略，工具未执行")
                elif read_only_command and self.auto_approve_read_only_commands:
                    await self.reporter.emit(
                        AgentEvent(
                            "notice",
                            f"命令只读免审。\n执行命令：{call.arguments['command']}\n{description}",
                        )
                    )
                    decision = ApprovalDecision(True, "approved", description, "command-review")
                else:
                    decision = await self.approval.authorize(
                        ApprovalRequest(
                            approval_id, copy.deepcopy(call), self.chat_id, description
                        ),
                        reporter=self.reporter,
                    )
                if self.store is not None:
                    self.store.record_approval_decision(
                        approval_id, decision.status, decision.reason, user_id=decision.user_id
                    )
                if not decision.allowed:
                    return self._result(call, decision.reason, True)
                if self.store is not None:
                    self.store.record_approval_execution(approval_id, "running")
                executing = True
            output = await tool.execute(call.arguments)
            if approval_id is not None and self.store is not None:
                self.store.record_approval_execution(approval_id, "succeeded")
            return self._result(call, output, False)
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
            return self._result(call, f"{type(exc).__name__}: {exc}", True)

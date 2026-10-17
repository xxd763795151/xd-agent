import asyncio
from pathlib import Path

import pytest

from xd_agent.agent.models import ToolCall
from xd_agent.agent.tools import (
    EditFile,
    ReadFile,
    RunCommand,
    ToolRegistry,
    WorkspaceFiles,
    WriteFile,
)
from xd_agent.core.config import AgentSettings


def test_file_tools_reject_escape_and_edit_only_unique_match(tmp_path: Path) -> None:
    files = WorkspaceFiles(tmp_path, 1000)
    with pytest.raises(ValueError, match="leaves the workspace"):
        files.resolve("../outside.txt")

    async def scenario() -> None:
        registry = ToolRegistry([ReadFile(files), WriteFile(files), EditFile(files)])
        written = await registry.execute(
            ToolCall("1", "write_file", {"path": "a.txt", "content": "one\r\ntwo\r\n"})
        )
        assert not written.is_error
        edited = await registry.execute(
            ToolCall(
                "2", "edit_file", {"path": "a.txt", "old_text": "one\ntwo", "new_text": "three"}
            )
        )
        assert not edited.is_error
        assert (tmp_path / "a.txt").read_bytes() == b"three\r\n"
        rejected = await registry.execute(
            ToolCall("3", "write_file", {"path": "../outside.txt", "content": "bad"})
        )
        assert rejected.is_error

    asyncio.run(scenario())


def test_read_tool_rejects_symlink_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation is unavailable")
        files = WorkspaceFiles(tmp_path, 1000)
        with pytest.raises(ValueError, match="leaves the workspace"):
            files.resolve("link.txt")
    finally:
        outside.unlink(missing_ok=True)


def test_command_nonzero_exit_is_tool_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        files = WorkspaceFiles(tmp_path, 1000)
        registry = ToolRegistry([RunCommand(files, AgentSettings())])
        result = await registry.execute(ToolCall("exit", "run_command", {"command": "exit 7"}))
        assert result.is_error
        assert "status 7" in result.output

    asyncio.run(scenario())

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from xd_agent.agent.approval import LocalApproval
from xd_agent.agent.context import ContextMemory, compose_context, context_size
from xd_agent.agent.models import Message, ModelReply, ToolCall, Usage
from xd_agent.agent.outputs import ToolOutputStore
from xd_agent.agent.reporting import NullReporter
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import ReadFile, ReadToolOutput, RunCommand, ToolRegistry, WorkspaceFiles
from xd_agent.core.config import AgentSettings


def test_unicode_archive_pages_are_lossless_and_bounded(tmp_path: Path) -> None:
    outputs = ToolOutputStore(tmp_path, 150)
    original = "中文🙂\r\n" * 100 + "TAIL"
    preview = outputs.bound(original)
    output_id = re.search(r"output_id=([a-f0-9]{32})", preview)[1]
    offset = 0
    recovered = ""
    while True:
        page = outputs.read(output_id, offset)
        assert len(page) <= 150
        match = re.search(r"\n\[read_tool_output:.*offset=(\d+)\]$", page)
        if match is None:
            recovered += page
            break
        recovered += page[: match.start()]
        next_offset = int(match[1])
        assert next_offset > offset
        offset = next_offset
    assert recovered == original
    with pytest.raises(ValueError, match="invalid output_id"):
        outputs.read("../outside")


def test_read_file_pagination_preserves_crlf_and_multibyte_text(tmp_path: Path) -> None:
    original = "行🙂\r\n" * 100
    (tmp_path / "a.txt").write_bytes(original.encode("utf-8"))

    async def scenario():
        tool = ReadFile(WorkspaceFiles(tmp_path, 100))
        recovered = ""
        offset = 0
        while True:
            page = await tool.execute({"path": "a.txt", "offset": offset})
            assert len(page) <= 100
            match = re.search(r"\n\[read_file:.*offset=(\d+)\]$", page)
            if match is None:
                recovered += page
                break
            recovered += page[: match.start()]
            offset = int(match[1])
        assert recovered == original
        for arguments in ({"offset": -1}, {"offset": True}, {"limit": 101}):
            with pytest.raises(ValueError):
                await tool.execute({"path": "a.txt", **arguments})

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_registry_archives_long_success_and_error(tmp_path: Path, fail: bool) -> None:
    class VerboseTool:
        name = "verbose"
        read_only = True
        description = "test"
        parameters = {}

        async def execute(self, arguments):
            if fail:
                raise RuntimeError("noise" * 1000 + "END")
            return "noise" * 1000 + "END"

    async def scenario():
        outputs = ToolOutputStore(tmp_path, 200)
        registry = ToolRegistry([VerboseTool(), ReadToolOutput(outputs)], outputs=outputs)
        result = await registry.execute(ToolCall("1", "verbose", {}))
        assert result.is_error is fail
        assert len(result.output) <= 200
        output_id = re.search(r"output_id=([a-f0-9]{32})", result.output)[1]
        assert outputs.path(output_id).read_text(encoding="utf-8").endswith("END")
        page = await registry.execute(
            ToolCall("2", "read_tool_output", {"output_id": output_id, "offset": 4950})
        )
        assert not page.is_error
        assert "END" in page.output

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_code", [0, 7])
def test_command_keeps_complete_output_on_disk(tmp_path: Path, exit_code: int) -> None:
    async def scenario():
        files = WorkspaceFiles(tmp_path, 200)
        registry = ToolRegistry(
            [RunCommand(files, AgentSettings())],
            approval=LocalApproval(),
            outputs=files.outputs,
        )
        prefix = "& " if sys.platform == "win32" else ""
        command = (
            f'{prefix}"{sys.executable}" -c '
            f"\"import sys; sys.stdout.write('x' * 5000 + 'TAIL'); sys.exit({exit_code})\""
        )
        if sys.platform == "win32":
            command += "; exit $LASTEXITCODE"
        result = await registry.execute(ToolCall("cmd", "run_command", {"command": command}))
        assert result.is_error is bool(exit_code)
        assert len(result.output) <= 200
        if exit_code:
            assert "status 7" in result.output
        archives = list((tmp_path / ".xd-agent" / "tool-outputs").glob("*.txt"))
        assert any(path.read_text(encoding="utf-8") == "x" * 5000 + "TAIL" for path in archives)

    asyncio.run(scenario())


def test_context_keeps_multi_tool_exchange_together() -> None:
    history = [
        Message(role="user", content="task"),
        Message(
            role="assistant",
            tool_calls=(
                ToolCall("1", "read_file", {}),
                ToolCall("2", "read_file", {}),
            ),
        ),
        Message(role="tool", content="one", tool_call_id="1"),
        Message(role="tool", content="two", tool_call_id="2"),
    ]
    system = Message(role="system", content="system")
    small = compose_context(system, history, "task", AgentSettings(memory_messages=2))
    assert not any(message.role == "tool" or message.tool_calls for message in small)
    large = compose_context(system, history, "task", AgentSettings(memory_messages=3))
    assert [message.role for message in large][-3:] == ["assistant", "tool", "tool"]


def test_memory_batches_history_and_bounds_summary_requests() -> None:
    class SummaryProvider:
        calls = 0

        async def generate(self, messages, tools):
            self.calls += 1
            assert tools == []
            assert context_size(messages) <= 1000
            return ModelReply(
                Message(role="assistant", content="约束：保留接口。未完成：验证。"), Usage(5, 2)
            )

    async def scenario():
        provider = SummaryProvider()
        usages = []
        memory = ContextMemory(
            provider,
            AgentSettings(max_context_chars=1000, memory_messages=2),
            on_usage=usages.append,
        )
        history = [
            Message(role="user", content="保留接口"),
            Message(role="assistant", content="日志" * 2000),
            Message(role="user", content="继续验证"),
        ]
        context = await memory.prepare(
            Message(role="system", content="system"), history, "继续验证"
        )
        assert "保留接口" in context[1].content
        assert "未完成：验证" in context[1].content
        assert memory.summarized_messages == 2
        assert len(usages) == provider.calls
        assert context_size(context) <= 1000
        calls = provider.calls
        await memory.prepare(Message(role="system", content="system"), history, "继续验证")
        assert provider.calls == calls

    asyncio.run(scenario())


def test_invalid_summary_does_not_advance_checkpoint() -> None:
    class InvalidProvider:
        async def generate(self, messages, tools):
            return ModelReply(Message(role="assistant", content=""))

    async def scenario():
        memory = ContextMemory(
            InvalidProvider(), AgentSettings(memory_messages=2), on_usage=lambda _: None
        )
        history = [Message(role="user", content=str(index)) for index in range(5)]
        with pytest.raises(ValueError, match="original history retained"):
            await memory.prepare(Message(role="system", content="system"), history, "task")
        assert memory.summarized_messages == 0
        assert memory.summary == ""
        assert len(history) == 5

    asyncio.run(scenario())


def test_memory_resummarizes_checkpoint_when_budget_is_reduced() -> None:
    class Provider:
        async def generate(self, messages, tools):
            assert context_size(messages) <= 1000
            return ModelReply(Message(role="assistant", content="保留接口；仍需验证。"))

    async def scenario():
        memory = ContextMemory(
            Provider(),
            AgentSettings(max_context_chars=1000),
            summary="保留接口；仍需验证。" * 100,
            summarized_messages=1,
            on_usage=lambda _: None,
        )
        history = [Message(role="user", content="old"), Message(role="user", content="继续")]
        context = await memory.prepare(Message(role="system", content="system"), history, "继续")
        assert len(memory.summary) <= 125
        assert memory.summarized_messages == 1
        assert "仍需验证" in context[1].content
        assert context_size(context) <= 1000

    asyncio.run(scenario())


def test_engine_persists_summary_checkpoint_and_reuses_after_restart(tmp_path: Path) -> None:
    class Provider:
        summaries = 0

        async def generate(self, messages, tools):
            if not tools:
                self.summaries += 1
                if self.summaries == 2:
                    assert "保留接口 0" not in messages[-1].content
                    assert "测试尚未运行" in messages[-1].content
                return ModelReply(
                    Message(role="assistant", content="约束：保留接口；测试尚未运行。"), Usage(3, 2)
                )
            assert "保留接口" in messages[1].content
            return ModelReply(Message(role="assistant", content="继续处理。"), Usage(10, 5))

    async def scenario():
        db = tmp_path / "sessions.sqlite3"
        workdir = tmp_path / "work"
        provider = Provider()
        settings = AgentSettings(memory_messages=3)
        store = SessionStore(db)
        store.get_or_create("test", workdir)
        store.append("test", *[Message(role="user", content=f"保留接口 {i}") for i in range(5)])
        service = AgentService(provider, store, settings)
        await service.run(
            "继续",
            session_id="test",
            workdir=workdir,
            reporter=NullReporter(),
            approval=LocalApproval(),
        )
        summary, checkpoint = store.load_memory("test")
        assert summary
        assert checkpoint == 3
        assert len(store.get_or_create("test", workdir).messages) == 7
        store.close()
        reopened = SessionStore(db)
        service = AgentService(provider, reopened, settings)
        await service.run(
            "验证",
            session_id="test",
            workdir=workdir,
            reporter=NullReporter(),
            approval=LocalApproval(),
        )
        assert reopened.load_memory("test")[1] > checkpoint
        # 首次摘要的旧消息不会再次作为原始历史发送；只滚动更新摘要。
        assert provider.summaries == 2
        assert reopened.get_or_create("test", workdir).input_tokens == 26
        assert len(reopened.get_or_create("test", workdir).messages) == 9
        reopened.close()

    asyncio.run(scenario())


def test_explorer_only_returns_structured_summary_to_main_agent(tmp_path: Path) -> None:
    raw = "RAW_NOISE_ONLY_IN_EXPLORER" * 15

    class Provider:
        calls = 0

        async def generate(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(
                    Message(
                        role="assistant",
                        tool_calls=(
                            ToolCall("explore", "explore_workspace", {"task": "检查 a.txt"}),
                        ),
                    )
                )
            if self.calls == 2:
                assert all(tool["name"] not in {"write_file", "run_command"} for tool in tools)
                return ModelReply(
                    Message(
                        role="assistant",
                        tool_calls=(ToolCall("read", "read_file", {"path": "a.txt"}),),
                    )
                )
            if self.calls == 3:
                assert raw in messages[-1].content
                return ModelReply(
                    Message(
                        role="assistant",
                        content=json.dumps(
                            {
                                "conclusion": "发现配置项。" * 200,
                                "evidence": ["a.txt:1 配置项存在。" * 100],
                                "unknowns": ["尚未验证实际行为。" * 100],
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
            report = messages[-1].content
            assert "结论：" in report and "关键证据：" in report and "未确认事项：" in report
            assert len(report) <= 300
            assert not any(raw in message.content for message in messages)
            assert "read_tool_output" in report
            return ModelReply(Message(role="assistant", content="调查完成。"))

    async def scenario():
        (tmp_path / "a.txt").write_text(raw, encoding="utf-8")
        store = SessionStore(tmp_path / "sessions.sqlite3")
        provider = Provider()
        result = await AgentService(
            provider,
            store,
            AgentSettings(max_exploration_summary_chars=300),
        ).run(
            "调查",
            session_id="explore",
            workdir=tmp_path,
            reporter=NullReporter(),
            approval=LocalApproval(),
        )
        assert result == "调查完成。"
        assert provider.calls == 4
        assert len(store.get_or_create("explore", tmp_path).messages) == 4
        store.close()

    asyncio.run(scenario())

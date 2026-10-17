import asyncio
from pathlib import Path

from xd_agent.agent.models import Message, ModelReply, ToolCall, Usage
from xd_agent.agent.reporting import AgentEvent
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.core.config import AgentSettings


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            assert any(tool["name"] == "write_file" for tool in tools)
            return ModelReply(
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall("call-1", "write_file", {"path": "note.txt", "content": "hello"}),
                    ),
                ),
                Usage(10, 5),
            )
        assert messages[-1].role == "tool"
        assert messages[-1].tool_call_id == "call-1"
        return ModelReply(Message(role="assistant", content="已完成。"), Usage(12, 3))


class CaptureReporter:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


def test_agent_runs_tool_and_persists_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SessionStore(tmp_path / "sessions.sqlite3")
        provider = FakeProvider()
        settings = AgentSettings(max_turns=3)
        reporter = CaptureReporter()
        service = AgentService(provider, store, settings)
        workdir = tmp_path / "work"
        result = await service.run(
            "写一份便签", session_id="cli:test", workdir=workdir, reporter=reporter
        )
        assert result == "已完成。"
        assert (workdir / "note.txt").read_text(encoding="utf-8") == "hello"
        assert [event.kind for event in reporter.events] == [
            "tool_start",
            "tool_result",
            "message",
        ]
        snapshot = store.get_or_create("cli:test", workdir)
        assert [message.role for message in snapshot.messages] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert snapshot.input_tokens == 22
        store.close()
        reopened = SessionStore(tmp_path / "sessions.sqlite3")
        assert len(reopened.get_or_create("cli:test", workdir).messages) == 4
        reopened.close()

    asyncio.run(scenario())


def test_interrupted_tool_call_gets_recovery_result(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.get_or_create("cli:test", tmp_path)
    store.append(
        "cli:test",
        Message(role="assistant", tool_calls=(ToolCall("missing", "read_file", {}),)),
    )
    snapshot = store.get_or_create("cli:test", tmp_path)
    assert snapshot.messages[-1].role == "tool"
    assert snapshot.messages[-1].is_error
    assert len(store.get_or_create("cli:test", tmp_path).messages) == 2
    store.close()

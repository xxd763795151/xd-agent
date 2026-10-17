import asyncio
from pathlib import Path

from xd_agent.agent.benchmark import BenchmarkCase, run_benchmark
from xd_agent.agent.models import Message, ModelReply, ToolCall
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.core.config import AgentSettings


class BenchmarkProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            "edit-1",
                            "edit_file",
                            {"path": "version.txt", "old_text": "v1", "new_text": "v2"},
                        ),
                    ),
                )
            )
        return ModelReply(Message(role="assistant", content="done"))


def test_benchmark_checks_artifact_contents(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SessionStore(tmp_path / "sessions.sqlite3")
        service = AgentService(BenchmarkProvider(), store, AgentSettings(max_turns=3))
        cases = [
            BenchmarkCase(
                "edit",
                "change version",
                {"version.txt": "v1"},
                {"version.txt": "v2"},
            )
        ]
        results = await run_benchmark(service, cases)
        assert len(results) == 1
        assert results[0].passed
        store.close()

    asyncio.run(scenario())

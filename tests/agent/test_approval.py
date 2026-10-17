import asyncio

from xd_agent.agent.approval import ChatApproval
from xd_agent.agent.models import ToolCall
from xd_agent.agent.reporting import AgentEvent


class Reporter:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


def test_approval_requires_correct_chat_and_user() -> None:
    async def scenario() -> None:
        approval = ChatApproval({"trusted"}, 10)
        reporter = Reporter()
        call = ToolCall("tool-1", "write_file", {"path": "a.txt", "content": "x"})
        task = asyncio.create_task(
            approval.authorize(call, chat_id="chat-a", reporter=reporter)
        )
        await asyncio.sleep(0)
        approval_id = next(iter(approval.pending))
        assert not approval.resolve(
            approval_id, chat_id="chat-b", user_id="trusted", allowed=True
        )
        assert not approval.resolve(
            approval_id, chat_id="chat-a", user_id="stranger", allowed=True
        )
        assert approval.resolve(
            approval_id, chat_id="chat-a", user_id="trusted", allowed=True
        )
        assert await task == (True, "")
        assert not approval.pending
        assert "a.txt" in reporter.events[0].text

    asyncio.run(scenario())

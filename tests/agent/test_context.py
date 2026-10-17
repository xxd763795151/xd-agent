from xd_agent.agent.context import compose_context
from xd_agent.agent.models import Message, ToolCall
from xd_agent.core.config import AgentSettings


def test_context_drops_orphan_tool_result_after_windowing() -> None:
    history = [
        Message(role="user", content="task"),
        Message(role="assistant", tool_calls=(ToolCall("1", "read_file", {}),)),
        Message(role="tool", content="old result", tool_call_id="1"),
        Message(role="assistant", content="next"),
        Message(role="user", content="continue"),
    ]
    context = compose_context(
        Message(role="system", content="system"),
        history,
        "continue",
        AgentSettings(memory_messages=3),
    )
    assert context[0].role == "system"
    assert context[1].role == "user"
    assert all(message.role != "tool" for message in context)

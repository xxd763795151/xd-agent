import asyncio
import json

import httpx
from pydantic import SecretStr

from xd_agent.agent.models import Message, ToolCall
from xd_agent.agent.provider import HttpModelProvider
from xd_agent.core.config import AgentSettings


def test_openai_provider_round_trips_tool_messages() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            data = json.loads(request.content)
            assert data["messages"][-1] == {
                "role": "tool",
                "tool_call_id": "t-1",
                "content": "done",
            }
            assert data["messages"][-2]["tool_calls"][0]["function"]["name"] == "read_file"
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "t-2",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"a.txt"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 3},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HttpModelProvider(AgentSettings(api_key=SecretStr("test")), client)
            reply = await provider.generate(
                [
                    Message(role="user", content="read"),
                    Message(role="assistant", tool_calls=(ToolCall("t-1", "read_file", {}),)),
                    Message(role="tool", content="done", tool_call_id="t-1"),
                ],
                [],
            )
            assert reply.message.tool_calls[0].arguments == {"path": "a.txt"}
            assert reply.usage.input_tokens == 8

    asyncio.run(scenario())


def test_anthropic_provider_keeps_tool_error_and_usage() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            data = json.loads(request.content)
            assert request.url.path.endswith("/v1/messages")
            assert data["system"] == "rules"
            assert data["messages"][-1]["content"][0]["is_error"] is True
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "text", "text": "check"},
                        {
                            "type": "tool_use",
                            "id": "next",
                            "name": "read_file",
                            "input": {"path": "a.txt"},
                        },
                    ],
                    "usage": {"input_tokens": 9, "output_tokens": 4},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HttpModelProvider(
                AgentSettings(
                    provider="anthropic_compatible",
                    api_key=SecretStr("test"),
                    base_url="https://example.test/api",
                ),
                client,
            )
            reply = await provider.generate(
                [
                    Message(role="system", content="rules"),
                    Message(role="user", content="try"),
                    Message(role="assistant", tool_calls=(ToolCall("old", "read_file", {}),)),
                    Message(role="tool", content="failed", tool_call_id="old", is_error=True),
                ],
                [],
            )
            assert reply.message.content == "check"
            assert reply.message.tool_calls[0].arguments == {"path": "a.txt"}
            assert reply.usage.output_tokens == 4

    asyncio.run(scenario())

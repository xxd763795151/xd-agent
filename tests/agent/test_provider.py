import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from xd_agent.agent.models import Message, ToolCall
from xd_agent.agent.provider import HttpModelProvider, ProviderError
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


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        (400, "请求参数无效"),
        (401, "身份认证失败"),
        (403, "访问被拒绝"),
        (404, "接口或模型不存在"),
        (429, "请求受到限制"),
        (503, "模型服务暂时不可用"),
        (418, "请检查模型服务配置"),
    ],
)
def test_provider_http_errors_keep_status_and_code_without_response_body(status, hint) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status, json={"error": {"code": "1302", "message": "private response body"}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HttpModelProvider(AgentSettings(api_key=SecretStr("test-key")), client)
            with pytest.raises(ProviderError) as caught:
                await provider.generate([Message(role="user", content="private prompt")], [])
        assert caught.value.status_code == status
        assert caught.value.error_code == "1302"
        assert f"HTTP {status}, code=1302" in str(caught.value)
        assert hint in str(caught.value)
        assert "private" not in str(caught.value)
        assert "test-key" not in str(caught.value)
        assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"gateway failure",
        b"[]",
        b'{"error":"limited"}',
        b'{"error":{"code":null}}',
        b'{"error":{"code":true}}',
        b'{"error":{"code":"unsafe\\ncode"}}',
    ],
)
def test_provider_http_error_handles_missing_or_invalid_error_code(body) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(429, content=body))
        ) as client:
            provider = HttpModelProvider(AgentSettings(api_key=SecretStr("test")), client)
            with pytest.raises(ProviderError) as caught:
                await provider.generate([], [])
        assert caught.value.status_code == 429
        assert caught.value.error_code is None
        assert "HTTP 429" in str(caught.value)
        assert "code=" not in str(caught.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error_type", "hint"),
    [(httpx.ReadTimeout, "模型请求超时"), (httpx.ConnectError, "无法连接模型服务")],
)
def test_provider_distinguishes_timeout_and_connection_errors(error_type, hint) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise error_type("private network details", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HttpModelProvider(AgentSettings(api_key=SecretStr("test")), client)
            with pytest.raises(ProviderError) as caught:
                await provider.generate([], [])
        assert hint in str(caught.value)
        assert "private" not in str(caught.value)
        assert caught.value.status_code is None
        assert isinstance(caught.value.__cause__, error_type)

    asyncio.run(scenario())


def test_provider_reports_invalid_success_json() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="invalid"))
        ) as client:
            provider = HttpModelProvider(AgentSettings(api_key=SecretStr("test")), client)
            with pytest.raises(ProviderError, match="无效 JSON"):
                await provider.generate([], [])

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

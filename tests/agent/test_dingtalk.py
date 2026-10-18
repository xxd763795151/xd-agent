import asyncio
from pathlib import Path
from types import SimpleNamespace

import dingtalk_stream
import httpx
import pytest
from pydantic import SecretStr

from xd_agent.agent.approval import PendingApproval
from xd_agent.agent.reporting import AgentEvent
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.dingtalk import DingTalkAdapter, DingTalkReporter, DingTalkSendError
from xd_agent.core.config import AgentSettings, DingTalkSettings


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, **kwargs: object) -> None:
        self.calls.append((prompt, str(kwargs["session_id"])))


def message(sender: str, message_id: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_type="text",
        text=SimpleNamespace(content=content),
        conversation_id="chat-1",
        sender_staff_id=sender,
        message_id=message_id,
        session_webhook="https://oapi.dingtalk.com/robot/sendBySession?session=abc",
    )


def test_dingtalk_sender_filter_dedupe_and_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SessionStore(tmp_path / "session.sqlite3")
        agent = FakeAgent()
        adapter = DingTalkAdapter(
            agent,
            store,
            DingTalkSettings(allowed_approver_ids=["admin"], allowed_sender_ids=["requester"]),
            AgentSettings(workspace_dir=tmp_path),
        )
        await adapter._on_message(message("outsider", "1", "ignored"))
        await adapter._on_message(message("requester", "2", "task"))
        await adapter._on_message(message("requester", "2", "task"))
        await asyncio.gather(*adapter.tasks)
        assert agent.calls == [("task", "dingtalk:chat-1")]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        adapter.approval.pending["approval-1"] = PendingApproval("chat-1", future, ready=True)
        await adapter._on_message(message("admin", "3", "approve approval-1"))
        assert future.result() is True
        await adapter.stop()
        store.close()

    asyncio.run(scenario())


def test_dingtalk_reporter_sends_to_session_webhook() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"errcode": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reporter = DingTalkReporter(client, "https://oapi.dingtalk.com/robot/sendBySession")
            await reporter.emit(AgentEvent("message", "完成"))
        assert requests[0].url.host == "oapi.dingtalk.com"
        assert b"\xe5\xae\x8c\xe6\x88\x90" in requests[0].content

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["", " ", "\r\n\t", "\u3000"])
def test_dingtalk_reporter_skips_blank_text(text: str) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, json={"errcode": 400202, "errmsg": "miss param : text->content"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reporter = DingTalkReporter(client, "https://oapi.dingtalk.com/robot/sendBySession")
            await reporter.emit(AgentEvent("message", text))
            await reporter.emit(AgentEvent("notice", text))
        assert not requests

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            200,
            b'{"errcode":40014,"errmsg":"invalid access token","requestId":"req-1"}',
            ["HTTP 200", "errcode=40014", "errmsg=invalid access token", "requestId=req-1"],
        ),
        (
            403,
            b'{"code":"Forbidden","message":"permission denied"}',
            ["HTTP 403", "code=Forbidden", "message=permission denied"],
        ),
        (502, b"private gateway error", ["HTTP 502", "non-JSON response"]),
        (200, b"[]", ["HTTP 200", "invalid response"]),
        (200, b"{}", ["HTTP 200", "missing errcode"]),
        (403, b'{"errcode":0}', ["HTTP 403", "errcode=0"]),
    ],
)
def test_dingtalk_send_errors_keep_api_diagnostics(status, body, expected) -> None:
    async def scenario() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reporter = DingTalkReporter(
                client, "https://oapi.dingtalk.com/robot/sendBySession?session=private-session"
            )
            with pytest.raises(DingTalkSendError) as caught:
                await reporter.emit(AgentEvent("message", "private-message"))
        for detail in expected:
            assert detail in str(caught.value)
        assert "private" not in str(caught.value)

    asyncio.run(scenario())


def test_dingtalk_delivery_failure_logs_reason_without_retrying_webhook(tmp_path, caplog) -> None:
    async def scenario() -> None:
        calls = 0
        webhook = "https://oapi.dingtalk.com/robot/sendBySession?session=private-session"

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "errcode": 123,
                    "errmsg": f"send rejected\n{webhook} session=private-session",
                    "requestId": "req-1",
                    "extra": "private response body",
                },
            )

        class ReplyingAgent:
            async def run(self, prompt, *, reporter, **kwargs):
                await reporter.emit(AgentEvent("message", "private-message"))

        store = SessionStore(tmp_path / "sessions.sqlite3")
        adapter = DingTalkAdapter(
            ReplyingAgent(), store, DingTalkSettings(), AgentSettings(workspace_dir=tmp_path)
        )
        await adapter.http_client.aclose()
        adapter.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            await adapter._process("chat-1", "task", webhook)
            assert calls == 1
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())
    assert "DingTalk reply delivery failed chat=chat-1" in caplog.text
    assert "errcode=123, errmsg=send rejected " in caplog.text
    assert "requestId=req-1" in caplog.text
    assert "private" not in caplog.text
    assert "https://" not in caplog.text


def test_dingtalk_network_error_hides_webhook() -> None:
    async def scenario() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(f"timeout for {request.url}", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            reporter = DingTalkReporter(
                client, "https://oapi.dingtalk.com/robot/sendBySession?session=private-session"
            )
            with pytest.raises(DingTalkSendError, match="ReadTimeout") as caught:
                await reporter.emit(AgentEvent("message", "private-message"))
        assert caught.value.__suppress_context__
        assert "private" not in str(caught.value)
        assert "https://" not in str(caught.value)

    asyncio.run(scenario())


def test_dingtalk_stream_registers_sdk_callback(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        store = SessionStore(tmp_path / "session.sqlite3")
        agent = FakeAgent()
        stopped = asyncio.Event()

        class FakeClient:
            def __init__(self, credential) -> None:
                self.credential = credential
                self.handler = None
                self.websocket = None

            def register_callback_handler(self, topic, handler) -> None:
                assert topic == dingtalk_stream.ChatbotMessage.TOPIC
                self.handler = handler

            async def start(self) -> None:
                self.websocket = object()
                await stopped.wait()

            async def stop(self) -> None:
                stopped.set()

        monkeypatch.setattr(dingtalk_stream, "DingTalkStreamClient", FakeClient)
        adapter = DingTalkAdapter(
            agent,
            store,
            DingTalkSettings(
                client_id="ding-app",
                client_secret=SecretStr("secret"),
                allowed_approver_ids=["admin"],
                allowed_sender_ids=["requester"],
            ),
            AgentSettings(workspace_dir=tmp_path),
        )
        await adapter.start()
        callback = SimpleNamespace(
            data={
                "msgtype": "text",
                "text": {"content": "task"},
                "conversationId": "chat-1",
                "senderStaffId": "requester",
                "msgId": "sdk-message-1",
                "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession",
            }
        )
        assert await adapter.client.handler.process(callback) == (200, "OK")
        await asyncio.gather(*adapter.tasks)
        assert agent.calls == [("task", "dingtalk:chat-1")]
        await adapter.stop()
        store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["accepted", "missing_sender", "unsupported", "duplicate"])
def test_dingtalk_receive_diagnostics_exclude_message_and_webhook(
    tmp_path: Path, caplog, kind: str
) -> None:
    caplog.set_level("INFO", logger="xd_agent.channels.dingtalk")

    async def scenario() -> None:
        store = SessionStore(tmp_path / "session.sqlite3")
        adapter = DingTalkAdapter(
            FakeAgent(),
            store,
            DingTalkSettings(allowed_approver_ids=["admin"]),
            AgentSettings(workspace_dir=tmp_path),
        )
        incoming = message("admin", "diagnostic-id", "private-message-content")
        if kind == "missing_sender":
            incoming.sender_staff_id = ""
        elif kind == "unsupported":
            incoming.message_type = "picture"
        elif kind == "duplicate":
            store.claim_event("dingtalk", "diagnostic-id")
        try:
            await adapter._on_message(incoming)
            if adapter.tasks:
                await asyncio.gather(*adapter.tasks)
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())
    assert "DingTalk message received" in caplog.text
    expected = {
        "accepted": "DingTalk task queued",
        "missing_sender": "reason=missing_fields",
        "unsupported": "reason=unsupported_message_type",
        "duplicate": "reason=duplicate",
    }
    assert expected[kind] in caplog.text
    assert "private-message-content" not in caplog.text
    assert "session=abc" not in caplog.text

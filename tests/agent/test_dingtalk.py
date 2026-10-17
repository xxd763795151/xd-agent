import asyncio
from pathlib import Path
from types import SimpleNamespace

import dingtalk_stream
import httpx
from pydantic import SecretStr

from xd_agent.agent.approval import PendingApproval
from xd_agent.agent.reporting import AgentEvent
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.dingtalk import DingTalkAdapter, DingTalkReporter
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

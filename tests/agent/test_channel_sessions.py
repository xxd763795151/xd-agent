import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pytest

from xd_agent.agent.approval import PendingApproval
from xd_agent.agent.models import Message, ModelReply, ToolCall
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.dingtalk import DingTalkAdapter
from xd_agent.channels.feishu import FeishuAdapter
from xd_agent.channels.wecom import WeComAdapter
from xd_agent.core.config import AgentSettings, DingTalkSettings, FeishuSettings, WeComSettings


@pytest.fixture(params=["dingtalk", "feishu", "wecom"])
def channel(request):
    return request.param


@pytest.fixture
def message(channel):
    def build(mid: str, text: str, *, sender: str = "requester", chat: str = "requester"):
        if channel == "wecom":
            root = ElementTree.Element("xml")
            for key, value in {
                "MsgType": "text",
                "ToUserName": "corp",
                "AgentID": "1",
                "FromUserName": chat if sender == "requester" else sender,
                "MsgId": mid,
                "Content": text,
            }.items():
                ElementTree.SubElement(root, key).text = value
            return root
        if channel == "feishu":
            return SimpleNamespace(
                chat_id=chat, sender_id=sender, message_id=mid, content_text=text
            )
        return SimpleNamespace(
            message_type="text",
            text=SimpleNamespace(content=text),
            conversation_id=chat,
            sender_staff_id=sender,
            message_id=mid,
            session_webhook="https://oapi.dingtalk.com/robot/sendBySession?session=secret",
        )

    return build


class Provider:
    def __init__(self):
        self.contexts = []

    async def generate(self, messages, tools):
        self.contexts.append(messages)
        return ModelReply(Message("assistant", "已收到"))


async def make_adapter(channel, store, settings, agent, sent, *, fail=False):
    common = {"allowed_sender_ids": ["requester", "other"], "allowed_approver_ids": ["admin"]}
    if channel == "feishu":
        adapter = FeishuAdapter(agent, store, FeishuSettings(**common), settings)

        async def send(chat_id, payload):
            sent.append(payload["text"])
            if fail:
                raise RuntimeError("test send failure")
            return SimpleNamespace(success=True)

        async def disconnect():
            pass

        adapter.channel = SimpleNamespace(send=send, disconnect=disconnect)
    elif channel == "wecom":
        adapter = WeComAdapter(
            agent, store, WeComSettings(corp_id="corp", agent_id=1, **common), settings
        )

        async def send_text(user_id, text):
            sent.append(text)
            if fail:
                raise RuntimeError("test send failure")

        adapter.send_text = send_text
    else:
        adapter = DingTalkAdapter(agent, store, DingTalkSettings(**common), settings)
        await adapter.http_client.aclose()

        def respond(request):
            sent.append(json.loads(request.content)["text"]["content"])
            return httpx.Response(
                200, json={"errcode": 123 if fail else 0, "errmsg": "test send failure"}
            )

        adapter.http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return adapter


async def drain(adapter):
    await asyncio.gather(*adapter.tasks)


def test_new_session_isolates_context_and_survives_restart(tmp_path: Path, channel, message):
    async def scenario():
        db = tmp_path / "sessions.sqlite3"
        store = SessionStore(db)
        settings = AgentSettings(workspace_dir=tmp_path / "work")
        provider = Provider()
        sent = []
        adapter = await make_adapter(
            channel, store, settings, AgentService(provider, store, settings), sent
        )
        try:
            # Existing installations keep using their original session until /new.
            await adapter._on_message(message("old", "remember-old-context"))
            await drain(adapter)
            old_id = f"{channel}:requester"
            old = store.get_or_create(old_id, settings.workspace_dir)
            store.save_memory(old_id, "old-summary", 2)
            assert len(old.messages) == 2
            (settings.workspace_dir / "keep.txt").write_text("keep", encoding="utf-8")

            await adapter._on_message(message("new", " /new \n"))
            # A following message can arrive before the confirmation finishes sending.
            current_id = store.get_channel_session(channel, "requester")
            assert current_id != old_id
            await adapter._on_message(message("new", "/new"))
            assert store.get_channel_session(channel, "requester") == current_id
            await drain(adapter)
            assert len(provider.contexts) == 1  # /new never calls the model.
            assert len(sent) == 2  # One model reply and one command confirmation.
            assert "已开启新会话" in sent[-1]
            assert store.load_memory(current_id) == ("", 0)
            assert store.get_or_create(old_id, settings.workspace_dir).messages == old.messages
            assert store.load_memory(old_id) == ("old-summary", 2)
            assert store.get_channel_session(channel, "other") == f"{channel}:other"
            other_channel = "wecom" if channel == "feishu" else "feishu"
            assert (
                store.get_channel_session(other_channel, "requester")
                == f"{other_channel}:requester"
            )
        finally:
            await adapter.stop()
            store.close()

        # Restart before the first prompt in the new session.
        store = SessionStore(db)
        adapter = await make_adapter(
            channel, store, settings, AgentService(provider, store, settings), sent
        )
        try:
            assert store.get_channel_session(channel, "requester") == current_id
            await adapter._on_message(message("new", "/new"))
            assert not adapter.tasks
            assert store.get_channel_session(channel, "requester") == current_id
            await adapter._on_message(message("first", "new-question"))
            await drain(adapter)
            await adapter._on_message(message("followup", "followup-question"))
            await drain(adapter)
            first_context = provider.contexts[1]
            assert [m.content for m in first_context if m.role == "user"] == ["new-question"]
            assert "old-summary" not in "\n".join(m.content for m in first_context)
            assert [m.content for m in provider.contexts[2] if m.role == "user"] == [
                "new-question",
                "followup-question",
            ]
            assert len(store.get_or_create(current_id, settings.workspace_dir).messages) == 4
            assert (settings.workspace_dir / "keep.txt").read_text(encoding="utf-8") == "keep"
            await adapter._on_message(message("another-new", "/new"))
            await drain(adapter)
            assert store.get_channel_session(channel, "requester") not in {old_id, current_id}
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())


def test_approval_stays_in_original_chat_after_new_session(tmp_path: Path, channel, message):
    async def scenario():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        settings = AgentSettings(workspace_dir=tmp_path / "work")
        sent = []

        class WritingProvider(Provider):
            async def generate(self, messages, tools):
                if messages[-1].role == "user":
                    return ModelReply(
                        Message(
                            "assistant",
                            tool_calls=(
                                ToolCall("write", "write_file", {"path": "note", "content": "ok"}),
                            ),
                        )
                    )
                return ModelReply(Message("assistant", "done"))

        adapter = await make_adapter(
            channel, store, settings, AgentService(WritingProvider(), store, settings), sent
        )
        try:
            await adapter._on_message(message("new", "/new"))
            await drain(adapter)
            session_id = store.get_channel_session(channel, "requester")
            await adapter._on_message(message("task", "write a note"))
            for _ in range(50):
                if any(pending.ready for pending in adapter.approval.pending.values()):
                    break
                await asyncio.sleep(0)
            approval_id = next(iter(adapter.approval.pending))
            assert not (settings.workspace_dir / "note").exists()
            await adapter._on_message(message("blocked-new", "/new"))
            assert store.get_channel_session(channel, "requester") == session_id
            await adapter._on_message(message("decision", f"approve {approval_id}", sender="admin"))
            await drain(adapter)
            assert (settings.workspace_dir / "note").read_text(encoding="utf-8") == "ok"
            record = store.get_approval(approval_id)
            assert record["session_id"] == session_id
            assert record["chat_id"] == "requester"
            assert record["status"] == "approved"
            assert record["execution_status"] == "succeeded"
            assert any("任务在执行或等待审批" in text for text in sent)
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/new", "/new task"])
def test_new_session_requires_sender_permission(tmp_path: Path, text: str, channel, message):
    async def scenario():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        settings = AgentSettings(workspace_dir=tmp_path)
        provider = Provider()
        sent = []
        adapter = await make_adapter(
            channel, store, settings, AgentService(provider, store, settings), sent
        )
        try:
            for sender in ("outsider", "admin"):
                await adapter._on_message(message(sender, text, sender=sender))
            await drain(adapter)
            assert not sent
            assert not provider.contexts
            assert store.get_channel_session(channel, "requester") == f"{channel}:requester"
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("busy", ["queued", "running", "approval"])
def test_new_session_refuses_busy_chat_but_allows_other_chats(
    tmp_path: Path, busy: str, channel, message
):
    async def scenario():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        settings = AgentSettings(workspace_dir=tmp_path)
        sent = []
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingAgent:
            async def run(self, prompt, **kwargs):
                started.set()
                await release.wait()

        adapter = await make_adapter(channel, store, settings, BlockingAgent(), sent)
        future = asyncio.get_running_loop().create_future()
        try:
            if busy == "approval":
                adapter.approval.pending["pending"] = PendingApproval(
                    "requester", future, ready=True
                )
            else:
                await adapter._on_message(message("task", "do something"))
                if busy == "running":
                    await started.wait()
            # In the queued case /new arrives before the agent task starts.
            await adapter._on_message(message("busy-new", "/new"))
            assert store.get_channel_session(channel, "requester") == f"{channel}:requester"
            await adapter._on_message(message("other-new", "/new", chat="other"))
            assert store.get_channel_session(channel, "other") != f"{channel}:other"
            if busy == "approval":
                assert not future.done()
                await adapter._on_message(message("approve", "approve pending", sender="admin"))
                assert future.result() is True
                adapter.approval.pending.clear()
            release.set()
            await drain(adapter)
            assert any("任务在执行或等待审批" in text for text in sent)
            assert not adapter.conversations._task_chats
            await adapter._on_message(message("idle-new", "/new"))
            await drain(adapter)
            assert store.get_channel_session(channel, "requester") != f"{channel}:requester"
        finally:
            if not future.done():
                future.cancel()
            await adapter.stop()
            store.close()

    asyncio.run(scenario())


def test_new_session_command_arguments_and_delivery_failure(
    tmp_path: Path, caplog, channel, message
):
    async def scenario():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        settings = AgentSettings(workspace_dir=tmp_path)
        provider = Provider()
        sent = []
        adapter = await make_adapter(
            channel, store, settings, AgentService(provider, store, settings), sent, fail=True
        )
        try:
            await adapter._on_message(message("args", "/new task"))
            await drain(adapter)
            assert store.get_channel_session(channel, "requester") == f"{channel}:requester"
            assert "请单独发送 /new" in sent[-1]
            await adapter._on_message(message("new", "/NEW"))
            await drain(adapter)
            # The command takes effect even if its confirmation cannot be delivered.
            assert store.get_channel_session(channel, "requester") != f"{channel}:requester"
            assert not provider.contexts
            assert not adapter.tasks
            assert not adapter.conversations._task_chats
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())
    assert "test send failure" in caplog.text
    assert "session=secret" not in caplog.text

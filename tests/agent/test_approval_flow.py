import asyncio
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import httpx
import pytest

from xd_agent.agent.models import Message, ModelReply, ToolCall
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.dingtalk import DingTalkAdapter
from xd_agent.channels.feishu import FeishuAdapter
from xd_agent.channels.wecom import WeComAdapter
from xd_agent.core.config import AgentSettings, DingTalkSettings, FeishuSettings, WeComSettings


class Provider:
    def __init__(self):
        self.calls = 0

    async def generate(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(
                Message(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            "write", "write_file", {"path": "note.txt", "content": "approved"}
                        ),
                    ),
                )
            )
        return ModelReply(Message(role="assistant", content="done"))


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom"])
@pytest.mark.parametrize("allowed", [True, False])
def test_channel_approval_controls_real_file_write(tmp_path: Path, channel: str, allowed: bool):
    async def scenario():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        settings = AgentSettings(workspace_dir=tmp_path / "work")
        agent = AgentService(Provider(), store, settings)
        common = {"allowed_approver_ids": ["admin"], "allowed_sender_ids": ["requester"]}
        if channel == "feishu":
            adapter = FeishuAdapter(agent, store, FeishuSettings(**common), settings)

            async def send(chat_id, message):
                return SimpleNamespace(success=True)

            async def disconnect():
                pass

            adapter.channel = SimpleNamespace(send=send, disconnect=disconnect)

            def message(user, mid, text):
                return SimpleNamespace(
                    chat_id="chat", sender_id=user, message_id=mid, content_text=text
                )
        elif channel == "dingtalk":
            adapter = DingTalkAdapter(agent, store, DingTalkSettings(**common), settings)
            await adapter.http_client.aclose()
            adapter.http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"errcode": 0})
                )
            )

            def message(user, mid, text):
                return SimpleNamespace(
                    message_type="text",
                    text=SimpleNamespace(content=text),
                    conversation_id="chat",
                    sender_staff_id=user,
                    message_id=mid,
                    session_webhook="https://oapi.dingtalk.com/robot/sendBySession",
                )
        else:
            adapter = WeComAdapter(
                agent, store, WeComSettings(corp_id="corp", agent_id=1, **common), settings
            )

            async def send(user, text):
                pass

            adapter.send_text = send

            def message(user, mid, text):
                root = ElementTree.Element("xml")
                for key, value in {
                    "MsgType": "text",
                    "ToUserName": "corp",
                    "AgentID": "1",
                    "FromUserName": user,
                    "MsgId": mid,
                    "Content": text,
                }.items():
                    ElementTree.SubElement(root, key).text = value
                return root

        try:
            await adapter._on_message(message("requester", "task", "write a note"))
            for _ in range(20):
                if any(pending.ready for pending in adapter.approval.pending.values()):
                    break
                await asyncio.sleep(0)
            approval_id = next(iter(adapter.approval.pending))
            assert not (settings.workspace_dir / "note.txt").exists()
            await adapter._on_message(message("outsider", "bad", f"approve {approval_id}"))
            assert store.get_approval(approval_id)["status"] == "pending"
            action = "approve" if allowed else "reject"
            await adapter._on_message(message("admin", "decision", f"{action} {approval_id}"))
            await asyncio.gather(*adapter.tasks)
            path = settings.workspace_dir / "note.txt"
            assert path.exists() is allowed
            if allowed:
                assert path.read_text(encoding="utf-8") == "approved"
            record = store.get_approval(approval_id)
            assert record["status"] == ("approved" if allowed else "rejected")
            assert record["execution_status"] == ("succeeded" if allowed else "not_started")
            assert record["decided_by"] == "admin"
            assert not adapter.approval.pending
        finally:
            await adapter.stop()
            store.close()

    asyncio.run(scenario())

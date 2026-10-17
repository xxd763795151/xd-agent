import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from xd_agent.agent.approval import PendingApproval
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.feishu import FeishuAdapter
from xd_agent.core.config import AgentSettings, FeishuSettings


class FakeAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return "done"


def test_feishu_ingress_filters_sender_and_duplicate(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SessionStore(tmp_path / "session.sqlite3")
        agent = FakeAgent()
        settings = FeishuSettings(
            app_id="cli_test",
            app_secret=SecretStr("secret"),
            verification_token=SecretStr("token"),
            encrypt_key=SecretStr("encrypt"),
            allowed_approver_ids=["admin"],
        )
        adapter = FeishuAdapter(agent, store, settings, AgentSettings(workspace_dir=tmp_path))
        adapter.channel = object()
        denied = SimpleNamespace(
            chat_id="chat", sender_id="unknown", message_id="one", content_text="test"
        )
        allowed = SimpleNamespace(
            chat_id="chat", sender_id="admin", message_id="two", content_text="task"
        )
        await adapter._on_message(denied)
        await adapter._on_message(allowed)
        await adapter._on_message(allowed)
        await asyncio.gather(*adapter.tasks)
        assert agent.prompts == ["task"]
        store.close()

    asyncio.run(scenario())


def test_feishu_approver_may_be_distinct_from_task_sender(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SessionStore(tmp_path / "session.sqlite3")
        settings = FeishuSettings(
            allowed_approver_ids=["admin"], allowed_sender_ids=["requester"]
        )
        adapter = FeishuAdapter(FakeAgent(), store, settings, AgentSettings())
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        adapter.approval.pending["approval-1"] = PendingApproval("chat", future)
        message = SimpleNamespace(
            chat_id="chat",
            sender_id="admin",
            message_id="approval-message",
            content_text="approve approval-1",
        )
        await adapter._on_message(message)
        assert future.result() is True
        store.close()

    asyncio.run(scenario())

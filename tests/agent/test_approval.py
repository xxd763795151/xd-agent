import asyncio
import json
from pathlib import Path

import pytest

from xd_agent.agent.approval import (
    MAX_APPROVAL_BYTES,
    ApprovalRequest,
    ChatApproval,
    LocalApproval,
)
from xd_agent.agent.models import ToolCall
from xd_agent.agent.reporting import AgentEvent
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import ReadFile, ToolRegistry, WorkspaceFiles


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
            approval.authorize(ApprovalRequest("approval-1", call, "chat-a"), reporter=reporter)
        )
        await asyncio.sleep(0)
        approval_id = next(iter(approval.pending))
        assert not approval.resolve(approval_id, chat_id="chat-b", user_id="trusted", allowed=True)
        assert not approval.resolve(approval_id, chat_id="chat-a", user_id="stranger", allowed=True)
        assert approval.resolve(approval_id, chat_id="chat-a", user_id="trusted", allowed=True)
        decision = await task
        assert decision.allowed
        assert decision.user_id == "trusted"
        assert not approval.pending
        assert "a.txt" in reporter.events[0].text

    asyncio.run(scenario())


class RecordingTool:
    """刻意不声明 read_only，模拟新增且尚未分类的工具。"""

    description = "test tool"
    parameters = {"type": "object", "required": []}

    def __init__(self, name="new_action", fail=False):
        self.name = name
        self.calls = []
        self.fail = fail

    async def execute(self, arguments):
        self.calls.append(arguments)
        if self.fail:
            raise RuntimeError("tool failed")
        return "done"


@pytest.mark.parametrize("name", ["write_file", "edit_file", "run_command", "new_action"])
def test_registry_denies_unclassified_tools_without_policy(tmp_path: Path, name: str) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = RecordingTool(name)
            result = await ToolRegistry([tool], store=store).execute(ToolCall("call", name, {}))
            assert result.is_error
            assert not tool.calls
            record = store.list_approvals()[0]
            assert record["status"] == "rejected"
            assert record["policy"] == "none"
            assert record["execution_status"] == "not_started"
        finally:
            store.close()

    asyncio.run(scenario())


def test_explicit_read_only_tool_needs_no_policy(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("read only", encoding="utf-8")
    registry = ToolRegistry([ReadFile(WorkspaceFiles(tmp_path, 1000))])
    result = asyncio.run(registry.execute(ToolCall("read", "read_file", {"path": "note.txt"})))
    assert not result.is_error
    assert result.output == "read only"


@pytest.mark.parametrize("allowed", [True, False])
def test_new_tool_approval_and_audit(tmp_path: Path, allowed: bool) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = RecordingTool()
            approval = ChatApproval({"admin"}, 10, store=store)
            registry = ToolRegistry(
                [tool],
                approval=approval,
                reporter=Reporter(),
                chat_id="chat",
                store=store,
                session_id="feishu:chat",
            )
            task = asyncio.create_task(registry.execute(ToolCall("call", tool.name, {"x": "y"})))
            await asyncio.sleep(0)
            approval_id = next(iter(approval.pending))
            assert store.get_approval(approval_id)["status"] == "pending"
            assert not tool.calls
            assert not approval.resolve(
                approval_id, chat_id="chat", user_id="outsider", allowed=True
            )
            assert approval.resolve(approval_id, chat_id="chat", user_id="admin", allowed=allowed)
            # 决定在唤醒执行任务前落盘。
            assert store.get_approval(approval_id)["decided_by"] == "admin"
            assert not approval.resolve(
                approval_id, chat_id="chat", user_id="admin", allowed=allowed
            )
            result = await task
            assert result.is_error is (not allowed)
            assert bool(tool.calls) is allowed
            record = store.get_approval(approval_id)
            assert record["session_id"] == "feishu:chat"
            assert record["tool_call_id"] == "call"
            assert record["arguments"] == {"x": "y"}
            assert record["status"] == ("approved" if allowed else "rejected")
            assert record["execution_status"] == ("succeeded" if allowed else "not_started")
            assert record["requested_at"] and record["decided_at"]
            events = [event["event"] for event in record["events"]]
            assert events.count("invalid_reply") == 2
            assert events.count("approved" if allowed else "rejected") == 1
            assert not approval.pending
        finally:
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "cancel", "notification_failed", "missing_chat"])
def test_wait_failures_never_execute_and_are_audited(tmp_path: Path, mode: str) -> None:
    class BrokenReporter(Reporter):
        async def emit(self, event):
            raise RuntimeError("send failed")

    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = RecordingTool()
            approval = ChatApproval({"admin"}, 0 if mode == "timeout" else 10, store=store)
            registry = ToolRegistry(
                [tool],
                approval=approval,
                reporter=BrokenReporter() if mode == "notification_failed" else Reporter(),
                chat_id=None if mode == "missing_chat" else "chat",
                store=store,
            )
            task = asyncio.create_task(registry.execute(ToolCall("call", tool.name, {})))
            if mode == "cancel":
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert (await task).is_error
            assert not tool.calls
            assert not approval.pending
            record = store.list_approvals()[0]
            assert (
                record["status"]
                == {
                    "timeout": "timed_out",
                    "cancel": "cancelled",
                    "notification_failed": "notification_failed",
                    "missing_chat": "rejected",
                }[mode]
            )
            assert record["execution_status"] == "not_started"
        finally:
            store.close()

    asyncio.run(scenario())


def test_long_arguments_are_complete_and_cannot_be_approved_mid_notification() -> None:
    async def scenario():
        approval = ChatApproval({"admin"}, 10)

        class EarlyReporter(Reporter):
            async def emit(self, event):
                await super().emit(event)
                assert not approval.resolve(
                    "approval-1", chat_id="chat", user_id="admin", allowed=True
                )

        reporter = EarlyReporter()
        arguments = {"command": "中🙂" * 1000 + " HIDDEN_TAIL"}
        request = ApprovalRequest("approval-1", ToolCall("call", "run_command", arguments), "chat")
        task = asyncio.create_task(approval.authorize(request, reporter=reporter))
        await asyncio.sleep(0)
        assert not task.done()
        assert len(reporter.events) > 2
        assert all(len(event.text.encode("utf-8")) <= 2048 for event in reporter.events)
        full = "".join(event.text.partition("\n")[2] for event in reporter.events[:-1])
        assert json.loads(full) == arguments
        assert "HIDDEN_TAIL" in reporter.events[-2].text
        assert all("回复 approve" not in event.text for event in reporter.events[:-1])
        assert approval.resolve("approval-1", chat_id="chat", user_id="admin", allowed=True)
        assert (await task).allowed

    asyncio.run(scenario())


def test_oversized_arguments_are_rejected_without_partial_notification(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            reporter = Reporter()
            tool = RecordingTool()
            registry = ToolRegistry(
                [tool],
                approval=ChatApproval({"admin"}, 10, store=store),
                reporter=reporter,
                chat_id="chat",
                store=store,
            )
            args = {"content": "x" * MAX_APPROVAL_BYTES}
            result = await registry.execute(ToolCall("call", tool.name, args))
            assert result.is_error and "64KB" in result.output
            assert not reporter.events and not tool.calls
            assert store.list_approvals()[0]["arguments"] == args
        finally:
            store.close()

    asyncio.run(scenario())


def test_approval_and_execution_use_the_same_snapshot(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = RecordingTool()
            approval = ChatApproval({"admin"}, 10, store=store)
            registry = ToolRegistry(
                [tool], approval=approval, chat_id="chat", reporter=Reporter(), store=store
            )
            args = {"nested": {"value": "original"}}
            task = asyncio.create_task(registry.execute(ToolCall("call", tool.name, args)))
            await asyncio.sleep(0)
            args["nested"]["value"] = "changed while awaiting approval"
            approval.resolve(
                next(iter(approval.pending)), chat_id="chat", user_id="admin", allowed=True
            )
            assert not (await task).is_error
            assert tool.calls == [{"nested": {"value": "original"}}]
            assert store.list_approvals()[0]["arguments"] == tool.calls[0]
        finally:
            store.close()

    asyncio.run(scenario())


def test_explicit_local_policy_and_execution_failure_are_audited(tmp_path: Path) -> None:
    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = RecordingTool(fail=True)
            registry = ToolRegistry([tool], approval=LocalApproval(), store=store)
            result = await registry.execute(ToolCall("call", tool.name, {}))
            assert result.is_error
            record = store.list_approvals()[0]
            assert record["policy"] == "LocalApproval"
            assert record["status"] == "approved"
            assert record["execution_status"] == "failed"
            assert "tool failed" in record["execution_error"]
        finally:
            store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["approved", "executing"])
def test_cancellation_after_approval_is_audited_without_reusing_permission(
    tmp_path: Path, phase: str
) -> None:
    class WaitingTool(RecordingTool):
        async def execute(self, arguments):
            self.calls.append(arguments)
            await asyncio.Future()

    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = WaitingTool()
            approval = ChatApproval({"admin"}, 10, store=store)
            registry = ToolRegistry(
                [tool], approval=approval, reporter=Reporter(), chat_id="chat", store=store
            )
            task = asyncio.create_task(registry.execute(ToolCall("call", tool.name, {})))
            await asyncio.sleep(0)
            approval_id = next(iter(approval.pending))
            approval.resolve(approval_id, chat_id="chat", user_id="admin", allowed=True)
            if phase == "executing":
                await asyncio.sleep(0)
                assert tool.calls == [{}]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            record = store.get_approval(approval_id)
            assert record["status"] == "approved"
            assert record["execution_status"] == "cancelled"
            assert not approval.pending
            if phase == "approved":
                assert not tool.calls
            # 相同工具调用 ID 再次执行也必须申请新审批。
            retry = asyncio.create_task(registry.execute(ToolCall("call", tool.name, {})))
            await asyncio.sleep(0)
            assert next(iter(approval.pending)) != approval_id
            retry.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retry
        finally:
            store.close()

    asyncio.run(scenario())


def test_failed_later_argument_chunk_prevents_execution(tmp_path: Path) -> None:
    class PartialReporter(Reporter):
        async def emit(self, event):
            await super().emit(event)
            if len(self.events) == 2:
                raise RuntimeError("second chunk failed")

    async def scenario():
        store = SessionStore(tmp_path / "audit.sqlite3")
        try:
            tool = RecordingTool()
            approval = ChatApproval({"admin"}, 10, store=store)
            registry = ToolRegistry(
                [tool], approval=approval, reporter=PartialReporter(), chat_id="chat", store=store
            )
            result = await registry.execute(ToolCall("call", tool.name, {"text": "中" * 2000}))
            assert result.is_error
            assert not tool.calls and not approval.pending
            record = store.list_approvals()[0]
            assert record["status"] == "notification_failed"
            assert "notified" not in [event["event"] for event in record["events"]]
        finally:
            store.close()

    asyncio.run(scenario())


def test_restart_expires_pending_and_preserves_completed_audit(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite3"
    store = SessionStore(db)
    call = ToolCall("call", "write_file", {"content": "完整内容"})
    for key in ("pending", "approved", "running", "succeeded"):
        store.create_approval(key, call, session_id="s", chat_id="chat", policy="ChatApproval")
        if key != "pending":
            store.record_approval_decision(key, "approved", user_id="admin")
        if key in {"running", "succeeded"}:
            store.record_approval_execution(key, key)
    store.close()
    for _ in range(2):
        store = SessionStore(db)
        try:
            assert store.get_approval("pending")["status"] == "interrupted"
            for key in ("approved", "running"):
                record = store.get_approval(key)
                assert record["status"] == "approved"
                assert record["execution_status"] == "interrupted"
                assert sum(e["event"] == "execution_interrupted" for e in record["events"]) == 1
            record = store.get_approval("succeeded")
            assert record["execution_status"] == "succeeded"
            assert record["decided_by"] == "admin"
            assert record["arguments"] == call.arguments
            assert not ChatApproval({"admin"}, 10, store=store).resolve(
                "pending", chat_id="chat", user_id="admin", allowed=True
            )
        finally:
            store.close()

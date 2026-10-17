from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol

import typer

from xd_agent.agent.models import ToolCall
from xd_agent.agent.reporting import AgentEvent, Reporter
from xd_agent.agent.sessions import SessionStore

logger = logging.getLogger(__name__)
MAX_APPROVAL_BYTES = 64_000
APPROVAL_CHUNK_BYTES = 1_600


@dataclass(slots=True, frozen=True)
class ApprovalRequest:
    """一次审批的标识、工具参数快照及原会话。"""

    id: str
    call: ToolCall
    chat_id: str | None


@dataclass(slots=True, frozen=True)
class ApprovalDecision:
    """审批结论及审计所需的状态、原因和审批人。"""

    allowed: bool
    status: str
    reason: str = ""
    user_id: str | None = None


class ApprovalGate(Protocol):
    """执行有副作用的工具前调用的审批策略。"""

    async def authorize(self, request: ApprovalRequest, *, reporter: Reporter) -> ApprovalDecision:
        """判断工具调用是否获准执行，并返回拒绝原因。"""
        ...


class LocalApproval:
    """仅供显式自动批准模式和评测使用的放行策略。"""

    async def authorize(self, request: ApprovalRequest, *, reporter: Reporter) -> ApprovalDecision:
        """直接允许调用，不产生审批提示。"""
        return ApprovalDecision(True, "approved", "显式选择本地自动放行策略")


class TerminalApproval:
    """CLI 中展示完整参数并逐次确认，空输入默认拒绝。"""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    async def authorize(self, request: ApprovalRequest, *, reporter: Reporter) -> ApprovalDecision:
        """终端无法读取确认或用户中断时拒绝执行。"""
        args = json.dumps(request.call.arguments, ensure_ascii=False)
        if len(args.encode("utf-8")) > MAX_APPROVAL_BYTES:
            return ApprovalDecision(False, "rejected", "审批参数超过 64KB，请拆分工具调用后重试")
        await reporter.emit(
            AgentEvent("notice", f"审批 {request.id}；工具 {request.call.name} 完整参数：\n{args}")
        )
        self.store.record_approval_notification(request.id)
        try:
            allowed = typer.confirm("允许执行以上工具调用吗？", default=False, err=True)
        except (typer.Abort, EOFError):
            return ApprovalDecision(False, "cancelled", "终端审批被取消或无法读取确认", "local")
        return ApprovalDecision(
            allowed,
            "approved" if allowed else "rejected",
            "" if allowed else "审批被拒绝",
            "local",
        )


@dataclass(slots=True)
class PendingApproval:
    """待处理审批，记录发起会话及等待审批结果的 Future。"""

    chat_id: str
    future: asyncio.Future[bool]
    ready: bool = False
    deadline: float | None = None
    decided_by: str | None = None


class ChatApproval:
    """管理聊天渠道中的敏感工具审批及超时等待。

    属性：allowed_user_ids 为可审批用户；pending 按审批 ID 保存进程内待办。
    """

    def __init__(
        self,
        allowed_user_ids: set[str],
        timeout_seconds: int,
        *,
        store: SessionStore | None = None,
    ) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.timeout_seconds = timeout_seconds
        self.pending: dict[str, PendingApproval] = {}
        self.store = store

    async def authorize(self, request: ApprovalRequest, *, reporter: Reporter) -> ApprovalDecision:
        """完整发送参数后开放审批；超长、发送失败、缺少会话和超时均拒绝。"""
        if not request.chat_id:
            return ApprovalDecision(False, "rejected", "审批缺少会话标识")
        args = json.dumps(request.call.arguments, ensure_ascii=False)
        if len(args.encode("utf-8")) > MAX_APPROVAL_BYTES:
            return ApprovalDecision(False, "rejected", "审批参数超过 64KB，请拆分工具调用后重试")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        pending = PendingApproval(
            request.chat_id, future, ready=False, deadline=loop.time() + self.timeout_seconds
        )
        self.pending[request.id] = pending
        try:
            async with asyncio.timeout(self.timeout_seconds):
                chunks = _split_arguments(args)
                for index, chunk in enumerate(chunks, 1):
                    text = (
                        f"审批 {request.id}；工具 {request.call.name} 请求执行，"
                        f"完整参数（{index}/{len(chunks)}）：\n{chunk}"
                    )
                    if len(chunks) == 1:
                        text += f"\n回复 approve {request.id} 或 reject {request.id}。"
                    await reporter.emit(AgentEvent("notice", text))
                if len(chunks) > 1:
                    await reporter.emit(
                        AgentEvent(
                            "notice",
                            f"审批 {request.id} 的完整参数已发送，"
                            f"请确认全部 {len(chunks)} 段内容。\n"
                            f"回复 approve {request.id} 或 reject {request.id}。",
                        )
                    )
                if self.store is not None:
                    self.store.record_approval_notification(request.id)
                pending.ready = True
                allowed = await future
            return ApprovalDecision(
                allowed,
                "approved" if allowed else "rejected",
                "" if allowed else "审批被拒绝",
                pending.decided_by,
            )
        except TimeoutError:
            return ApprovalDecision(False, "timed_out", "审批超时")
        except Exception:
            logger.exception("failed to send approval request id=%s", request.id)
            return ApprovalDecision(False, "notification_failed", "审批通知发送失败，工具未执行")
        finally:
            self.pending.pop(request.id, None)
            if not future.done():
                future.cancel()

    def resolve(self, approval_id: str, *, chat_id: str, user_id: str, allowed: bool) -> bool:
        """仅接受原会话中授权用户对未完成审批的决定，成功时唤醒等待者。"""
        pending = self.pending.get(approval_id)
        if (
            pending is None
            or pending.chat_id != chat_id
            or user_id not in self.allowed_user_ids
            or pending.future.done()
            or not pending.ready
            or (
                pending.deadline is not None
                and asyncio.get_running_loop().time() >= pending.deadline
            )
        ):
            if self.store is not None:
                self.store.record_approval_attempt(
                    approval_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    detail="无效、过期或重复的审批回复",
                )
            logger.warning("approval rejected id=%s chat=%s user=%s", approval_id, chat_id, user_id)
            return False
        if self.store is not None:
            self.store.record_approval_decision(
                approval_id,
                "approved" if allowed else "rejected",
                "" if allowed else "审批被拒绝",
                user_id=user_id,
            )
        pending.decided_by = user_id
        pending.future.set_result(allowed)
        return True


def _split_arguments(text: str) -> list[str]:
    """按 UTF-8 字节数分段，保留全部参数及 Unicode 字符。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if size + char_size > APPROVAL_CHUNK_BYTES:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(char)
        size += char_size
    chunks.append("".join(current))
    return chunks

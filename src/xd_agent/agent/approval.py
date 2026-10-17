from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from xd_agent.agent.models import ToolCall
from xd_agent.agent.reporting import AgentEvent, Reporter

logger = logging.getLogger(__name__)
SENSITIVE_TOOLS = frozenset({"write_file", "edit_file", "run_command"})


class ApprovalGate(Protocol):
    """工具审批接口；authorize 返回是否允许执行及拒绝原因。"""

    async def authorize(
        self, call: ToolCall, *, chat_id: str | None, reporter: Reporter
    ) -> tuple[bool, str]:
        """判断工具调用是否获准执行，并返回拒绝原因。"""
        ...


class LocalApproval:
    """本地 CLI 的审批策略，直接放行工具调用。"""

    async def authorize(
        self, call: ToolCall, *, chat_id: str | None, reporter: Reporter
    ) -> tuple[bool, str]:
        """直接允许调用，不产生审批提示。"""
        return True, ""


@dataclass(slots=True)
class PendingApproval:
    """待处理审批，记录发起会话及等待审批结果的 Future。"""

    chat_id: str
    future: asyncio.Future[bool]


class ChatApproval:
    """管理聊天渠道中的敏感工具审批及超时等待。

    属性：allowed_user_ids 为可审批用户；pending 按审批 ID 保存进程内待办。
    """

    def __init__(self, allowed_user_ids: set[str], timeout_seconds: int) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.timeout_seconds = timeout_seconds
        self.pending: dict[str, PendingApproval] = {}

    async def authorize(
        self, call: ToolCall, *, chat_id: str | None, reporter: Reporter
    ) -> tuple[bool, str]:
        """敏感工具向原会话发送审批请求，等待结果或超时；其他工具直接放行。"""
        if call.name not in SENSITIVE_TOOLS:
            return True, ""
        if chat_id is None:
            return False, "审批缺少会话标识"
        approval_id = uuid4().hex[:12]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = PendingApproval(chat_id, future)
        try:
            args = json.dumps(call.arguments, ensure_ascii=False)
            if len(args) > 2_000:
                args = args[:2_000] + "…"
            await reporter.emit(
                AgentEvent(
                    "notice",
                    f"工具 {call.name} 请求执行：\n{args}\n"
                    f"回复 approve {approval_id} 或 reject {approval_id}。",
                )
            )
            try:
                allowed = await asyncio.wait_for(future, timeout=self.timeout_seconds)
            except TimeoutError:
                return False, "审批超时"
            return (True, "") if allowed else (False, "审批被拒绝")
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, *, chat_id: str, user_id: str, allowed: bool) -> bool:
        """仅接受原会话中授权用户对未完成审批的决定，成功时唤醒等待者。"""
        pending = self.pending.get(approval_id)
        if (
            pending is None
            or pending.chat_id != chat_id
            or user_id not in self.allowed_user_ids
            or pending.future.done()
        ):
            logger.warning("approval rejected id=%s chat=%s user=%s", approval_id, chat_id, user_id)
            return False
        pending.future.set_result(allowed)
        return True

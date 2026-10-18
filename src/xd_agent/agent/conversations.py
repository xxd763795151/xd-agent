from __future__ import annotations

import asyncio
import logging

from xd_agent.agent.approval import ChatApproval
from xd_agent.agent.sessions import SessionStore

logger = logging.getLogger(__name__)


class ConversationControl:
    """各渠道共用的会话命令、持久化映射与聊天任务跟踪。"""

    def __init__(
        self, sessions: SessionStore, channel: str, approval: ChatApproval | None = None
    ) -> None:
        self.sessions = sessions
        self.channel = channel
        self.approval = approval
        self.tasks: set[asyncio.Task[None]] = set()
        self._task_chats: dict[asyncio.Task[None], str] = {}

    def session_id(self, chat_id: str) -> str:
        """读取聊天当前使用的逻辑会话，兼容未切换过的旧会话。"""
        return self.sessions.get_channel_session(self.channel, chat_id)

    def handle_command(self, content: str, chat_id: str) -> str | None:
        """同步处理 /new，返回通知文本；普通消息返回 None。调用前须校验权限。"""
        tokens = content.split()
        if not tokens or tokens[0].lower() != "/new":
            return None
        if len(tokens) != 1:
            return "请单独发送 /new 开启新会话，再发送任务内容。"
        if any(
            task_chat == chat_id and not task.done() for task, task_chat in self._task_chats.items()
        ) or (
            self.approval is not None
            and any(
                pending.chat_id == chat_id and not pending.future.done()
                for pending in self.approval.pending.values()
            )
        ):
            return "当前会话还有任务在执行或等待审批，请先完成后再发送 /new。"
        session_id = self.sessions.new_channel_session(self.channel, chat_id)
        logger.info("%s new session chat=%s session=%s", self.channel, chat_id, session_id)
        return "已开启新会话，后续消息将使用新的上下文。旧会话记录已保留。"

    def track(self, chat_id: str, task: asyncio.Task[None]) -> None:
        """在接收下一条消息前登记任务，包含尚未开始执行的排队任务。"""
        self.tasks.add(task)
        self._task_chats[task] = chat_id
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        """移除结束或取消的任务，释放聊天的忙碌状态。"""
        self.tasks.discard(task)
        self._task_chats.pop(task, None)

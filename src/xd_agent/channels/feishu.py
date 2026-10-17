from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path
from typing import Any, cast

from xd_agent.agent.approval import ChatApproval
from xd_agent.agent.reporting import AgentEvent, Reporter
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.core.config import AgentSettings, FeishuSettings, resolve_project_path

logger = logging.getLogger(__name__)


class FeishuReporter:
    """将 Agent 事件转换为飞书会话消息。"""

    def __init__(self, channel: Any, chat_id: str) -> None:
        self.channel = channel
        self.chat_id = chat_id

    async def emit(self, event: AgentEvent) -> None:
        """发送消息、工具状态或通知；发送失败时抛出异常。"""
        if event.kind == "tool_start":
            text = f"正在调用工具：{event.tool_name}"
        elif event.kind == "tool_result":
            status = "失败" if event.is_error else "完成"
            text = f"工具 {event.tool_name} {status}"
            if event.is_error:
                text += f"：{event.text}"
        else:
            text = event.text
        result = await self.channel.send(self.chat_id, {"text": text})
        if not getattr(result, "success", False):
            raise RuntimeError("failed to send Feishu message")


class FeishuAdapter:
    """通过飞书 SDK 接收 Webhook 并按聊天会话运行 Agent。

    属性：approval 保存进程内审批请求；tasks 跟踪后台任务以便关闭时取消。
    """

    def __init__(
        self,
        agent: AgentService,
        sessions: SessionStore,
        settings: FeishuSettings,
        agent_settings: AgentSettings,
    ) -> None:
        self.agent = agent
        self.sessions = sessions
        self.settings = settings
        self.agent_settings = agent_settings
        self.approval = ChatApproval(
            set(settings.allowed_approver_ids), settings.approval_timeout_seconds
        )
        self.channel: Any = None
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """校验凭据，注册 SDK 回调并等待飞书渠道就绪。"""
        app_id = self.settings.app_id
        app_secret = self.settings.app_secret
        verification_token = self.settings.verification_token
        encrypt_key = self.settings.encrypt_key
        if (
            not app_id
            or app_secret is None
            or verification_token is None
            or encrypt_key is None
            or not self.settings.allowed_approver_ids
        ):
            raise ValueError(
                "Feishu requires app_id, app_secret, verification_token, "
                "encrypt_key and allowed_approver_ids"
            )
        try:
            sdk = importlib.import_module("lark_channel")
        except ImportError as exc:
            raise RuntimeError("install the feishu extra to enable the Feishu channel") from exc
        self.channel = sdk.FeishuChannel(
            app_id=app_id,
            app_secret=app_secret.get_secret_value(),
            verification_token=verification_token.get_secret_value(),
            encrypt_key=encrypt_key.get_secret_value(),
            transport="webhook",
            security=sdk.SecurityConfig(mode="strict"),
        )
        self.channel.on("message", self._on_message)
        self.channel.on("error", self._on_error)
        await self.channel.connect_until_ready(timeout=30)

    async def stop(self) -> None:
        """取消运行中的任务并断开飞书渠道。"""
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.channel is not None:
            await self.channel.disconnect()

    async def handle_webhook(
        self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        """将 HTTP 请求交给 SDK 执行校验、解密与事件分发。"""
        if self.channel is None:
            raise RuntimeError("Feishu channel is not ready")
        result = await self.channel.handle_webhook_request(headers=headers, body=body)
        return cast(tuple[int, bytes], result)

    async def verify_webhook(self, query: dict[str, str]) -> tuple[int, bytes]:
        """飞书渠道不使用 GET 验证入口，返回方法不允许。"""
        return 405, b"method not allowed"

    async def _on_error(self, error: Exception) -> None:
        """记录飞书 SDK 报告的渠道错误。"""
        logger.error("Feishu channel error: %s", error)

    async def _on_message(self, message: Any) -> None:
        """去重文本事件，先处理审批回复，再校验发起人并启动任务。"""
        if getattr(message, "raw_content_type", "text") != "text":
            return
        chat_id = str(message.chat_id)
        sender_id = str(message.sender_id)
        message_id = str(message.message_id)
        content = str(message.content_text).strip()
        if not content or not self.sessions.claim_event("feishu", message_id):
            return
        tokens = content.split()
        if len(tokens) == 2 and tokens[0].lower() in {"approve", "reject"}:
            self.approval.resolve(
                tokens[1],
                chat_id=chat_id,
                user_id=sender_id,
                allowed=tokens[0].lower() == "approve",
            )
            return
        allowed_senders = self.settings.allowed_sender_ids or self.settings.allowed_approver_ids
        if sender_id not in allowed_senders:
            logger.warning("Feishu sender denied chat=%s sender=%s", chat_id, sender_id)
            return
        task = asyncio.create_task(self._process(chat_id, content))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _process(self, chat_id: str, prompt: str) -> None:
        """在聊天对应的持久化会话中运行 Agent，并报告执行错误。"""
        reporter: Reporter = FeishuReporter(self.channel, chat_id)
        workdir: Path = resolve_project_path(self.agent_settings.workspace_dir)
        try:
            await self.agent.run(
                prompt,
                session_id=f"feishu:{chat_id}",
                workdir=workdir,
                reporter=reporter,
                approval=self.approval,
                chat_id=chat_id,
            )
        except Exception:
            logger.exception("Feishu agent run failed chat=%s", chat_id)
            try:
                await reporter.emit(AgentEvent("notice", "任务执行失败，请查看服务日志。"))
            except Exception:
                logger.exception("failed to report Feishu task error")

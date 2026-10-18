from __future__ import annotations

import asyncio
import importlib
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from xd_agent.agent.approval import ChatApproval
from xd_agent.agent.conversations import ConversationControl
from xd_agent.agent.reporting import AgentEvent, Reporter
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.core.config import AgentSettings, DingTalkSettings, resolve_project_path

logger = logging.getLogger(__name__)


class DingTalkSendError(RuntimeError):
    """钉钉消息未能送达；错误信息保留接口诊断并隐藏会话凭据。"""


class DingTalkReporter:
    """使用钉钉会话 Webhook 发送 Agent 事件。

    初始化时仅接受钉钉 HTTPS 域名的会话地址。
    """

    def __init__(self, client: httpx.AsyncClient, session_webhook: str) -> None:
        parsed = urlsplit(session_webhook)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not (parsed.hostname == "dingtalk.com" or parsed.hostname.endswith(".dingtalk.com"))
        ):
            raise ValueError("invalid DingTalk session webhook")
        self.client = client
        self.session_webhook = session_webhook

    async def emit(self, event: AgentEvent) -> None:
        """将事件转换为文本消息并检查钉钉接口返回码。"""
        if event.kind == "tool_start":
            text = f"正在调用工具：{event.tool_name}"
        elif event.kind == "tool_result":
            status = "失败" if event.is_error else "完成"
            text = f"工具 {event.tool_name} {status}"
            if event.is_error:
                text += f"：{event.text}"
        else:
            text = event.text
        if not text.strip():
            return
        try:
            response = await self.client.post(
                self.session_webhook, json={"msgtype": "text", "text": {"content": text}}
            )
        except httpx.RequestError as exc:
            # HTTP 异常的默认文本可能包含带会话凭据的完整 URL。
            raise DingTalkSendError(
                f"failed to send DingTalk message: {type(exc).__name__}"
            ) from None
        try:
            data = response.json()
        except ValueError:
            raise DingTalkSendError(
                f"failed to send DingTalk message: HTTP {response.status_code}, non-JSON response"
            ) from None
        if not isinstance(data, dict):
            raise DingTalkSendError(
                f"failed to send DingTalk message: HTTP {response.status_code}, invalid response"
            )
        if response.is_success and data.get("errcode") == 0:
            return
        details = [f"HTTP {response.status_code}"]
        for key in ("errcode", "errmsg", "code", "message", "request_id", "requestId"):
            if key in data:
                details.append(f"{key}={self._safe_detail(data[key])}")
        if "errcode" not in data:
            details.append("missing errcode")
        raise DingTalkSendError(f"failed to send DingTalk message: {', '.join(details)}")

    def _safe_detail(self, value: Any) -> str:
        """仅输出限长的接口诊断字段，移除可能回显的 Webhook 和查询凭据。"""
        detail = str(value).replace(self.session_webhook, "[redacted webhook]")
        for _, secret in parse_qsl(urlsplit(self.session_webhook).query):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        detail = re.sub(r"https?://\S+", "[redacted URL]", detail)
        return " ".join(detail.split())[:500]


class DingTalkAdapter:
    """通过钉钉 Stream 接收消息，并按会话运行 Agent。

    属性：stream_task 管理长连接；tasks 跟踪会话任务；approval 保存待审批请求。
    """

    def __init__(
        self,
        agent: AgentService,
        sessions: SessionStore,
        settings: DingTalkSettings,
        agent_settings: AgentSettings,
    ) -> None:
        self.agent = agent
        self.sessions = sessions
        self.settings = settings
        self.agent_settings = agent_settings
        self.approval = ChatApproval(
            set(settings.allowed_approver_ids), settings.approval_timeout_seconds, store=sessions
        )
        self.http_client = httpx.AsyncClient(timeout=15)
        self.client: Any = None
        self.stream_task: asyncio.Task[None] | None = None
        self.conversations = ConversationControl(sessions, "dingtalk", self.approval)
        self.tasks = self.conversations.tasks

    async def start(self) -> None:
        """校验凭据并启动 Stream，最多等待 30 秒建立连接。"""
        if (
            not self.settings.client_id
            or self.settings.client_secret is None
            or not self.settings.client_secret.get_secret_value()
            or not self.settings.allowed_approver_ids
        ):
            raise ValueError("DingTalk requires client_id, client_secret and allowed_approver_ids")
        try:
            sdk = importlib.import_module("dingtalk_stream")
        except ImportError as exc:
            raise RuntimeError("install the dingtalk extra to enable the DingTalk channel") from exc

        adapter = self

        class Handler(sdk.ChatbotHandler):  # type: ignore[name-defined,misc]
            """将 SDK 的机器人回调转交给当前适配器。"""

            async def process(self, callback: Any) -> tuple[int, str]:
                """归一化消息并向 Stream 确认已接收。"""
                logger.info("DingTalk callback received topic=%s", sdk.ChatbotMessage.TOPIC)
                message = sdk.ChatbotMessage.from_dict(callback.data)
                await adapter._on_message(message)
                return sdk.AckMessage.STATUS_OK, "OK"

        credential = sdk.Credential(
            self.settings.client_id, self.settings.client_secret.get_secret_value()
        )
        self.client = sdk.DingTalkStreamClient(credential)
        self.client.register_callback_handler(sdk.ChatbotMessage.TOPIC, Handler())
        self.stream_task = asyncio.create_task(self.client.start())
        self.stream_task.add_done_callback(self._stream_finished)
        try:
            async with asyncio.timeout(30):
                while self.client.websocket is None:
                    if self.stream_task.done():
                        await self.stream_task
                        raise RuntimeError("DingTalk stream stopped before connecting")
                    await asyncio.sleep(0.1)
        except TimeoutError as exc:
            raise RuntimeError("DingTalk Stream did not connect within 30 seconds") from exc
        logger.info("DingTalk Stream connected topic=%s", sdk.ChatbotMessage.TOPIC)

    def _stream_finished(self, task: asyncio.Task[None]) -> None:
        """记录意外结束的 Stream 任务异常。"""
        if not task.cancelled() and task.exception() is not None:
            logger.error("DingTalk stream stopped", exc_info=task.exception())

    async def stop(self) -> None:
        """取消会话任务，停止 Stream 并关闭 HTTP 客户端。"""
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.stop()
        if self.stream_task is not None:
            await asyncio.gather(self.stream_task, return_exceptions=True)
        await self.http_client.aclose()

    async def handle_webhook(
        self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        """Stream 模式不接收入站 Webhook。"""
        return 404, b"DingTalk uses Stream mode"

    async def verify_webhook(self, query: dict[str, str]) -> tuple[int, bytes]:
        """Stream 模式不提供 Webhook 验证。"""
        return 404, b"DingTalk uses Stream mode"

    async def _on_message(self, message: Any) -> None:
        """过滤并去重文本消息，处理审批或启动授权用户的任务。"""
        logger.info(
            "DingTalk message received type=%s chat=%s sender=%s message_id=%s",
            message.message_type,
            message.conversation_id,
            message.sender_staff_id,
            message.message_id,
        )
        if message.message_type != "text" or message.text is None:
            logger.info("DingTalk message ignored reason=unsupported_message_type")
            return
        chat_id = str(message.conversation_id or "")
        sender_id = str(message.sender_staff_id or "")
        message_id = str(message.message_id or "")
        content = str(message.text.content or "").strip()
        if not chat_id or not sender_id or not message_id or not content:
            logger.warning(
                "DingTalk message ignored reason=missing_fields "
                "has_chat=%s has_sender=%s has_message_id=%s has_content=%s",
                bool(chat_id),
                bool(sender_id),
                bool(message_id),
                bool(content),
            )
            return
        if not self.sessions.claim_event("dingtalk", message_id):
            logger.info("DingTalk message ignored reason=duplicate message_id=%s", message_id)
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
            logger.warning("DingTalk sender denied chat=%s sender=%s", chat_id, sender_id)
            return
        notice = self.conversations.handle_command(content, chat_id)
        if notice is not None:
            self.conversations.track(
                chat_id,
                asyncio.create_task(
                    self._send_notice(chat_id, str(message.session_webhook), notice)
                ),
            )
            return
        logger.info("DingTalk task queued chat=%s message_id=%s", chat_id, message_id)
        task = asyncio.create_task(self._process(chat_id, content, str(message.session_webhook)))
        self.conversations.track(chat_id, task)

    async def _send_notice(self, chat_id: str, session_webhook: str, text: str) -> None:
        """在后台发送会话命令结果，不调用模型或阻塞 Stream 确认。"""
        try:
            reporter = DingTalkReporter(self.http_client, session_webhook)
            await reporter.emit(AgentEvent("notice", text))
        except DingTalkSendError as exc:
            logger.error("DingTalk reply delivery failed chat=%s: %s", chat_id, exc)
        except Exception:
            logger.exception("failed to report DingTalk command result chat=%s", chat_id)

    async def _process(self, chat_id: str, prompt: str, session_webhook: str) -> None:
        """使用当前消息的会话 Webhook 报告 Agent 进度和错误。"""
        try:
            reporter: Reporter = DingTalkReporter(self.http_client, session_webhook)
            await self.agent.run(
                prompt,
                session_id=self.conversations.session_id(chat_id),
                workdir=resolve_project_path(self.agent_settings.workspace_dir),
                reporter=reporter,
                approval=self.approval,
                chat_id=chat_id,
            )
        except DingTalkSendError as exc:
            logger.error("DingTalk reply delivery failed chat=%s: %s", chat_id, exc)
        except Exception:
            logger.exception("DingTalk agent run failed chat=%s", chat_id)
            if "reporter" in locals():
                try:
                    await reporter.emit(AgentEvent("notice", "任务执行失败，请查看服务日志。"))
                except Exception:
                    logger.exception("failed to report DingTalk task error")

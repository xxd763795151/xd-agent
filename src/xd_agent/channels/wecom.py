from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import struct
import time
from typing import Any
from xml.etree import ElementTree

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from xd_agent.agent.approval import ApprovalDecision, ApprovalRequest, ChatApproval
from xd_agent.agent.conversations import ConversationControl
from xd_agent.agent.reporting import AgentEvent, Reporter
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.core.config import AgentSettings, WeComSettings, resolve_project_path

logger = logging.getLogger(__name__)
_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComCrypto:
    """校验企业微信回调签名并解密 AES-CBC 消息。

    属性：key 为解码后的 32 字节密钥；corp_id 用于核对消息接收方。
    """

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        if len(encoding_aes_key) != 43:
            raise ValueError("WeCom encoding_aes_key must have 43 characters")
        try:
            self.key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid WeCom encoding_aes_key") from exc
        if len(self.key) != 32:
            raise ValueError("invalid WeCom encoding_aes_key")
        self.token = token
        self.corp_id = corp_id

    def decrypt(self, encrypted: str, signature: str, timestamp: str, nonce: str) -> bytes:
        """验证签名、填充和接收方，返回回调消息明文。"""
        expected = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, encrypted))).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("invalid WeCom callback signature")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            padding = padded[-1]
            if padding < 1 or padding > 32 or padded[-padding:] != bytes([padding]) * padding:
                raise ValueError("invalid padding")
            payload = padded[:-padding]
            length = struct.unpack("!I", payload[16:20])[0]
            if len(payload) < 20 + length or payload[20 + length :].decode() != self.corp_id:
                raise ValueError("invalid WeCom receiver")
            return payload[20 : 20 + length]
        except (ValueError, IndexError, UnicodeDecodeError, struct.error) as exc:
            raise ValueError("invalid WeCom encrypted payload") from exc


class WeComReporter:
    """将 Agent 事件发送给指定企业微信成员。"""

    def __init__(self, adapter: WeComAdapter, user_id: str) -> None:
        self.adapter = adapter
        self.user_id = user_id

    async def emit(self, event: AgentEvent) -> None:
        """将工具状态或普通文本转成应用消息。"""
        if event.kind == "tool_start":
            text = f"正在调用工具：{event.tool_name}"
        elif event.kind == "tool_result":
            status = "失败" if event.is_error else "完成"
            text = f"工具 {event.tool_name} {status}"
            if event.is_error:
                text += f"：{event.text}"
        else:
            text = event.text
        await self.adapter.send_text(self.user_id, text)


class ApprovalReporter:
    """把审批通知同时发送给发起人与其他审批人。"""

    def __init__(self, adapter: WeComAdapter, requester: str, reporter: Reporter) -> None:
        self.adapter = adapter
        self.requester = requester
        self.reporter = reporter

    async def emit(self, event: AgentEvent) -> None:
        """先通知发起人，再向其余配置的审批人转发请求。"""
        await self.reporter.emit(event)
        for user_id in self.adapter.settings.allowed_approver_ids:
            if user_id != self.requester:
                await self.adapter.send_text(
                    user_id, f"来自 {self.requester} 的审批请求：\n{event.text}"
                )


class WeComApproval(ChatApproval):
    """支持跨成员单聊审批的企业微信审批策略。"""

    def __init__(self, adapter: WeComAdapter) -> None:
        super().__init__(
            set(adapter.settings.allowed_approver_ids),
            adapter.settings.approval_timeout_seconds,
            store=adapter.sessions,
        )
        self.adapter = adapter

    async def authorize(self, request: ApprovalRequest, *, reporter: Reporter) -> ApprovalDecision:
        """包装通知接收者后沿用聊天审批流程。"""
        wrapped = (
            ApprovalReporter(self.adapter, request.chat_id, reporter)
            if request.chat_id
            else reporter
        )
        return await super().authorize(request, reporter=wrapped)


class WeComAdapter:
    """处理企业微信应用回调、消息发送及会话执行。

    属性：_access_token 和 _token_expires 缓存凭据；_token_lock 避免并发刷新。
    """

    def __init__(
        self,
        agent: AgentService,
        sessions: SessionStore,
        settings: WeComSettings,
        agent_settings: AgentSettings,
    ) -> None:
        self.agent = agent
        self.sessions = sessions
        self.settings = settings
        self.agent_settings = agent_settings
        self.approval = WeComApproval(self)
        self.crypto: WeComCrypto | None = None
        self.http_client = httpx.AsyncClient(timeout=15)
        self.conversations = ConversationControl(sessions, "wecom", self.approval)
        self.tasks = self.conversations.tasks
        self._access_token = ""
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()

    async def start(self) -> None:
        """校验企业微信配置并初始化回调解密器。"""
        if (
            not self.settings.corp_id
            or self.settings.corp_secret is None
            or not self.settings.corp_secret.get_secret_value()
            or self.settings.agent_id is None
            or self.settings.token is None
            or not self.settings.token.get_secret_value()
            or self.settings.encoding_aes_key is None
            or not self.settings.allowed_approver_ids
        ):
            raise ValueError(
                "WeCom requires corp_id, corp_secret, agent_id, token, "
                "encoding_aes_key and allowed_approver_ids"
            )
        self.crypto = WeComCrypto(
            self.settings.token.get_secret_value(),
            self.settings.encoding_aes_key.get_secret_value(),
            self.settings.corp_id,
        )

    async def stop(self) -> None:
        """取消后台任务并关闭 HTTP 客户端。"""
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.http_client.aclose()

    async def verify_webhook(self, query: dict[str, str]) -> tuple[int, bytes]:
        """解密 GET 验证参数并回显明文，非法回调返回 403。"""
        if self.crypto is None:
            raise RuntimeError("WeCom channel is not ready")
        try:
            plain = self.crypto.decrypt(
                query["echostr"], query["msg_signature"], query["timestamp"], query["nonce"]
            )
            return 200, plain
        except (KeyError, ValueError):
            return 403, b"invalid callback"

    async def handle_webhook(
        self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        """验证、解密并处理 POST 消息，非法回调返回 403。"""
        if self.crypto is None:
            raise RuntimeError("WeCom channel is not ready")
        try:
            params = query or {}
            encrypted = ElementTree.fromstring(body).findtext("Encrypt")
            if not encrypted:
                raise ValueError("missing Encrypt")
            plain = self.crypto.decrypt(
                encrypted, params["msg_signature"], params["timestamp"], params["nonce"]
            )
            message = ElementTree.fromstring(plain)
            await self._on_message(message)
            return 200, b"success"
        except (ElementTree.ParseError, KeyError, ValueError):
            return 403, b"invalid callback"

    async def _on_message(self, message: ElementTree.Element) -> None:
        """过滤非目标应用消息，去重后处理审批或启动任务。"""
        if message.findtext("MsgType") != "text":
            return
        if message.findtext("ToUserName") != self.settings.corp_id:
            return
        agent_id = message.findtext("AgentID")
        if agent_id is not None and agent_id != str(self.settings.agent_id):
            return
        sender_id = message.findtext("FromUserName") or ""
        message_id = message.findtext("MsgId") or ""
        content = (message.findtext("Content") or "").strip()
        if not sender_id or not message_id or not content:
            return
        if not self.sessions.claim_event("wecom", message_id):
            return
        tokens = content.split()
        if len(tokens) == 2 and tokens[0].lower() in {"approve", "reject"}:
            pending = self.approval.pending.get(tokens[1])
            self.approval.resolve(
                tokens[1],
                chat_id=pending.chat_id if pending is not None else sender_id,
                user_id=sender_id,
                allowed=tokens[0].lower() == "approve",
            )
            return
        allowed_senders = self.settings.allowed_sender_ids or self.settings.allowed_approver_ids
        if sender_id not in allowed_senders:
            logger.warning("WeCom sender denied sender=%s", sender_id)
            return
        notice = self.conversations.handle_command(content, sender_id)
        if notice is not None:
            self.conversations.track(
                sender_id, asyncio.create_task(self._send_notice(sender_id, notice))
            )
            return
        task = asyncio.create_task(self._process(sender_id, content))
        self.conversations.track(sender_id, task)

    async def _send_notice(self, user_id: str, text: str) -> None:
        """在后台发送会话命令结果，不调用模型或阻塞 Webhook 确认。"""
        try:
            await WeComReporter(self, user_id).emit(AgentEvent("notice", text))
        except Exception:
            logger.exception("failed to report WeCom command result user=%s", user_id)

    async def _get_access_token(self) -> str:
        """在锁内复用未过期令牌，必要时向企业微信接口刷新。"""
        async with self._token_lock:
            if self._access_token and time.monotonic() < self._token_expires:
                return self._access_token
            assert self.settings.corp_id is not None
            assert self.settings.corp_secret is not None
            response = await self.http_client.get(
                f"{_API_BASE}/gettoken",
                params={
                    "corpid": self.settings.corp_id,
                    "corpsecret": self.settings.corp_secret.get_secret_value(),
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            if data.get("errcode") != 0 or not data.get("access_token"):
                raise RuntimeError("failed to get WeCom access token")
            self._access_token = str(data["access_token"])
            self._token_expires = time.monotonic() + max(1, int(data.get("expires_in", 7200)) - 300)
            return self._access_token

    async def send_text(self, user_id: str, text: str) -> None:
        """按 UTF-8 字节数拆分长文本，逐段发送给成员。"""
        while text:
            size = 0
            cut = 0
            for char in text:
                char_size = len(char.encode("utf-8"))
                if size + char_size > 2048:
                    break
                size += char_size
                cut += 1
            if cut == len(text):
                await self._send_chunk(user_id, text)
                return
            line_end = text.rfind("\n", 0, cut)
            if line_end > 0:
                cut = line_end + 1
            await self._send_chunk(user_id, text[:cut])
            text = text[cut:]

    async def _send_chunk(self, user_id: str, text: str) -> None:
        """发送单段文本；令牌失效时清空缓存并重试一次。"""
        for attempt in range(2):
            token = await self._get_access_token()
            response = await self.http_client.post(
                f"{_API_BASE}/message/send",
                params={"access_token": token},
                json={
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": self.settings.agent_id,
                    "text": {"content": text},
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            if data.get("errcode") == 0 and not data.get("invaliduser"):
                return
            if attempt == 0 and data.get("errcode") in {40014, 42001}:
                self._access_token = ""
                continue
            raise RuntimeError("failed to send WeCom message")

    async def _process(self, user_id: str, prompt: str) -> None:
        """以成员 ID 作为会话标识运行 Agent，并回报执行失败。"""
        reporter: Reporter = WeComReporter(self, user_id)
        try:
            await self.agent.run(
                prompt,
                session_id=self.conversations.session_id(user_id),
                workdir=resolve_project_path(self.agent_settings.workspace_dir),
                reporter=reporter,
                approval=self.approval,
                chat_id=user_id,
            )
        except Exception:
            logger.exception("WeCom agent run failed user=%s", user_id)
            try:
                await reporter.emit(AgentEvent("notice", "任务执行失败，请查看服务日志。"))
            except Exception:
                logger.exception("failed to report WeCom task error")

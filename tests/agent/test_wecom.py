import asyncio
import base64
import hashlib
import json
import os
import struct
from pathlib import Path
from xml.etree import ElementTree

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import SecretStr

from xd_agent.agent.approval import PendingApproval
from xd_agent.agent.models import ToolCall
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.wecom import WeComAdapter, WeComReporter
from xd_agent.core.config import AgentSettings, WeComSettings

TOKEN = "callback-token"
KEY = bytes(range(32))
AES_KEY = base64.b64encode(KEY).decode().rstrip("=")
CORP_ID = "corp-1"


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, **kwargs: object) -> None:
        self.calls.append((prompt, str(kwargs["session_id"])))


def encrypt(plain: bytes) -> tuple[str, dict[str, str]]:
    data = os.urandom(16) + struct.pack("!I", len(plain)) + plain + CORP_ID.encode()
    padding = 32 - len(data) % 32
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
    encrypted = base64.b64encode(
        encryptor.update(data + bytes([padding]) * padding) + encryptor.finalize()
    ).decode()
    query = {"timestamp": "123", "nonce": "456"}
    query["msg_signature"] = hashlib.sha1(
        "".join(sorted((TOKEN, query["timestamp"], query["nonce"], encrypted))).encode()
    ).hexdigest()
    return encrypted, query


def make_adapter(tmp_path: Path) -> tuple[WeComAdapter, FakeAgent, SessionStore]:
    store = SessionStore(tmp_path / "session.sqlite3")
    agent = FakeAgent()
    adapter = WeComAdapter(
        agent,
        store,
        WeComSettings(
            corp_id=CORP_ID,
            corp_secret=SecretStr("secret"),
            agent_id=1000001,
            token=SecretStr(TOKEN),
            encoding_aes_key=SecretStr(AES_KEY),
            allowed_approver_ids=["admin"],
            allowed_sender_ids=["requester"],
        ),
        AgentSettings(workspace_dir=tmp_path),
    )
    return adapter, agent, store


def test_wecom_verification_ingress_dedupe_and_approval(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter, agent, store = make_adapter(tmp_path)
        await adapter.start()
        echo, query = encrypt(b"echo-value")
        assert await adapter.verify_webhook({**query, "echostr": echo}) == (200, b"echo-value")
        bad_query = {**query, "echostr": echo, "nonce": "wrong"}
        assert (await adapter.verify_webhook(bad_query))[0] == 403

        def message(sender: str, message_id: str, content: str) -> bytes:
            root = ElementTree.Element("xml")
            for key, value in {
                "ToUserName": CORP_ID,
                "FromUserName": sender,
                "MsgType": "text",
                "Content": content,
                "MsgId": message_id,
                "AgentID": "1000001",
            }.items():
                ElementTree.SubElement(root, key).text = value
            return ElementTree.tostring(root)

        async def post(sender: str, message_id: str, content: str) -> tuple[int, bytes]:
            encrypted, params = encrypt(message(sender, message_id, content))
            body = f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode()
            return await adapter.handle_webhook({}, body, params)

        assert await post("outsider", "1", "ignored") == (200, b"success")
        assert await post("requester", "2", "task") == (200, b"success")
        assert await post("requester", "2", "task") == (200, b"success")
        await asyncio.gather(*adapter.tasks)
        assert agent.calls == [("task", "wecom:requester")]

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        adapter.approval.pending["approval-1"] = PendingApproval("requester", future)
        assert await post("admin", "3", "approve approval-1") == (200, b"success")
        assert future.result() is True

        encrypted, params = encrypt(message("requester", "4", "task"))
        assert (
            await adapter.handle_webhook(
                {}, f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode(),
                {**params, "msg_signature": "bad"},
            )
        )[0] == 403
        await adapter.stop()
        store.close()

    asyncio.run(scenario())


def test_wecom_send_refreshes_token_and_splits_long_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter, _, store = make_adapter(tmp_path)
        await adapter.start()
        sent: list[httpx.Request] = []
        token_requests = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal token_requests
            if request.url.path.endswith("/gettoken"):
                token_requests += 1
                return httpx.Response(
                    200, json={"errcode": 0, "access_token": "token", "expires_in": 7200}
                )
            sent.append(request)
            return httpx.Response(200, json={"errcode": 0})

        await adapter.http_client.aclose()
        adapter.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        await adapter.send_text("requester", "你好" * 1000)
        assert len(sent) == 3
        assert token_requests == 1
        assert all(len(request.content) < 2600 for request in sent)
        assert "".join(json.loads(request.content)["text"]["content"] for request in sent) == (
            "你好" * 1000
        )
        sent.clear()
        await adapter.send_text("requester", "工具请求：\n" + "中" * 1000 + "\n回复 approve abc123")
        chunks = [json.loads(request.content)["text"]["content"] for request in sent]
        assert "approve abc123" in chunks[-1]
        assert all("approve abc123" not in chunk for chunk in chunks[:-1])
        await adapter.stop()
        store.close()

    asyncio.run(scenario())


def test_wecom_approval_notifies_distinct_approver(tmp_path: Path) -> None:
    async def scenario() -> None:
        adapter, _, store = make_adapter(tmp_path)
        await adapter.start()
        sent: list[tuple[str, str]] = []

        async def send(user_id: str, text: str) -> None:
            sent.append((user_id, text))

        adapter.send_text = send  # type: ignore[method-assign]
        task = asyncio.create_task(
            adapter.approval.authorize(
                ToolCall("call", "write_file", {"path": "note.txt"}),
                chat_id="requester",
                reporter=WeComReporter(adapter, "requester"),
            )
        )
        await asyncio.sleep(0)
        approval_id = next(iter(adapter.approval.pending))
        assert {user for user, _ in sent} == {"requester", "admin"}
        assert all(approval_id in text for _, text in sent)
        message = ElementTree.fromstring(
            f"<xml><ToUserName>{CORP_ID}</ToUserName><FromUserName>admin</FromUserName>"
            f"<MsgType>text</MsgType><Content>approve {approval_id}</Content>"
            "<MsgId>approval-1</MsgId>"
            "<AgentID>1000001</AgentID></xml>"
        )
        await adapter._on_message(message)
        assert await task == (True, "")
        await adapter.stop()
        store.close()

    asyncio.run(scenario())

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from xd_agent.agent.models import Message, ToolCall, ToolResult, Usage


@dataclass(slots=True, frozen=True)
class SessionSnapshot:
    """会话快照；id、workdir 标识会话，messages 为历史消息。

    input_tokens、output_tokens 和 cost 是会话累计用量。
    """

    id: str
    workdir: Path
    messages: tuple[Message, ...]
    input_tokens: int
    output_tokens: int
    cost: float


class SessionStore:
    """使用 SQLite 保存会话、消息、用量及入站事件去重记录。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                workdir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbound_events (
                channel TEXT NOT NULL,
                event_id TEXT NOT NULL,
                PRIMARY KEY (channel, event_id)
            );
            CREATE TABLE IF NOT EXISTS channel_sessions (
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                PRIMARY KEY (channel, chat_id)
            );
            CREATE TABLE IF NOT EXISTS session_memory (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                summary TEXT NOT NULL,
                message_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                chat_id TEXT,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                policy TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                reason TEXT NOT NULL DEFAULT '',
                execution_status TEXT NOT NULL DEFAULT 'not_started',
                execution_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS approval_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT NOT NULL REFERENCES approvals(id),
                occurred_at TEXT NOT NULL,
                event TEXT NOT NULL,
                user_id TEXT,
                chat_id TEXT,
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS approval_events_by_request
                ON approval_events(approval_id, id);
            """
        )
        self._recover_approvals()

    def _recover_approvals(self) -> None:
        """单进程重启时终止遗留审批及未知执行，绝不复用旧批准。"""
        with self._connection:
            rows = self._connection.execute(
                "SELECT id, status FROM approvals WHERE status = 'pending' "
                "OR (status = 'approved' AND execution_status IN ('not_started', 'running'))"
            ).fetchall()
            for approval_id, status in rows:
                if status == "pending":
                    self.record_approval_decision(
                        approval_id, "interrupted", "服务重启，审批已失效，请重新申请"
                    )
                else:
                    self.record_approval_execution(
                        approval_id, "interrupted", "服务重启，工具执行结果未知，请检查实际状态"
                    )

    def create_approval(
        self,
        approval_id: str,
        call: ToolCall,
        *,
        session_id: str | None,
        chat_id: str | None,
        policy: str,
    ) -> None:
        """在通知和执行前保存完整审批快照。"""
        with self._connection:
            self._connection.execute(
                "INSERT INTO approvals "
                "(id, session_id, chat_id, tool_call_id, tool_name, arguments, "
                "policy, requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    session_id,
                    chat_id,
                    call.id,
                    call.name,
                    json.dumps(call.arguments, ensure_ascii=False),
                    policy,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._append_approval_event(approval_id, "requested", chat_id=chat_id)

    def record_approval_decision(
        self, approval_id: str, status: str, reason: str = "", *, user_id: str | None = None
    ) -> None:
        """原子保存审批结论和审批人，并追加审计事件。"""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE approvals SET status = ?, reason = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, reason, user_id, datetime.now(UTC).isoformat(), approval_id),
            )
            if cursor.rowcount:
                self._append_approval_event(approval_id, status, user_id=user_id, detail=reason)

    def record_approval_execution(self, approval_id: str, status: str, error: str = "") -> None:
        """记录批准后的工具执行开始、完成、失败或中断。"""
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE approvals SET execution_status = ?, execution_error = ? WHERE id = ?",
                (status, error, approval_id),
            )
            if cursor.rowcount:
                self._append_approval_event(approval_id, f"execution_{status}", detail=error)

    def record_command_assessment(self, approval_id: str, description: str) -> None:
        """在执行前保存完整命令分析，人工或自动批准均可追溯判断依据。"""
        with self._connection:
            self._append_approval_event(approval_id, "command_assessed", detail=description)

    def record_approval_notification(self, approval_id: str) -> None:
        """记录完整参数和批准指令均已成功发送。"""
        with self._connection:
            if self._connection.execute(
                "SELECT 1 FROM approvals WHERE id = ? AND status = 'pending'", (approval_id,)
            ).fetchone():
                self._append_approval_event(approval_id, "notified")

    def record_approval_attempt(
        self, approval_id: str, *, chat_id: str, user_id: str, detail: str
    ) -> None:
        """保存非法、过期或重复审批尝试；未知审批 ID 不创建记录。"""
        with self._connection:
            if self._connection.execute(
                "SELECT 1 FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone():
                self._append_approval_event(
                    approval_id, "invalid_reply", chat_id=chat_id, user_id=user_id, detail=detail
                )

    def _append_approval_event(
        self,
        approval_id: str,
        event: str,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        detail: str = "",
    ) -> None:
        """在调用方事务中追加一条审计事件。"""
        self._connection.execute(
            "INSERT INTO approval_events "
            "(approval_id, occurred_at, event, user_id, chat_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (approval_id, datetime.now(UTC).isoformat(), event, user_id, chat_id, detail),
        )

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        """查询审批快照、执行结果及按时间排列的审计事件。"""
        cursor = self._connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        result = dict(zip((column[0] for column in cursor.description), row, strict=True))
        result["arguments"] = json.loads(result["arguments"])
        events = self._connection.execute(
            "SELECT occurred_at, event, user_id, chat_id, detail FROM approval_events "
            "WHERE approval_id = ? ORDER BY id",
            (approval_id,),
        )
        result["events"] = [
            dict(zip((column[0] for column in events.description), event, strict=True))
            for event in events
        ]
        return result

    def list_approvals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """按申请时间倒序查询最近审批，附带完整审计事件。"""
        if not 1 <= limit <= 1_000:
            raise ValueError("approval query limit must be between 1 and 1000")
        rows = self._connection.execute(
            "SELECT id FROM approvals ORDER BY requested_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        records = [self.get_approval(row[0]) for row in rows]
        return [record for record in records if record is not None]

    def get_channel_session(self, channel: str, chat_id: str) -> str:
        """读取聊天当前的逻辑会话；未切换过时沿用旧会话 ID。"""
        row = self._connection.execute(
            "SELECT session_id FROM channel_sessions WHERE channel = ? AND chat_id = ?",
            (channel, chat_id),
        ).fetchone()
        return str(row[0]) if row else f"{channel}:{chat_id}"

    def new_channel_session(self, channel: str, chat_id: str) -> str:
        """持久化新的逻辑会话 ID，保留旧会话、消息和历史摘要。"""
        session_id = f"{channel}:{chat_id}:{uuid4().hex}"
        with self._connection:
            self._connection.execute(
                "INSERT INTO channel_sessions (channel, chat_id, session_id) VALUES (?, ?, ?) "
                "ON CONFLICT(channel, chat_id) DO UPDATE SET session_id = excluded.session_id",
                (channel, chat_id, session_id),
            )
        return session_id

    def get_or_create(self, session_id: str, workdir: Path) -> SessionSnapshot:
        """读取或创建会话，并将中断后未返回的工具调用标记为失败。"""
        path = workdir.resolve()
        now = datetime.now(UTC).isoformat()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO sessions (id, workdir, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, str(path), now, now),
            )
        row = self._connection.execute(
            "SELECT workdir, input_tokens, output_tokens, cost FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        assert row is not None
        if Path(row[0]) != path:
            raise ValueError("session ID already belongs to another workspace")
        messages = [
            Message.from_dict(json.loads(record[0]))
            for record in self._connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            )
        ]
        pending: dict[str, str] = {}
        for message in messages:
            if message.role == "assistant":
                pending.update({call.id: call.name for call in message.tool_calls})
            elif message.role == "tool" and message.tool_call_id:
                pending.pop(message.tool_call_id, None)
        if pending:
            recovered = [
                ToolResult(
                    call_id, name, "上次执行中断，工具结果未知。请重新检查状态。", True
                ).as_message()
                for call_id, name in pending.items()
            ]
            self.append(session_id, *recovered)
            messages.extend(recovered)
        return SessionSnapshot(session_id, path, tuple(messages), row[1], row[2], row[3])

    def append(self, session_id: str, *messages: Message) -> None:
        """按顺序追加消息并更新会话修改时间。"""
        now = datetime.now(UTC).isoformat()
        with self._connection:
            self._connection.executemany(
                "INSERT INTO messages (session_id, payload) VALUES (?, ?)",
                [
                    (session_id, json.dumps(message.to_dict(), ensure_ascii=False))
                    for message in messages
                ],
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )

    def record_usage(self, session_id: str, usage: Usage, cost: float) -> None:
        """累加会话的输入、输出 token 与估算成本。"""
        with self._connection:
            self._connection.execute(
                "UPDATE sessions SET input_tokens = input_tokens + ?, "
                "output_tokens = output_tokens + ?, cost = cost + ? WHERE id = ?",
                (usage.input_tokens, usage.output_tokens, cost, session_id),
            )

    def load_memory(self, session_id: str) -> tuple[str, int]:
        """读取摘要及其覆盖的历史消息数；兼容尚无摘要的旧会话。"""
        row = self._connection.execute(
            "SELECT summary, message_count FROM session_memory WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return (str(row[0]), int(row[1])) if row else ("", 0)

    def save_memory(self, session_id: str, summary: str, message_count: int) -> None:
        """原子保存摘要检查点，不删除原始会话消息。"""
        with self._connection:
            self._connection.execute(
                "INSERT INTO session_memory (session_id, summary, message_count) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "summary = excluded.summary, message_count = excluded.message_count",
                (session_id, summary, message_count),
            )

    def claim_event(self, channel: str, event_id: str) -> bool:
        """原子登记渠道事件 ID；重复事件返回 False。"""
        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO inbound_events (channel, event_id) VALUES (?, ?)",
                (channel, event_id),
            )
        return cursor.rowcount == 1

    def close(self) -> None:
        """关闭底层 SQLite 连接。"""
        self._connection.close()

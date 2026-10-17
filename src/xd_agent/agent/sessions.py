from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from xd_agent.agent.models import Message, ToolResult, Usage


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
            """
        )

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

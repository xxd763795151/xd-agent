from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

from xd_agent.core.config import LoggingSettings

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
session_id_context: ContextVar[str | None] = ContextVar("session_id", default=None)


class JsonFormatter(logging.Formatter):
    """输出包含关联 ID、会话 ID 和异常信息的结构化日志。"""

    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        """将日志记录序列化为单行 JSON。"""
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "correlation_id": correlation_id.get(),
            "session_id": session_id_context.get(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def configure_logging(settings: LoggingSettings, service: str, environment: str) -> None:
    """配置根日志处理器，并降低可能包含凭据 URL 的 HTTP 日志级别。"""
    handler = logging.StreamHandler(sys.stderr)
    if settings.json_output:
        handler.setFormatter(JsonFormatter(service, environment))
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.level)
    # 部分消息接口将凭据放在查询参数中；httpx 日志会记录完整 URL。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

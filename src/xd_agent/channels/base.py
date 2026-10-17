from __future__ import annotations

from typing import Protocol


class ChannelAdapter(Protocol):
    """消息渠道接口；负责启动、关闭及处理 Webhook 请求。

    handle_webhook 和 verify_webhook 返回 HTTP 状态码与响应内容。
    """

    async def start(self) -> None:
        """初始化渠道连接与回调处理。"""
        ...

    async def stop(self) -> None:
        """停止渠道并释放资源。"""
        ...

    async def handle_webhook(
        self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        """处理 POST 回调并返回 HTTP 响应。"""
        ...

    async def verify_webhook(self, query: dict[str, str]) -> tuple[int, bytes]:
        """处理 GET 回调验证请求并返回 HTTP 响应。"""
        ...

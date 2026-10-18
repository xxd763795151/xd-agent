from typing import Annotated

import typer
import uvicorn

from xd_agent.core.config import load_settings


def serve(
    host: Annotated[str | None, typer.Option("--host", help="覆盖 http.host 监听地址")] = None,
    port: Annotated[
        int | None, typer.Option("--port", min=1, max=65535, help="覆盖 http.port 监听端口")
    ] = None,
) -> None:
    """启动 HTTP 服务及 channel.type 指定的消息渠道，按 Ctrl+C 停止。"""
    settings = load_settings()
    uvicorn.run(
        "xd_agent.web.app:create_app",
        factory=True,
        host=host if host is not None else settings.http.host,
        port=port if port is not None else settings.http.port,
        workers=1,
    )

import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request

from xd_agent.core.logging import correlation_id

logger = logging.getLogger(__name__)


def register_middleware(app: FastAPI) -> None:
    """为每个 HTTP 请求记录关联 ID、响应状态及耗时。"""

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """为单次请求设置关联 ID，并记录响应耗时。"""
        request_id = str(uuid4())
        token = correlation_id.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            route = request.scope.get("route")
            logger.info(
                "request method=%s route=%s status=%s duration_ms=%.1f",
                request.method,
                getattr(route, "path", request.url.path),
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
            return response
        finally:
            correlation_id.reset(token)

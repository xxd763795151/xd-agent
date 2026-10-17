import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from xd_agent.features.greeting.service import ApplicationError, InvalidNameError

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """注册应用错误和未处理异常的统一 JSON 响应。"""

    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        """将已知应用错误映射为带错误码的客户端响应。"""
        status = 400 if isinstance(exc, InvalidNameError) else 422
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """记录未处理异常并返回统一的服务端错误响应。"""
        logger.exception("unhandled HTTP error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal server error"}},
        )

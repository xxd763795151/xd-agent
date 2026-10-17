from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from xd_agent.core.config import load_settings
from xd_agent.core.logging import configure_logging
from xd_agent.core.runtime import build_runtime
from xd_agent.web.exception_handlers import register_exception_handlers
from xd_agent.web.middleware import register_middleware
from xd_agent.web.router import router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """在 Web 应用启动时创建运行时，关闭时释放资源。"""
    settings = load_settings()
    configure_logging(settings.logging, settings.service_name, settings.env)
    runtime = await build_runtime(settings, start_channel=True)
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.aclose()


def create_app() -> FastAPI:
    """创建 FastAPI 应用并注册路由、异常处理及中间件。"""
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    register_exception_handlers(app)
    register_middleware(app)
    return app

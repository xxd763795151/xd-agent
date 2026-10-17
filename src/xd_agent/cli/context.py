import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from xd_agent.core.config import load_settings
from xd_agent.core.logging import configure_logging, correlation_id
from xd_agent.core.runtime import Runtime, build_runtime


async def run_with_runtime_async[T](action: Callable[[Runtime], Awaitable[T]]) -> T:
    """创建请求关联 ID 和运行时，执行异步操作后释放资源。"""
    token = correlation_id.set(str(uuid4()))
    try:
        settings = load_settings()
        configure_logging(settings.logging, settings.service_name, settings.env)
        runtime = await build_runtime(settings)
        try:
            return await action(runtime)
        finally:
            await runtime.aclose()
    finally:
        correlation_id.reset(token)


def run_with_runtime[T](action: Callable[[Runtime], Awaitable[T]]) -> T:
    """在同步 CLI 入口中运行带生命周期管理的异步操作。"""
    return asyncio.run(run_with_runtime_async(action))

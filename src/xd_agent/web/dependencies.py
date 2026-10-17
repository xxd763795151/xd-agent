from typing import Annotated, cast

from fastapi import Depends, Request

from xd_agent.core.runtime import Runtime
from xd_agent.features.greeting.service import GreetingService


def get_runtime(request: Request) -> Runtime:
    """从应用状态中获取启动阶段创建的运行时。"""
    return cast(Runtime, request.app.state.runtime)


def get_greeting_service(runtime: Annotated[Runtime, Depends(get_runtime)]) -> GreetingService:
    """从运行时提供问候服务依赖。"""
    return runtime.greeting

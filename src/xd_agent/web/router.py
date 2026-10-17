from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from xd_agent.core.runtime import Runtime
from xd_agent.features.greeting.models import GreetInput
from xd_agent.features.greeting.service import GreetingService
from xd_agent.web.dependencies import get_greeting_service, get_runtime

router = APIRouter()


class GreetRequest(BaseModel):
    """问候接口的请求体；姓名长度限制为 1 至 200 字符。"""

    name: str = Field(min_length=1, max_length=200)


class GreetResponse(BaseModel):
    """问候接口返回的消息。"""

    message: str


@router.post("/greetings", response_model=GreetResponse)
def greet(
    body: GreetRequest,
    service: Annotated[GreetingService, Depends(get_greeting_service)],
) -> GreetResponse:
    """处理示例问候请求。"""
    result = service.execute(GreetInput(name=body.name))
    return GreetResponse(message=result.message)


@router.get("/health/live")
def live() -> dict[str, str]:
    """返回进程存活状态。"""
    return {"status": "ok"}


@router.get("/health/ready")
def ready(runtime: Annotated[object, Depends(get_runtime)]) -> dict[str, str]:
    """运行时可用时返回就绪状态。"""
    return {"status": "ok"}


@router.post("/webhook/event")
async def channel_webhook(
    request: Request, runtime: Annotated[Runtime, Depends(get_runtime)]
) -> Response:
    """限制回调体大小，并交由当前消息渠道处理 POST 请求。"""
    if runtime.channel is None:
        raise HTTPException(status_code=404, detail="no webhook channel is configured")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(status_code=413, detail="webhook payload too large")
    status, content = await runtime.channel.handle_webhook(
        dict(request.headers), body, dict(request.query_params)
    )
    media_type = "text/plain" if runtime.settings.channel.type == "wecom" else "application/json"
    return Response(status_code=status, content=content, media_type=media_type)


@router.get("/webhook/event")
async def verify_channel_webhook(
    request: Request, runtime: Annotated[Runtime, Depends(get_runtime)]
) -> Response:
    """将 GET 回调验证参数交由当前消息渠道处理。"""
    if runtime.channel is None:
        raise HTTPException(status_code=404, detail="no webhook channel is configured")
    status, content = await runtime.channel.verify_webhook(dict(request.query_params))
    return Response(status_code=status, content=content, media_type="text/plain")

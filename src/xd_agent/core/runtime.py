from dataclasses import dataclass

import httpx

from xd_agent.agent.provider import HttpModelProvider
from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.base import ChannelAdapter
from xd_agent.channels.factory import build_channel
from xd_agent.core.config import Settings, resolve_project_path
from xd_agent.features.greeting.service import GreetingService


@dataclass(slots=True)
class Runtime:
    """聚合服务、会话存储、HTTP 客户端和可选消息渠道。

    greeting 和 agent 是业务入口；settings 为当前配置；sessions 保存历史；
    http_client 与 channel 按运行时生命周期统一释放。
    """

    greeting: GreetingService
    agent: AgentService
    settings: Settings
    http_client: httpx.AsyncClient
    sessions: SessionStore
    channel: ChannelAdapter | None = None

    async def aclose(self) -> None:
        """依次停止渠道并关闭 HTTP 客户端及会话存储。"""
        if self.channel is not None:
            await self.channel.stop()
        await self.http_client.aclose()
        self.sessions.close()


async def build_runtime(settings: Settings, *, start_channel: bool = False) -> Runtime:
    """创建运行时组件，可选启动渠道；初始化失败时释放已创建资源。"""
    client = httpx.AsyncClient()
    sessions = SessionStore(resolve_project_path(settings.agent.session_db))
    channel: ChannelAdapter | None = None
    try:
        provider = (
            HttpModelProvider(settings.agent, client)
            if settings.agent.api_key is not None and settings.agent.api_key.get_secret_value()
            else None
        )
        agent = AgentService(provider, sessions, settings.agent)
        channel = (
            build_channel(settings.channel, agent, sessions, settings.agent)
            if start_channel
            else None
        )
        runtime = Runtime(
            GreetingService(settings.greeting.prefix), agent, settings, client, sessions, channel
        )
        if channel is not None:
            await channel.start()
        return runtime
    except BaseException:
        if channel is not None:
            await channel.stop()
        await client.aclose()
        sessions.close()
        raise

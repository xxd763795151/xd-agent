from __future__ import annotations

from xd_agent.agent.service import AgentService
from xd_agent.agent.sessions import SessionStore
from xd_agent.channels.base import ChannelAdapter
from xd_agent.core.config import AgentSettings, ChannelSettings


def build_channel(
    settings: ChannelSettings,
    agent: AgentService,
    sessions: SessionStore,
    agent_settings: AgentSettings,
) -> ChannelAdapter | None:
    """根据渠道配置构建适配器；CLI 模式返回 None。"""
    if settings.type == "cli":
        return None
    if settings.type == "feishu":
        from xd_agent.channels.feishu import FeishuAdapter

        return FeishuAdapter(agent, sessions, settings.feishu, agent_settings)
    if settings.type == "dingtalk":
        from xd_agent.channels.dingtalk import DingTalkAdapter

        return DingTalkAdapter(agent, sessions, settings.dingtalk, agent_settings)
    if settings.type == "wecom":
        from xd_agent.channels.wecom import WeComAdapter

        return WeComAdapter(agent, sessions, settings.wecom, agent_settings)
    raise RuntimeError(f"channel {settings.type!r} is not implemented")

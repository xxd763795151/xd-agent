from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
_ENVIRONMENTS = {"development", "test", "staging", "production"}
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class HttpSettings(BaseModel):
    """HTTP 服务监听地址和端口配置。"""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class LoggingSettings(BaseModel):
    """日志级别及是否输出 JSON 的配置。"""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_output: bool = True


class GreetingSettings(BaseModel):
    """示例问候语的前缀配置。"""

    prefix: str = Field(default="Hello", min_length=1)


class AgentSettings(BaseModel):
    """模型、工作区、会话及执行预算配置。

    provider、model、api_key、base_url 指定模型服务；workspace_dir 和 session_db
    指定默认工作区及会话数据库。max_turns 限制模型轮数；max_context_chars
    与 memory_messages 限制上下文；command_timeout_seconds 和
    max_tool_output_chars 限制工具执行。价格及 currency 仅用于成本估算。
    """

    provider: Literal["openai_compatible", "anthropic_compatible"] = "openai_compatible"
    model: str = "glm-4.5-air"
    api_key: SecretStr | None = None
    base_url: str | None = None
    workspace_dir: Path = Path("workspace")
    session_db: Path = Path(".xd-agent/sessions.sqlite3")
    max_turns: int = Field(default=30, ge=1, le=500)
    max_context_chars: int = Field(default=120_000, ge=1_000)
    memory_messages: int = Field(default=40, ge=2)
    command_timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_tool_output_chars: int = Field(default=8_000, ge=100)
    input_price_per_million: float = Field(default=0.15, ge=0)
    output_price_per_million: float = Field(default=0.15, ge=0)
    currency: str = "CNY"


class FeishuSettings(BaseModel):
    """飞书凭据、发起人与审批人白名单及审批超时配置。

    app_id、app_secret 为应用凭据；verification_token 和 encrypt_key 用于
    验证及解密回调。allowed_sender_ids 留空时由 allowed_approver_ids
    控制任务发起权限；approval_timeout_seconds 限制审批等待时间。
    """

    app_id: str | None = None
    app_secret: SecretStr | None = None
    verification_token: SecretStr | None = None
    encrypt_key: SecretStr | None = None
    allowed_approver_ids: list[str] = Field(default_factory=list)
    allowed_sender_ids: list[str] = Field(default_factory=list)
    approval_timeout_seconds: int = Field(default=300, ge=10, le=3600)


class DingTalkSettings(BaseModel):
    """钉钉 Stream 凭据、用户白名单及审批超时配置。

    client_id、client_secret 用于建立 Stream 连接。allowed_sender_ids 留空时
    由 allowed_approver_ids 控制任务发起权限；approval_timeout_seconds
    限制审批等待时间。
    """

    client_id: str | None = None
    client_secret: SecretStr | None = None
    allowed_approver_ids: list[str] = Field(default_factory=list)
    allowed_sender_ids: list[str] = Field(default_factory=list)
    approval_timeout_seconds: int = Field(default=300, ge=10, le=3600)


class WeComSettings(BaseModel):
    """企业微信应用凭据、回调密钥及用户权限配置。

    corp_id、corp_secret、agent_id 标识应用；token 和 encoding_aes_key
    用于验证及解密回调。allowed_sender_ids 留空时由 allowed_approver_ids
    控制任务发起权限；approval_timeout_seconds 限制审批等待时间。
    """

    corp_id: str | None = None
    corp_secret: SecretStr | None = None
    agent_id: int | None = Field(default=None, ge=1)
    token: SecretStr | None = None
    encoding_aes_key: SecretStr | None = None
    allowed_approver_ids: list[str] = Field(default_factory=list)
    allowed_sender_ids: list[str] = Field(default_factory=list)
    approval_timeout_seconds: int = Field(default=300, ge=10, le=3600)


class ChannelSettings(BaseModel):
    """渠道类型及各渠道专属配置；仅选中的渠道需要提供必需凭据。"""

    type: Literal["cli", "feishu", "dingtalk", "wecom"] = "cli"
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)
    dingtalk: DingTalkSettings = Field(default_factory=DingTalkSettings)
    wecom: WeComSettings = Field(default_factory=WeComSettings)


def selected_environment() -> Environment:
    """从进程环境变量读取并校验 APP_ENV。"""
    raw = os.getenv("APP_ENV", "development").strip().lower()
    if raw not in _ENVIRONMENTS:
        raise RuntimeError(f"unsupported APP_ENV: {raw!r}")
    return raw  # type: ignore[return-value]


def resolve_project_path(path: Path) -> Path:
    """将相对路径按项目根目录展开为绝对路径。"""
    return (path if path.is_absolute() else _PROJECT_ROOT / path).expanduser().resolve()


def resolve_config_files() -> tuple[Path, Path]:
    """定位基础与当前环境配置文件，并校验部署环境路径要求。"""
    environment = selected_environment()
    configured = os.getenv("APP_CONFIG_DIR")
    if environment in ("staging", "production") and not configured:
        raise RuntimeError("APP_CONFIG_DIR is required for staging and production")
    directory = Path(configured).expanduser() if configured else _PROJECT_ROOT / "config"
    if environment in ("staging", "production") and not directory.is_absolute():
        raise RuntimeError("APP_CONFIG_DIR must be absolute for staging and production")
    if not directory.is_absolute():
        directory = _PROJECT_ROOT / directory
    files = (directory.resolve() / "base.yaml", directory.resolve() / f"{environment}.yaml")
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise RuntimeError("missing configuration file: " + ", ".join(missing))
    return files


def _merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """递归合并嵌套配置字典，环境值覆盖基础值。"""
    result = base.copy()
    for key, value in override.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            result[key] = _merge(previous, value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, object]:
    """读取 YAML 配置，要求顶层为字符串键映射。"""
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise RuntimeError(f"configuration must be a string-keyed mapping: {path}")
    return data


class MergedYamlSettingsSource(PydanticBaseSettingsSource):
    """将基础 YAML 与环境 YAML 合并为 Pydantic 配置来源。"""

    def get_field_value(self, field: object, field_name: str) -> tuple[None, str, bool]:
        """由整体配置读取完成字段解析，单字段读取不提供值。"""
        return None, field_name, False

    def __call__(self) -> dict[str, object]:
        """返回当前环境覆盖基础配置后的字典。"""
        base, environment = resolve_config_files()
        return _merge(_read_yaml(base), _read_yaml(environment))


class Settings(BaseSettings):
    """应用总配置；初始化参数、环境变量、.env 和 YAML 依次决定优先级。"""

    model_config = SettingsConfigDict(env_prefix="APP_", env_nested_delimiter="__", extra="ignore")

    env: Environment = "development"
    service_name: str = "xd-agent"
    http: HttpSettings = Field(default_factory=HttpSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    greeting: GreetingSettings = Field(default_factory=GreetingSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    channel: ChannelSettings = Field(default_factory=ChannelSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """指定配置来源顺序，使环境变量优先于 YAML。"""
        return init_settings, env_settings, dotenv_settings, MergedYamlSettingsSource(settings_cls)


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """加载并缓存配置，仅在开发与测试环境读取项目 .env。"""
    environment = selected_environment()
    dotenv = _PROJECT_ROOT / ".env"
    env_file = dotenv if environment in ("development", "test") and dotenv.is_file() else None
    settings = Settings(_env_file=env_file)
    if settings.env != environment:
        raise RuntimeError("APP_ENV may only be set by process environment or CLI bootstrap option")
    return settings

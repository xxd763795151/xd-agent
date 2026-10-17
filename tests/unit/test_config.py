from pathlib import Path

import pytest
from pydantic import SecretStr

from xd_agent.core.config import load_settings, resolve_config_files


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


def test_environment_overrides_merged_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "base.yaml").write_text(
        "service_name: sample\nhttp:\n  host: 127.0.0.1\n  port: 8000\n",
        encoding="utf-8",
    )
    (tmp_path / "test.yaml").write_text("http:\n  port: 9000\n", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("APP_HTTP__PORT", "9100")

    settings = load_settings()
    assert settings.service_name == "sample"
    assert settings.http.host == "127.0.0.1"
    assert settings.http.port == 9100


def test_production_requires_absolute_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_CONFIG_DIR", "relative/config")
    with pytest.raises(RuntimeError, match="must be absolute"):
        resolve_config_files()


def test_cli_is_default_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("APP_CHANNEL__TYPE", raising=False)
    assert load_settings().channel.type == "cli"


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom"])
def test_channel_properties_load_from_yaml_and_environment(
    channel: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "base.yaml").write_text(
        "channel:\n"
        f"  type: {channel}\n"
        "  feishu:\n"
        "    app_id: cli_yaml\n"
        "    app_secret: yaml-secret\n"
        "  dingtalk:\n"
        "    client_id: ding_yaml\n"
        "    client_secret: yaml-secret\n"
        "  wecom:\n"
        "    corp_id: corp_yaml\n"
        "    corp_secret: yaml-secret\n"
        "    agent_id: 1000001\n"
        "    token: yaml-token\n"
        "    encoding_aes_key: yaml-aes-key\n",
        encoding="utf-8",
    )
    (tmp_path / "test.yaml").write_text(
        "channel:\n"
        "  dingtalk:\n"
        "    allowed_approver_ids: [admin]\n"
        "  wecom:\n"
        "    allowed_sender_ids: [requester]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("APP_CHANNEL__DINGTALK__CLIENT_SECRET", "env-secret")

    settings = load_settings().channel
    assert settings.type == channel
    assert settings.feishu.app_id == "cli_yaml"
    assert settings.dingtalk.client_id == "ding_yaml"
    assert settings.dingtalk.client_secret == SecretStr("env-secret")
    assert settings.dingtalk.allowed_approver_ids == ["admin"]
    assert settings.wecom.corp_id == "corp_yaml"
    assert settings.wecom.agent_id == 1000001
    assert settings.wecom.allowed_sender_ids == ["requester"]

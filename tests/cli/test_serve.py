import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from xd_agent.cli.app import app
from xd_agent.core.config import load_settings

serve_module = importlib.import_module("xd_agent.cli.commands.serve")


@pytest.fixture(autouse=True)
def serve_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("xd_agent.core.config._PROJECT_ROOT", tmp_path)
    (tmp_path / "base.yaml").write_text(
        "http:\n  host: 127.0.0.2\n  port: 8100\n", encoding="utf-8"
    )
    (tmp_path / "test.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("APP_HTTP__HOST", raising=False)
    monkeypatch.delenv("APP_HTTP__PORT", raising=False)
    load_settings.cache_clear()
    yield
    load_settings.cache_clear()


@pytest.mark.parametrize(
    ("environment", "options", "host", "port"),
    [
        ({}, [], "127.0.0.2", 8100),
        ({"APP_HTTP__HOST": "127.0.0.3", "APP_HTTP__PORT": "8200"}, [], "127.0.0.3", 8200),
        (
            {"APP_HTTP__HOST": "127.0.0.3", "APP_HTTP__PORT": "8200"},
            ["--host", "0.0.0.0", "--port", "8300"],
            "0.0.0.0",
            8300,
        ),
    ],
)
def test_serve_listen_configuration(monkeypatch, environment, options, host, port) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    run = Mock()
    monkeypatch.setattr(serve_module.uvicorn, "run", run)
    result = CliRunner().invoke(app, ["serve", *options])
    assert result.exit_code == 0, result.output
    run.assert_called_once_with(
        "xd_agent.web.app:create_app", factory=True, host=host, port=port, workers=1
    )


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_serve_rejects_invalid_port(monkeypatch, port) -> None:
    run = Mock()
    monkeypatch.setattr(serve_module.uvicorn, "run", run)
    result = CliRunner().invoke(app, ["serve", "--port", port])
    assert result.exit_code == 2
    run.assert_not_called()


def test_serve_global_environment_options(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "production.yaml").write_text("http:\n  port: 8400\n", encoding="utf-8")
    run = Mock()
    monkeypatch.setattr(serve_module.uvicorn, "run", run)
    result = CliRunner().invoke(
        app, ["--env", "production", "--config-dir", str(tmp_path), "serve"]
    )
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["port"] == 8400


def test_serve_starts_and_stops_configured_channel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APP_CHANNEL__TYPE", "dingtalk")
    monkeypatch.setenv("APP_AGENT__SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    events = []

    class FakeChannel:
        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    def build_channel(settings, agent, sessions, agent_settings):
        assert settings.type == "dingtalk"
        return FakeChannel()

    def run(application, **kwargs):
        module_name, factory_name = application.split(":")
        factory = getattr(importlib.import_module(module_name), factory_name)
        with TestClient(factory()) as client:
            assert events == ["start"]
            assert client.get("/health/ready").json() == {"status": "ok"}
        assert events == ["start", "stop"]

    monkeypatch.setattr("xd_agent.core.runtime.build_channel", build_channel)
    monkeypatch.setattr(serve_module.uvicorn, "run", run)
    # Avoid retaining a logging handler that refers to CliRunner's closed stream.
    monkeypatch.setattr("xd_agent.web.app.configure_logging", lambda *args: None)
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 0, result.output

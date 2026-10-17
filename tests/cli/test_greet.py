import json

from typer.testing import CliRunner

from xd_agent.cli.app import app
from xd_agent.core.config import load_settings

runner = CliRunner()


def test_cli_json_contract(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    load_settings.cache_clear()
    result = runner.invoke(app, ["greet", "Ada", "--output", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"message": "Hello, Ada!"}
    load_settings.cache_clear()


def test_cli_application_error_exit_code(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    load_settings.cache_clear()
    result = runner.invoke(app, ["greet", " "])
    assert result.exit_code == 2
    assert "invalid_name" in result.output
    if result.stderr_bytes is not None:
        assert result.stdout == ""
    load_settings.cache_clear()

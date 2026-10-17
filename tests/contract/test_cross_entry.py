import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from xd_agent.cli.app import app
from xd_agent.core.config import load_settings
from xd_agent.web.app import create_app


def test_same_result_semantics_across_entries(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    load_settings.cache_clear()
    with TestClient(create_app()) as client:
        http_result = client.post("/greetings", json={"name": "Ada"})
    cli_result = CliRunner().invoke(app, ["greet", "Ada", "--output", "json"])
    assert http_result.status_code == 200
    assert cli_result.exit_code == 0
    assert http_result.json()["message"] == json.loads(cli_result.stdout)["message"]
    load_settings.cache_clear()

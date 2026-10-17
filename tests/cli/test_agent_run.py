from pathlib import Path

from typer.testing import CliRunner

from xd_agent.agent.provider import HttpModelProvider
from xd_agent.cli.app import app
from xd_agent.core.config import load_settings


def test_cli_run_executes_agent_with_configured_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_AGENT__API_KEY", "test-key")
    monkeypatch.setenv("APP_AGENT__WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("APP_AGENT__SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    load_settings.cache_clear()
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "write-1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":"hello.txt","content":"hello"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "任务完成。"}}]},
        ]
    )

    async def fake_post(self, url, headers, payload):
        return next(responses)

    monkeypatch.setattr(HttpModelProvider, "_post", fake_post)
    result = CliRunner().invoke(app, ["run", "创建 hello.txt", "--session", "cli-test"])
    assert result.exit_code == 0, result.output
    assert "任务完成" in result.output
    assert (tmp_path / "workspace" / "hello.txt").read_text(encoding="utf-8") == "hello"
    load_settings.cache_clear()

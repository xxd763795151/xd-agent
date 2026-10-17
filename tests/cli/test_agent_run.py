import logging
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from xd_agent.agent.provider import HttpModelProvider
from xd_agent.cli.app import app
from xd_agent.core.config import load_settings


@pytest.fixture(autouse=True)
def isolate_logging():
    """避免 CLI 日志处理器引用上一次 CliRunner 已关闭的输出流。"""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers = []
    root.setLevel(logging.WARNING)
    try:
        yield
    finally:
        root.handlers = handlers
        root.setLevel(level)


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


@pytest.mark.parametrize("command", ["run", "chat"])
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_cli_provider_error_logs_once_with_optional_traceback(
    tmp_path: Path, monkeypatch, command, debug, json_output
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_AGENT__API_KEY", "test-key")
    monkeypatch.setenv("APP_AGENT__SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    # 开发环境默认 DEBUG 级别也不应打印预期错误的堆栈。
    monkeypatch.setenv("APP_LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("APP_LOGGING__JSON_OUTPUT", str(json_output).lower())

    async def fake_post(self, url, **kwargs):
        return httpx.Response(
            429,
            request=httpx.Request("POST", url),
            json={"error": {"code": "1302", "message": "private response body"}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    args = [command, "--dir", str(tmp_path)]
    if command == "run":
        args.append("分析这个项目的目录结构")
    if debug:
        args.append("--debug")
    try:
        result = CliRunner().invoke(app, args, input="分析这个项目的目录结构\n")
        assert result.exit_code == 5, result.output
        assert result.output.count("agent_error:") == 1
        assert "HTTP 429, code=1302" in result.output
        assert "请求受到限制" in result.output
        assert ("Traceback (most recent call last)" in result.output) is debug
        assert "private response body" not in result.output
        assert "test-key" not in result.output
    finally:
        load_settings.cache_clear()

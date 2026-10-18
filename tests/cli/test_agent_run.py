import json
import logging
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from xd_agent.agent.models import Message, ModelReply, ToolCall
from xd_agent.agent.provider import HttpModelProvider
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import RunCommand
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


@pytest.mark.parametrize("command", ["run", "chat"])
@pytest.mark.parametrize(
    ("answer", "auto_approve", "written", "status"),
    [
        ("y\n", False, True, "approved"),
        ("n\n", False, False, "rejected"),
        ("\n", False, False, "rejected"),
        ("", False, False, "cancelled"),
        ("", True, True, "approved"),
    ],
)
def test_cli_run_and_chat_require_approval_and_record_selected_policy(
    tmp_path: Path, monkeypatch, command, answer, auto_approve, written, status
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
    args = [command, "--session", "cli-test"]
    if command == "run":
        args.append("创建 hello.txt")
        input_text = answer
    else:
        input_text = "创建 hello.txt\n" + answer
        if answer or auto_approve:
            input_text += "/exit\n"
    if auto_approve:
        args.append("--auto-approve")
    result = CliRunner().invoke(app, args, input=input_text)
    assert result.exit_code == 0, result.output
    assert "任务完成" in result.output
    path = tmp_path / "workspace" / "hello.txt"
    assert path.exists() is written
    if written:
        assert path.read_text(encoding="utf-8") == "hello"
    assert ("允许执行以上工具调用吗" in result.output) is (not auto_approve)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    try:
        record = store.list_approvals()[0]
        assert record["status"] == status
        assert record["policy"] == ("LocalApproval" if auto_approve else "TerminalApproval")
        assert record["execution_status"] == ("succeeded" if written else "not_started")
    finally:
        store.close()
    load_settings.cache_clear()


@pytest.mark.parametrize("entrypoint", ["run", "chat"])
@pytest.mark.parametrize(
    ("shell_command", "enabled", "analysis", "answer", "executed", "asked"),
    [
        ("lsblk", True, "valid", "", True, False),
        ("df -h", False, "valid", "n\n", False, True),
        ("df -h", False, "valid", "y\n", True, True),
        ("rm example", True, "unused", "n\n", False, True),
        ("lsblk", True, "invalid", "n\n", False, True),
        ("lsblk", True, "error", "n\n", False, True),
    ],
)
def test_cli_command_analysis_and_read_only_switch(
    tmp_path, monkeypatch, entrypoint, shell_command, enabled, analysis, answer, executed, asked
):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_AGENT__API_KEY", "test-key")
    monkeypatch.setenv("APP_AGENT__WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("APP_AGENT__SESSION_DB", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("APP_AGENT__AUTO_APPROVE_READ_ONLY_COMMANDS", str(enabled).lower())
    load_settings.cache_clear()
    calls = []
    analyses = []
    executed_commands = []

    async def generate(self, messages, tools):
        if not tools:
            analyses.append(messages)
            if analysis == "error":
                raise RuntimeError("test model unavailable")
            content = (
                "invalid"
                if analysis == "invalid"
                else json.dumps(
                    {
                        "read_only": True,
                        "purpose": "查询分区信息",
                        "impact": "不修改系统分区",
                    }
                )
            )
            return ModelReply(Message("assistant", content))
        calls.append(messages)
        if len(calls) == 1:
            return ModelReply(
                Message(
                    "assistant",
                    tool_calls=(
                        ToolCall(
                            "cmd",
                            "run_command",
                            {
                                "command": shell_command,
                            },
                        ),
                    ),
                )
            )
        return ModelReply(Message("assistant", "任务完成"))

    async def execute(self, arguments):
        executed_commands.append(arguments["command"])
        return "sda1"

    monkeypatch.setattr(HttpModelProvider, "generate", generate)
    monkeypatch.setattr(RunCommand, "execute", execute)
    args = [entrypoint, "--session", "command-test"]
    if entrypoint == "run":
        args.append("查看分区")
        input_text = answer
    else:
        input_text = "查看分区\n" + answer + "/exit\n"
    try:
        result = CliRunner().invoke(app, args, input=input_text)
        assert result.exit_code == 0, result.output
        assert "任务完成" in result.output
        assert bool(executed_commands) is executed
        assert bool(analyses) is (analysis != "unused")
        assert ("允许执行以上工具调用吗" in result.output) is asked
        assert "作用：" in result.output and "影响：" in result.output
        if analysis == "unused":
            assert "固定高危规则" in result.output
        if analysis in {"invalid", "error"}:
            assert "保守回退" in result.output
        store = SessionStore(tmp_path / "sessions.sqlite3")
        try:
            record = store.list_approvals()[0]
            assert record["status"] == ("approved" if executed else "rejected")
            assert record["decided_by"] == ("local" if asked else "command-review")
        finally:
            store.close()
    finally:
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

import asyncio
import json
from pathlib import Path

import pytest

from xd_agent.agent.approval import ApprovalDecision
from xd_agent.agent.command_review import CommandReviewer
from xd_agent.agent.models import Message, ModelReply, ToolCall, Usage
from xd_agent.agent.sessions import SessionStore
from xd_agent.agent.tools import ToolRegistry
from xd_agent.core.config import AgentSettings


class Provider:
    def __init__(self, read_only=True, *, content=None, error=None):
        self.content = (
            content
            if content is not None
            else json.dumps(
                {"read_only": read_only, "purpose": "查询分区信息", "impact": "不修改系统分区"}
            )
        )
        self.calls = []
        self.error = error

    async def generate(self, messages, tools):
        self.calls.append((messages, tools))
        if self.error:
            raise self.error
        return ModelReply(Message("assistant", self.content), Usage(100, 20))


class CommandTool:
    name = "run_command"
    description = "test command"
    parameters = {"required": ["command"]}

    def __init__(self):
        self.calls = []

    async def execute(self, arguments):
        self.calls.append(arguments)
        return "partition info"


class Gate:
    def __init__(self, allowed=False):
        self.requests = []
        self.allowed = allowed

    async def authorize(self, request, *, reporter):
        self.requests.append(request)
        return ApprovalDecision(
            self.allowed, "approved" if self.allowed else "rejected", "人工决定", "admin"
        )


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/data",
        "/bin/rm /tmp/data",
        "lsblk; rm /tmp/data",
        "df -h && rmdir empty",
        "echo 'rm -rf data'",
        'r""m data',
        r"r\m data",
        "git reset --hard",
        "git -C repo clean -fd",
        "git\nreset --hard",
        "sudo lsblk",
        "mkfs.ext4 /dev/sdb",
        "dd if=image of=/dev/sdb",
        "wipefs -a disk",
        "chmod 777 data",
        "chown user file",
        "mount /dev/sdb /mnt",
        "cp a b",
        "mv a b",
        "reboot",
        "kill -9 1234",
        "systemctl restart nginx",
        "crontab -r",
        "apt install package",
        "pip install package",
        "uv run script.py",
        "npm install",
        "curl https://example.com",
        "wget https://example.com",
        "ssh host uptime",
        "docker rm container",
        "kubectl delete pod test",
        "terraform apply",
        'python -c "print(1)"',
        'bash -c "lsblk"',
        "node script.js",
        ". script.sh",
        "find . -delete",
        "find . -exec action {} ;",
        "sed -i s/a/b/ file",
        "echo x > file",
        "lsblk >> file",
        "cat < file",
        "echo $(action)",
        "echo `action`",
        "Remove-Item -Recurse data",
        "REMOVE-ITEM data",
        "cmd.exe /c del file",
        "powershell -EncodedCommand abc",
        "Set-Content file data",
        "Stop-Process -Id 123",
        "Format-Volume -DriveLetter Z",
        "Clear-Disk -Number 3",
        "netsh advfirewall reset",
        "Invoke-WebRequest https://example.com",
        "Invoke-Expression command",
        "awk 'BEGIN {system(\"action\")}'",
        "tar xf archive.tar",
        "sqlite3 data.db 'DROP TABLE notes;'",
        "make target",
        "Clear-Content file",
        "git -C repo\nreset --hard",
    ],
)
def test_fixed_rules_require_human_without_calling_model(command):
    provider = Provider()
    assessment = asyncio.run(CommandReviewer(provider, AgentSettings()).assess(command))
    assert not assessment.read_only
    assert assessment.source == "固定高危规则"
    assert assessment.purpose and assessment.impact
    assert not provider.calls


@pytest.mark.parametrize("command", ["lsblk", "df -h", "lsblk | head -n 3", "Get-Partition"])
def test_read_only_commands_get_isolated_model_analysis(command):
    provider = Provider()
    assessment = asyncio.run(CommandReviewer(provider, AgentSettings()).assess(command))
    assert assessment.read_only
    messages, tools = provider.calls[0]
    assert tools == []
    assert [message.role for message in messages] == ["system", "user"]
    assert json.loads(messages[1].content)["command"] == command
    assert "作用：查询分区信息" in assessment.describe()
    assert "影响：不修改系统分区" in assessment.describe()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not JSON",
        "{}",
        "[]",
        "null",
        '{"read_only":"true","purpose":"test","impact":"test"}',
        '{"read_only":1,"purpose":"test","impact":"test"}',
        '{"read_only":true,"purpose":"","impact":"test"}',
        '{"read_only":true,"purpose":"  ","impact":"test"}',
        '{"read_only":true,"purpose":"test","impact":"test","extra":true}',
    ],
)
def test_invalid_response_never_auto_approves(content):
    assessment = asyncio.run(
        CommandReviewer(Provider(content=content), AgentSettings()).assess("lsblk")
    )
    assert not assessment.read_only
    assert assessment.source == "保守回退"


def test_custom_fragments_add_to_builtin_rules():
    provider = Provider()
    reviewer = CommandReviewer(
        provider, AgentSettings(command_approval_required_fragments=["sensitive-tool"])
    )
    for command in ("Sensitive-Tool --inspect", "rm file"):
        assert not asyncio.run(reviewer.assess(command)).read_only
    assert not provider.calls


def test_model_review_deadline_cancels_request_and_requires_human():
    class WaitingProvider(Provider):
        cancelled = False

        async def generate(self, messages, tools):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    provider = WaitingProvider()
    assessment = asyncio.run(
        CommandReviewer(provider, AgentSettings(command_review_timeout_seconds=1)).assess("lsblk")
    )
    assert provider.cancelled
    assert not assessment.read_only
    assert assessment.source == "保守回退"


@pytest.mark.parametrize("mode", ["exception", "timeout", "oversized", "tool_call", "wrong_role"])
def test_review_failure_and_budget_fall_back_to_human(mode):
    class FailedProvider(Provider):
        async def generate(self, messages, tools):
            if mode == "timeout":
                raise TimeoutError
            if mode == "tool_call":
                return ModelReply(Message("assistant", self.content, (ToolCall("id", "x", {}),)))
            if mode == "wrong_role":
                return ModelReply(Message("user", self.content))
            return await super().generate(messages, tools)

    provider = FailedProvider(error=RuntimeError("private server details"))
    command = "a" * 501 if mode == "oversized" else "lsblk"
    assessment = asyncio.run(
        CommandReviewer(provider, AgentSettings(max_context_chars=1000)).assess(command)
    )
    assert not assessment.read_only
    assert assessment.source == "保守回退"
    assert "private" not in assessment.describe()
    if mode == "oversized":
        assert not provider.calls


@pytest.mark.parametrize(
    ("command", "model_read_only", "enabled", "human_allowed", "executed", "asked"),
    [
        ("lsblk", True, True, False, True, False),
        ("lsblk", True, False, False, False, True),
        ("lsblk", True, False, True, True, True),
        ("custom-tool", False, True, False, False, True),
        ("custom-tool", False, True, True, True, True),
        ("rm file", True, True, False, False, True),
        ("rm file", True, False, False, False, True),
    ],
)
def test_registry_approval_execution_and_analysis_audit(
    tmp_path: Path, command, model_read_only, enabled, human_allowed, executed, asked
):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.get_or_create("session", tmp_path)
    try:
        provider = Provider(read_only=model_read_only)
        gate = Gate(allowed=human_allowed)
        tool = CommandTool()
        reviewer = CommandReviewer(provider, AgentSettings(), store=store, session_id="session")
        registry = ToolRegistry(
            [tool],
            approval=gate,
            command_reviewer=reviewer,
            auto_approve_read_only_commands=enabled,
            store=store,
            session_id="session",
        )
        result = asyncio.run(
            registry.execute(ToolCall("call", "run_command", {"command": command}))
        )
        assert result.is_error is (not executed)
        assert bool(tool.calls) is executed
        assert bool(gate.requests) is asked
        record = store.list_approvals()[0]
        assert record["status"] == ("approved" if executed else "rejected")
        assert record["execution_status"] == ("succeeded" if executed else "not_started")
        assessment_event = next(e for e in record["events"] if e["event"] == "command_assessed")
        assert "作用：" in assessment_event["detail"]
        assert "影响：" in assessment_event["detail"]
        if asked:
            assert gate.requests[0].description == assessment_event["detail"]
        else:
            assert record["decided_by"] == "command-review"
        usage = store.get_or_create("session", tmp_path)
        assert usage.input_tokens == (100 if provider.calls else 0)
        assert usage.output_tokens == (20 if provider.calls else 0)
        if provider.calls:
            assert usage.cost > 0
    finally:
        store.close()


def test_missing_approval_policy_still_denies_read_only_model_result():
    tool = CommandTool()
    registry = ToolRegistry([tool], command_reviewer=CommandReviewer(Provider(), AgentSettings()))
    result = asyncio.run(registry.execute(ToolCall("call", "run_command", {"command": "lsblk"})))
    assert result.is_error
    assert not tool.calls


def test_oversized_arguments_never_reach_model_or_execute():
    provider = Provider()
    tool = CommandTool()
    gate = Gate(allowed=True)
    registry = ToolRegistry(
        [tool], approval=gate, command_reviewer=CommandReviewer(provider, AgentSettings())
    )
    result = asyncio.run(
        registry.execute(ToolCall("call", "run_command", {"command": "a" * 64_001}))
    )
    assert result.is_error and "64KB" in result.output
    assert not provider.calls and not gate.requests and not tool.calls


def test_cancellation_during_analysis_never_executes(tmp_path):
    class WaitingProvider(Provider):
        async def generate(self, messages, tools):
            await asyncio.Future()

    async def scenario():
        store = SessionStore(tmp_path / "sessions.sqlite3")
        try:
            tool = CommandTool()
            gate = Gate(allowed=True)
            registry = ToolRegistry(
                [tool],
                approval=gate,
                store=store,
                command_reviewer=CommandReviewer(WaitingProvider(), AgentSettings()),
            )
            task = asyncio.create_task(
                registry.execute(ToolCall("call", "run_command", {"command": "lsblk"}))
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not tool.calls and not gate.requests
            assert store.list_approvals()[0]["status"] == "cancelled"
        finally:
            store.close()

    asyncio.run(scenario())

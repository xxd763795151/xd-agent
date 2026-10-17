from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from xd_agent.agent.approval import LocalApproval
from xd_agent.agent.reporting import NullReporter
from xd_agent.agent.service import AgentService


@dataclass(slots=True, frozen=True)
class BenchmarkCase:
    """单个评测用例：准备文件、任务提示词和预期文件内容。"""

    id: str
    prompt: str
    files: dict[str, str]
    expected_contains: dict[str, str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkCase:
        """从 JSON 对象构造评测用例。"""
        return cls(
            id=str(data["id"]),
            prompt=str(data["prompt"]),
            files=dict(data.get("files") or {}),
            expected_contains=dict(data["expected_contains"]),
        )


@dataclass(slots=True, frozen=True)
class BenchmarkResult:
    """单个评测用例的通过状态、耗时及错误信息。"""

    id: str
    passed: bool
    duration_ms: float
    error: str = ""


def load_cases(path: Path) -> list[BenchmarkCase]:
    """读取非空 JSON 数组形式的评测用例。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("benchmark file must contain a nonempty JSON array")
    return [BenchmarkCase.from_dict(item) for item in raw]


async def run_benchmark(service: AgentService, cases: list[BenchmarkCase]) -> list[BenchmarkResult]:
    """逐项在临时工作区运行 Agent，并检查预期文件内容。"""
    results: list[BenchmarkResult] = []
    for case in cases:
        started = time.perf_counter()
        error = ""
        with tempfile.TemporaryDirectory(prefix="xd-agent-bench-") as temporary:
            root = Path(temporary)
            for name, content in case.files.items():
                path = (root / name).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("benchmark fixture leaves temporary workspace")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            try:
                await service.run(
                    case.prompt,
                    session_id=f"bench:{case.id}:{uuid4().hex}",
                    workdir=root,
                    reporter=NullReporter(),
                    approval=LocalApproval(),
                )
                for name, expected in case.expected_contains.items():
                    path = (root / name).resolve()
                    if not path.is_relative_to(root) or not path.is_file():
                        raise AssertionError(f"expected file missing: {name}")
                    if expected not in path.read_text(encoding="utf-8"):
                        raise AssertionError(f"expected text missing from {name}")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        results.append(
            BenchmarkResult(
                case.id,
                not error,
                round((time.perf_counter() - started) * 1000, 1),
                error,
            )
        )
    return results

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from xd_agent.agent.benchmark import load_cases, run_benchmark
from xd_agent.cli.context import run_with_runtime
from xd_agent.cli.errors import ExitCode
from xd_agent.core.runtime import Runtime


def benchmark(
    cases_file: Annotated[Path, typer.Argument(help="JSON 评测用例文件")],
) -> None:
    """运行 JSON 评测用例并输出各用例的结果。"""

    async def action(runtime: Runtime) -> list[dict[str, object]]:
        """载入用例并将评测结果转换为可输出的字典。"""
        cases = load_cases(cases_file)
        results = await run_benchmark(runtime.agent, cases)
        return [asdict(result) for result in results]

    try:
        results = run_with_runtime(action)
    except Exception as exc:
        typer.echo(f"benchmark_error: {exc}", err=True)
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from exc
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))
    if not all(result["passed"] for result in results):
        raise typer.Exit(1)

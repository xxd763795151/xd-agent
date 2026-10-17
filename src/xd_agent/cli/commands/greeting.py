import logging
from typing import Annotated

import typer

from xd_agent.cli.context import run_with_runtime
from xd_agent.cli.errors import ExitCode, exit_code_for
from xd_agent.cli.output import OutputFormat, render_greeting
from xd_agent.core.runtime import Runtime
from xd_agent.features.greeting.models import GreetInput, GreetResult
from xd_agent.features.greeting.service import ApplicationError

logger = logging.getLogger(__name__)


def greet(
    name: Annotated[str, typer.Argument(help="Name to greet")],
    output: Annotated[
        OutputFormat, typer.Option("--output", help="Output format")
    ] = OutputFormat.TEXT,
) -> None:
    """执行示例问候命令并按所选格式输出结果。"""

    async def action(runtime: Runtime) -> GreetResult:
        """调用运行时中的问候服务。"""
        return runtime.greeting.execute(GreetInput(name=name))

    try:
        result = run_with_runtime(action)
    except ApplicationError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(exit_code_for(exc)) from exc
    except KeyboardInterrupt as exc:
        typer.echo("interrupted", err=True)
        raise typer.Exit(ExitCode.INTERRUPTED) from exc
    except Exception as exc:
        logger.exception("unhandled CLI error")
        typer.echo("internal_error: command failed", err=True)
        raise typer.Exit(ExitCode.INTERNAL_ERROR) from exc
    render_greeting(result, output)

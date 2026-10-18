import os
from pathlib import Path
from typing import Annotated

import typer

from xd_agent.cli.commands.agent import chat, run
from xd_agent.cli.commands.benchmark import benchmark
from xd_agent.cli.commands.greeting import greet
from xd_agent.cli.commands.serve import serve
from xd_agent.core.config import load_settings

app = typer.Typer(no_args_is_help=True, help="xd-agent command-line interface")


@app.callback()
def bootstrap(
    env: Annotated[str | None, typer.Option("--env", help="Configuration environment")] = None,
    config_dir: Annotated[
        Path | None, typer.Option("--config-dir", help="Configuration directory")
    ] = None,
) -> None:
    """应用命令行环境选项，并清除已缓存的配置。"""
    if env is not None:
        os.environ["APP_ENV"] = env
    if config_dir is not None:
        os.environ["APP_CONFIG_DIR"] = str(config_dir)
    load_settings.cache_clear()


app.command("greet")(greet)
app.command("run")(run)
app.command("chat")(chat)
app.command("benchmark")(benchmark)
app.command("serve")(serve)


def main() -> None:
    """启动 Typer 命令行应用。"""
    app()

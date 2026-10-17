import json
from enum import StrEnum

import typer

from xd_agent.features.greeting.models import GreetResult


class OutputFormat(StrEnum):
    """问候命令支持的文本或 JSON 输出格式。"""

    TEXT = "text"
    JSON = "json"


def render_greeting(result: GreetResult, output: OutputFormat) -> None:
    """按指定格式输出问候结果。"""
    if output is OutputFormat.JSON:
        typer.echo(json.dumps({"message": result.message}, ensure_ascii=False))
    else:
        typer.echo(result.message)

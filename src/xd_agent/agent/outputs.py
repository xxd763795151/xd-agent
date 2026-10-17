from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4


def page_arguments(arguments: dict[str, Any], maximum: int) -> tuple[int, int]:
    """分页偏移以 Unicode 字符计数，避免把 UTF-8 字节偏移误当字符。"""
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", maximum)
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return offset, limit


def read_page(path: Path, offset: int, limit: int, notice: Callable[[int], str]) -> str:
    """有界读取一页；续读提示也计入本页预算，且偏移不跳过内容。"""
    with path.open(encoding="utf-8", newline="") as stream:
        remaining = offset
        while remaining:
            skipped = stream.read(min(remaining, 8192))
            if not skipped:
                return ""
            remaining -= len(skipped)
        content = stream.read(limit + 1)
    if len(content) <= limit:
        return content
    # 使用最大可能偏移计算提示空间，确保提示总长度不超过预算。
    reserved = len(notice(offset + limit))
    count = limit - reserved
    if count < 1:
        raise ValueError("limit is too small for the continuation notice")
    return content[:count] + notice(offset + count)


class ToolOutputStore:
    """将完整工具输出存入工作区，仅向模型返回有预算限制的预览。"""

    def __init__(self, root: Path, limit: int = 8000) -> None:
        self.root = root.resolve()
        self.limit = limit

    def path(self, output_id: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{32}", output_id) is None:
            raise ValueError("invalid output_id")
        path = (self.root / ".xd-agent" / "tool-outputs" / f"{output_id}.txt").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("output archive leaves the workspace")
        return path

    def create(self) -> tuple[str, Path]:
        output_id = uuid4().hex
        path = self.path(output_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return output_id, path

    def save(self, text: str) -> str:
        output_id, path = self.create()
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        return output_id

    def read(self, output_id: str, offset: int = 0, limit: int | None = None) -> str:
        budget = self.limit if limit is None else min(limit, self.limit)
        return read_page(
            self.path(output_id),
            offset,
            budget,
            lambda next_offset: (
                f"\n[read_tool_output: output_id={output_id}, offset={next_offset}]"
            ),
        )

    def bound(self, text: str) -> str:
        if len(text) <= self.limit:
            return text
        return self.read(self.save(text))

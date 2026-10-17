from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GreetInput:
    """问候功能的输入姓名。"""

    name: str


@dataclass(frozen=True, slots=True)
class GreetResult:
    """问候功能生成的完整消息。"""

    message: str

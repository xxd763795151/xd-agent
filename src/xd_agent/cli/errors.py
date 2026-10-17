from enum import IntEnum

from xd_agent.features.greeting.service import ApplicationError


class ExitCode(IntEnum):
    """CLI 使用的进程退出码。"""

    OK = 0
    INVALID_USAGE = 2
    NOT_FOUND = 3
    CONFLICT = 4
    UNAVAILABLE = 5
    PARTIAL_SUCCESS = 6
    INTERNAL_ERROR = 70
    INTERRUPTED = 130


def exit_code_for(error: ApplicationError) -> ExitCode:
    """将应用层错误映射为无效输入退出码。"""
    return ExitCode.INVALID_USAGE

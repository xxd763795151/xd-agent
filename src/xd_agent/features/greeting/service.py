from xd_agent.features.greeting.models import GreetInput, GreetResult


class ApplicationError(Exception):
    """可映射为客户端错误响应的应用层异常。"""

    code = "application_error"


class InvalidNameError(ApplicationError):
    """姓名为空白时抛出的输入错误。"""

    code = "invalid_name"


class GreetingService:
    """根据配置前缀生成示例问候语。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def execute(self, request: GreetInput) -> GreetResult:
        """去除姓名两端空白；姓名为空时抛出 InvalidNameError。"""
        name = request.name.strip()
        if not name:
            raise InvalidNameError("name must not be blank")
        return GreetResult(message=f"{self._prefix}, {name}!")

"""运行时错误类型：调用方需能区分以下不同失败类别。"""


class RuntimeError_(Exception):
    """运行时错误基类。"""


class FunctionNotFoundError(RuntimeError_):
    """按注册名找不到函数。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"function not found: {name}")


class ValidationError(RuntimeError_):
    """入参校验失败。"""


class FunctionExecutionError(RuntimeError_):
    """函数内部执行报错（携带原始异常信息）。"""

    def __init__(self, name: str, cause: BaseException) -> None:
        self.name = name
        self.cause = cause
        super().__init__(f"function {name!r} failed: {cause!r}")


class RetryError(RuntimeError_):
    """重试相关错误基类。"""


class RetryExhaustedError(RetryError):
    """可重试错误在重试次数耗尽后仍未成功。"""

    def __init__(self, name: str, attempts: int, cause: BaseException) -> None:
        self.name = name
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"function {name!r} exhausted {attempts} attempts: {cause!r}"
        )


class NonRetryableError(RetryError):
    """明确不该重试的错误（如业务校验在函数内部抛出）。"""

    def __init__(self, name: str, cause: BaseException) -> None:
        self.name = name
        self.cause = cause
        super().__init__(f"function {name!r} non-retryable: {cause!r}")


class PermanentError(Exception):
    """用户函数可抛出的标记异常：即使策略允许重试也立即终态失败。

    与 RetryPolicy.retry_on 配合使用：命中该标记的错误一律不可重试，
    终态为 FAILED_NON_RETRYABLE。
    """

    def __init__(self, message: str = "permanent failure") -> None:
        super().__init__(message)


class TimeoutExceededError(RetryError):
    """单次调用超时耗尽。"""

    def __init__(self, name: str, timeout: float) -> None:
        self.name = name
        self.timeout = timeout
        super().__init__(f"function {name!r} timed out after {timeout}s")

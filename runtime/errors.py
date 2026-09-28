"""错误类型定义：区分函数不存在、入参校验失败、函数内部错误等。"""


class RuntimeError(Exception):
    """运行时错误基类，携带可分类的错误码。"""

    error_type = "runtime_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        if retryable is not None:
            self.retryable = retryable
        self.cause = cause

    def to_dict(self) -> dict:
        return {"error_type": self.error_type, "message": self.message, "retryable": self.retryable}


class FunctionNotFoundError(RuntimeError):
    """按注册名找不到函数。"""

    error_type = "function_not_found"
    retryable = False


class InvalidPayloadError(RuntimeError):
    """入参校验失败。"""

    error_type = "invalid_payload"
    retryable = False


class FunctionExecutionError(RuntimeError):
    """函数内部抛出的错误。默认可重试，除非函数显式抛出 NonRetryableError。"""

    error_type = "function_execution_error"
    retryable = True


class NonRetryableError(FunctionExecutionError):
    """函数显式声明该错误不可重试。"""

    error_type = "non_retryable_error"
    retryable = False


class InvocationTimeoutError(RuntimeError):
    """单次尝试超过超时时间。"""

    error_type = "timeout"
    retryable = True


class RetriesExhaustedError(RuntimeError):
    """重试次数耗尽，进入终态 FAILED。"""

    error_type = "retries_exhausted"
    retryable = False

    def __init__(self, message: str, *, attempts: int = 0, last_error: dict | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error or {}

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["attempts"] = self.attempts
        data["last_error"] = self.last_error
        return data


class TimeoutExhaustedError(RuntimeError):
    """总超时（deadline）耗尽，进入终态 TIMED_OUT。"""

    error_type = "timeout_exhausted"
    retryable = False


class IdempotencyKeyConflictError(RuntimeError):
    """同一幂等键绑定了不同的函数名。"""

    error_type = "idempotency_key_conflict"
    retryable = False

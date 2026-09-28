"""运行时核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class Status(str, Enum):
    """一次调用的终态/中间态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED_RETRY_EXHAUSTED = "failed_retry_exhausted"
    FAILED_NON_RETRYABLE = "failed_non_retryable"
    FAILED_TIMEOUT = "failed_timeout"


@dataclass(frozen=True)
class RetryPolicy:
    """重试与超时策略。

    max_attempts: 总尝试次数（含首次），1 表示不重试。
    backoff_seconds: 两次尝试之间的固定退避秒数。
    timeout_seconds: 单次尝试超时，None 表示不限制。
    retry_on: 可重试的异常类型元组。
    """

    max_attempts: int = 3
    backoff_seconds: float = 0.0
    timeout_seconds: float | None = None
    retry_on: tuple[type[BaseException], ...] = (Exception,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be >= 0")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0 or None")


@dataclass(frozen=True)
class FunctionSpec:
    """一个已注册函数的描述。"""

    name: str
    func: Callable[..., Any]
    is_async: bool
    validator: Callable[[Any], None] | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class CallResult:
    """一次幂等调用的结果（会被持久化）。"""

    idempotency_key: str
    function_name: str
    status: Status
    value: Any = None
    error_type: str | None = None
    error_message: str | None = None
    attempts: int = 0

    @property
    def terminal(self) -> bool:
        """是否已落到终态。"""
        return self.status in (
            Status.SUCCEEDED,
            Status.FAILED_RETRY_EXHAUSTED,
            Status.FAILED_NON_RETRYABLE,
            Status.FAILED_TIMEOUT,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "function_name": self.function_name,
            "status": self.status.value,
            "value": self.value,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CallResult:
        return cls(
            idempotency_key=data["idempotency_key"],
            function_name=data["function_name"],
            status=Status(data["status"]),
            value=data.get("value"),
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
            attempts=data.get("attempts", 0),
        )

"""核心数据模型：调用状态、重试策略、函数规格与调用记录。"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable


class InvocationStatus(str, enum.Enum):
    """调用的执行状态。

    RUNNING   执行中（含等待中的尝试）
    SUCCEEDED 成功终态
    FAILED    失败终态（函数报错且不可重试，或重试次数耗尽）
    TIMED_OUT 超时终态（总 deadline 耗尽）
    """

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in (
            InvocationStatus.SUCCEEDED,
            InvocationStatus.FAILED,
            InvocationStatus.TIMED_OUT,
        )


@dataclass
class RetryPolicy:
    """重试策略。

    max_attempts        最大尝试次数（含首次），1 表示不重试
    backoff_seconds     重试间隔基数（线性退避: attempt_index * backoff_seconds）
    timeout_seconds     单次尝试超时；None 表示不限制
    total_deadline_seconds 整体截止时间；None 表示不限制
    """

    max_attempts: int = 3
    backoff_seconds: float = 0.0
    timeout_seconds: float | None = None
    total_deadline_seconds: float | None = None


Validator = Callable[[Any], None]
SyncFn = Callable[[Any], Any]
AsyncFn = Callable[[Any], Awaitable[Any]]


@dataclass
class FunctionSpec:
    """注册的函数规格。"""

    name: str
    func: SyncFn | AsyncFn
    is_async: bool
    validator: Validator | None = None
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class InvocationRecord:
    """一次幂等调用的持久化记录。"""

    idempotency_key: str
    function_name: str
    status: InvocationStatus
    payload: Any = None
    result: Any = None
    error: dict | None = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "idempotency_key": self.idempotency_key,
            "function_name": self.function_name,
            "status": self.status.value,
            "payload": self.payload,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

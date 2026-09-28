"""执行器：带超时与重试地执行一次幂等调用，并持久化每次尝试。"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from runtime.errors import (
    FunctionExecutionError,
    InvocationTimeoutError,
    NonRetryableError,
    RetriesExhaustedError,
    TimeoutExhaustedError,
)
from runtime.models import FunctionSpec, InvocationRecord, InvocationStatus
from runtime.store import InvocationStore

logger = logging.getLogger("runtime.executor")


class Executor:
    def __init__(self, store: InvocationStore) -> None:
        self._store = store

    async def execute(self, spec: FunctionSpec, record: InvocationRecord) -> None:
        """执行并就地更新 record，终态为 SUCCEEDED / FAILED / TIMED_OUT。"""
        policy = spec.retry_policy
        started = time.monotonic()
        log = logging.LoggerAdapter(
            logger, {"idempotency_key": record.idempotency_key}
        )

        def deadline_exceeded() -> bool:
            return (
                policy.total_deadline_seconds is not None
                and time.monotonic() - started >= policy.total_deadline_seconds
            )

        def persist() -> None:
            self._store.update(record)

        for attempt in range(1, policy.max_attempts + 1):
            if deadline_exceeded():
                self._mark_timeout(record, "total deadline exhausted before attempt")
                persist()
                return

            record.attempts = attempt
            record.status = InvocationStatus.RUNNING if attempt == 1 else InvocationStatus.RETRYING
            persist()
            log.info(
                "invoke start idempotency_key=%s function=%s attempt=%d/%d status=%s",
                record.idempotency_key, spec.name, attempt, policy.max_attempts, record.status.value,
            )

            try:
                record.result = await self._attempt(spec, record.payload, policy.timeout_seconds)
            except NonRetryableError as exc:
                record.result = None
                record.error = self._error_dict(exc)
                record.status = InvocationStatus.FAILED
                persist()
                log.info(
                    "invoke end idempotency_key=%s attempt=%d status=FAILED non_retryable error=%s",
                    record.idempotency_key, attempt, exc.message,
                )
                return
            except InvocationTimeoutError as exc:
                error = exc
            except Exception as exc:  # 用户函数内部错误（含校验器之外的任何异常）
                error = FunctionExecutionError(
                    f"{type(exc).__name__}: {exc}", cause=exc
                )
            else:
                record.error = None
                record.status = InvocationStatus.SUCCEEDED
                persist()
                log.info(
                    "invoke end idempotency_key=%s attempt=%d status=SUCCEEDED",
                    record.idempotency_key, attempt,
                )
                return

            record.result = None
            record.error = self._error_dict(error)
            persist()
            log.warning(
                "invoke failed idempotency_key=%s function=%s attempt=%d/%d retryable=%s error=%s",
                record.idempotency_key, spec.name, attempt, policy.max_attempts,
                error.retryable, error.message,
            )

            if not error.retryable:
                record.status = InvocationStatus.FAILED
                persist()
                log.info("invoke end idempotency_key=%s status=FAILED", record.idempotency_key)
                return

            if attempt >= policy.max_attempts:
                exhausted = RetriesExhaustedError(
                    f"retries exhausted after {attempt} attempt(s): {error.message}",
                    attempts=attempt,
                    last_error=record.error,
                )
                record.error = exhausted.to_dict()
                record.status = InvocationStatus.FAILED
                persist()
                log.info(
                    "invoke end idempotency_key=%s attempts=%d status=FAILED reason=retries_exhausted",
                    record.idempotency_key, attempt,
                )
                return

            # 退避后重试；退避也要受总 deadline 约束
            delay = policy.backoff_seconds * attempt
            if policy.total_deadline_seconds is not None:
                remaining = policy.total_deadline_seconds - (time.monotonic() - started)
                delay = min(delay, max(remaining, 0.0))
                if remaining <= 0:
                    self._mark_timeout(record, f"total deadline exhausted: {error.message}")
                    persist()
                    return
            await asyncio.sleep(delay)

    async def _attempt(self, spec: FunctionSpec, payload: Any, timeout: float | None) -> Any:
        if spec.is_async:
            coro = spec.func(payload)
            if timeout is None:
                return await coro
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except TimeoutError as exc:
                raise InvocationTimeoutError(
                    f"attempt timed out after {timeout}s", retryable=True
                ) from exc

        # 同步函数：每次尝试使用独立守护线程，卡住的调用不会阻塞事件循环，
        # 也不会占用共享线程池，因而不会拖垮其他调用。
        return await asyncio.to_thread(self._run_sync_in_thread, spec.func, payload, timeout)

    @staticmethod
    def _run_sync_in_thread(func, payload: Any, timeout: float | None) -> Any:
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = func(payload)
            except Exception as exc:
                # 用户声明的不可重试错误原样透传
                if isinstance(exc, NonRetryableError):
                    box["non_retryable"] = exc
                else:
                    box["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise InvocationTimeoutError(
                f"attempt timed out after {timeout}s (worker still running)",
                retryable=True,
            )
        if "non_retryable" in box:
            raise box["non_retryable"]
        if "error" in box:
            raise box["error"]
        return box.get("result")

    @staticmethod
    def _error_dict(exc: Exception) -> dict:
        data = exc.to_dict() if hasattr(exc, "to_dict") else {"message": str(exc)}
        data.setdefault("error_type", "function_execution_error")
        data.setdefault("retryable", True)
        cause = getattr(exc, "cause", None)
        data["exception_type"] = type(cause).__name__ if cause is not None else type(exc).__name__
        return data

    @staticmethod
    def _mark_timeout(record: InvocationRecord, message: str) -> None:
        record.status = InvocationStatus.TIMED_OUT
        record.result = None
        record.error = TimeoutExhaustedError(
            message,
        ).to_dict()

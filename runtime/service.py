"""运行时服务：编排注册、幂等去重、重试、超时与终态落盘。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from runtime.errors import (
    PermanentError,
    TimeoutExceededError,
    ValidationError,
)
from runtime.executor import Executor
from runtime.models import CallResult, RetryPolicy, Status
from runtime.registry import FunctionRegistry
from runtime.store import InvocationStore

_DEFAULT_LOGGER_NAME = "runtime"


class RuntimeService:
    def __init__(
        self,
        store: InvocationStore | None = None,
        executor: Executor | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.registry = FunctionRegistry()
        self.store = store or InvocationStore()
        self.executor = executor or Executor()
        self.logger = logger or logging.getLogger(_DEFAULT_LOGGER_NAME)
        # 幂等键维度的领导权锁：保证同一时刻只有一个线程真正执行
        self._leader_locks: dict[str, threading.Lock] = {}
        self._leader_locks_guard = threading.Lock()

    def register(self, *args: Any, **kwargs: Any) -> Callable[..., Any]:
        """委托给注册表。"""
        return self.registry.register(*args, **kwargs)

    def invoke(
        self,
        name: str,
        payload: Any = None,
        *,
        idempotency_key: str,
        retry_policy: RetryPolicy | None = None,
    ) -> CallResult:
        """按幂等键调用函数；重复/并发调用返回同一份终态结果。

        函数名不存在、入参校验失败属于调用方错误，直接抛出对应异常，
        不会占用幂等键。函数内部执行结果（成功/各类失败终态）按
        幂等键持久化，重复与并发调用拿到同一份 CallResult。
        """
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")

        # 1. 函数必须存在（错误类别一：函数名不存在）
        spec = self.registry.get(name)

        # 2. 入参校验（错误类别二：入参校验失败）
        if spec.validator is not None:
            try:
                spec.validator(payload)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc

        policy = retry_policy or spec.retry_policy

        # 3. 幂等去重：已终态直接复用，进行中则等待同一份结果
        existing, wait_event = self.store.begin(idempotency_key)
        if existing is not None:
            self.logger.info(
                "invoke dedup-hit key=%s function=%s status=%s attempts=%d",
                idempotency_key,
                name,
                existing.status.value,
                existing.attempts,
            )
            return existing
        if wait_event is not None:
            return self._wait_result(idempotency_key, name, wait_event, policy)

        # 4. 首次到达：获取该键的领导权锁后真正执行
        lock = self._get_leader_lock(idempotency_key)
        with lock:
            # 双重检查：可能在等待 begin/锁的期间已被其他线程落盘
            existing = self.store.load(idempotency_key)
            if existing is not None and existing.terminal:
                self.logger.info(
                    "invoke dedup-hit key=%s function=%s status=%s attempts=%d",
                    idempotency_key,
                    name,
                    existing.status.value,
                    existing.attempts,
                )
                return existing
            return self._execute_with_retry(
                spec, payload, idempotency_key, policy
            )

    def get_result(self, idempotency_key: str) -> CallResult | None:
        return self.store.load(idempotency_key)

    def shutdown(self) -> None:
        self.executor.shutdown()

    # ------------------------------------------------------------------

    def _get_leader_lock(self, key: str) -> threading.Lock:
        with self._leader_locks_guard:
            lock = self._leader_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._leader_locks[key] = lock
            return lock

    def _wait_result(
        self,
        key: str,
        name: str,
        event: threading.Event,
        policy: RetryPolicy,
    ) -> CallResult:
        # 等待时长按最坏执行时间上界估算，避免无限等待卡住调用方
        deadline = None
        if policy.timeout_seconds is not None:
            deadline = policy.max_attempts * policy.timeout_seconds
        self.logger.info(
            "invoke waiting key=%s function=%s",
            key,
            name,
        )
        event.wait(timeout=deadline)
        result = self.store.load(key)
        if result is None or not result.terminal:
            # 理论上不应发生；落盘与事件 set 在同一把锁内，属于不变量破坏
            raise RuntimeError(f"execution for key {key!r} did not complete")
        self.logger.info(
            "invoke dedup-hit key=%s function=%s status=%s attempts=%d",
            key,
            name,
            result.status.value,
            result.attempts,
        )
        return result

    def _execute_with_retry(
        self,
        spec: Any,
        payload: Any,
        key: str,
        policy: RetryPolicy,
    ) -> CallResult:
        attempt = 0
        last_exc: BaseException | None = None
        while attempt < policy.max_attempts:
            attempt += 1
            self.logger.info(
                "invoke attempt key=%s function=%s attempt=%d/%d",
                key,
                spec.name,
                attempt,
                policy.max_attempts,
            )
            try:
                value = self.executor.run(
                    spec, payload, timeout=policy.timeout_seconds
                )
            except PermanentError as exc:
                # 用户显式标记不可重试，立即终态
                result = CallResult(
                    key,
                    spec.name,
                    Status.FAILED_NON_RETRYABLE,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    attempts=attempt,
                )
                self._finish(key, result)
                return result
            except TimeoutExceededError as exc:
                last_exc = exc
                # 超时默认可重试；最后一次尝试超时则落到超时终态
                if attempt >= policy.max_attempts:
                    result = CallResult(
                        key,
                        spec.name,
                        Status.FAILED_TIMEOUT,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        attempts=attempt,
                    )
                    self._finish(key, result)
                    return result
            except BaseException as exc:  # noqa: BLE001 - 边界统一分类
                last_exc = exc
                retryable = isinstance(exc, policy.retry_on) and not isinstance(
                    exc, PermanentError
                )
                if not retryable or attempt >= policy.max_attempts:
                    status = (
                        Status.FAILED_NON_RETRYABLE
                        if not retryable
                        else Status.FAILED_RETRY_EXHAUSTED
                    )
                    result = CallResult(
                        key,
                        spec.name,
                        status,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        attempts=attempt,
                    )
                    self._finish(key, result)
                    return result
            else:
                result = CallResult(
                    key,
                    spec.name,
                    Status.SUCCEEDED,
                    value=value,
                    attempts=attempt,
                )
                self._finish(key, result)
                return result

            self.logger.info(
                "invoke retry key=%s function=%s next_attempt=%d/%d",
                key,
                spec.name,
                attempt + 1,
                policy.max_attempts,
            )
            if policy.backoff_seconds:
                time.sleep(policy.backoff_seconds)

        # 不可达：循环内必有 return；保留作为防御性终态
        result = CallResult(
            key,
            spec.name,
            Status.FAILED_RETRY_EXHAUSTED,
            error_type=type(last_exc).__name__ if last_exc else None,
            error_message=str(last_exc) if last_exc else "retry exhausted",
            attempts=attempt,
        )
        self._finish(key, result)
        return result

    def _finish(self, key: str, result: CallResult) -> None:
        self.store.save(result)
        self.logger.info(
            "invoke finished key=%s function=%s status=%s attempts=%d",
            key,
            result.function_name,
            result.status.value,
            result.attempts,
        )

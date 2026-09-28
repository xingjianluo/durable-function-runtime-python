"""门面服务：注册表 + 持久化存储 + 并发去重。

并发语义：
- 同一幂等键在本进程内并发到达时，只有第一个请求真正执行，其余请求等待
  并拿到同一份 InvocationRecord（同一份结果）。
- 终态持久化在 SQLite 中，进程重启后重复触发仍只命中既有终态，不会再执行。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from runtime.errors import (
    IdempotencyKeyConflictError,
    InvalidPayloadError,
    RuntimeError as RTRuntimeError,
)
from runtime.executor import Executor
from runtime.models import (
    FunctionSpec,
    InvocationRecord,
    InvocationStatus,
    RetryPolicy,
    SyncFn,
    AsyncFn,
    Validator,
)
from runtime.registry import FunctionRegistry
from runtime.store import InvocationStore

logger = logging.getLogger("runtime.service")

_WAIT_POLL_INTERVAL = 0.02
_WAIT_TIMEOUT_SECONDS = 300.0


class FunctionRuntime:
    def __init__(self, db_path: str = "runtime.db") -> None:
        self.registry = FunctionRegistry()
        self.store = InvocationStore(db_path)
        removed = self.store.reset_stale_running()
        if removed:
            logger.info("recovered %d non-terminal record(s) from previous process", removed)
        self._executor = Executor(self.store)
        self._inflight: dict[str, asyncio.Event] = {}
        self._inflight_lock = asyncio.Lock()

    def register(
        self,
        name: str,
        func: SyncFn | AsyncFn,
        *,
        validator: Validator | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.registry.register(name, func, validator=validator, retry_policy=retry_policy)

    async def invoke(
        self,
        function_name: str,
        payload: Any = None,
        idempotency_key: str | None = None,
    ) -> InvocationRecord:
        if not idempotency_key:
            raise InvalidPayloadError("idempotency_key is required")
        log = logging.LoggerAdapter(logger, {"idempotency_key": idempotency_key})

        # 函数名不存在在这里直接判定，与入参错误、函数内部错误明确区分
        spec = self.registry.get(function_name)

        # 快速路径：终态记录直接返回（含进程重启后的场景）
        existing = self.store.load(idempotency_key)
        if existing is not None and existing.status.is_terminal:
            self._check_function_binding(existing, function_name, idempotency_key)
            log.info(
                "invoke dedup-hit function=%s status=%s attempts=%d",
                function_name, existing.status.value, existing.attempts,
            )
            return existing

        async with self._inflight_lock:
            existing = self.store.load(idempotency_key)
            if existing is not None and existing.status.is_terminal:
                self._check_function_binding(existing, function_name, idempotency_key)
                log.info(
                    "invoke dedup-hit function=%s status=%s attempts=%d",
                    function_name, existing.status.value, existing.attempts,
                )
                return existing

            event = self._inflight.get(idempotency_key)
            if event is not None:
                should_wait = True
            else:
                should_wait = False
                event = asyncio.Event()
                self._inflight[idempotency_key] = event

        if should_wait:
            return await self._wait_completion(idempotency_key, function_name, event, log)

        # 本请求是领导者，负责真正执行
        try:
            return await self._lead(spec, payload, idempotency_key, event, log)
        finally:
            async with self._inflight_lock:
                self._inflight.pop(idempotency_key, None)
            event.set()

    async def _lead(
        self,
        spec: FunctionSpec,
        payload: Any,
        idempotency_key: str,
        event: asyncio.Event,
        log: logging.LoggerAdapter,
    ) -> InvocationRecord:
        # 入参校验只在首次执行时做；终态命中走快速路径直接返回旧结果
        if spec.validator is not None:
            try:
                spec.validator(payload)
            except Exception as exc:
                raise InvalidPayloadError(
                    f"payload validation failed: {type(exc).__name__}: {exc}",
                    cause=exc,
                ) from exc

        record = InvocationRecord(
            idempotency_key=idempotency_key,
            function_name=spec.name,
            status=InvocationStatus.RUNNING,
            payload=payload,
        )
        if not self.store.insert(record):
            # 另一个进程抢先插入：轮询等待对方的终态
            log.info("invoke lost-insert race, waiting peer process")
            return await self._wait_completion(idempotency_key, spec.name, event, log)

        log.info("invoke accepted function=%s", spec.name)
        try:
            await self._executor.execute(spec, record)
        except Exception as exc:
            # 兜底：执行器自身不应抛出，但要保证落到明确终态
            record.status = InvocationStatus.FAILED
            record.error = {
                "error_type": "function_execution_error",
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
                "exception_type": type(exc).__name__,
            }
            self.store.update(record)
            log.error("invoke end status=FAILED unexpected error=%s", exc)
        log.info(
            "invoke terminal function=%s status=%s attempts=%d",
            spec.name, record.status.value, record.attempts,
        )
        return record

    async def _wait_completion(
        self,
        idempotency_key: str,
        function_name: str,
        event: asyncio.Event,
        log: logging.LoggerAdapter,
    ) -> InvocationRecord:
        deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
        record = None
        while True:
            # 同时兼容本进程领导者（event 会被 set）与跨进程领导者（只能轮询存储）
            record = self.store.load(idempotency_key)
            if record is not None and record.status.is_terminal:
                break
            if event.is_set():
                record = self.store.load(idempotency_key)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RTRuntimeError(
                    f"timed out waiting for in-flight invocation {idempotency_key!r}"
                )
            try:
                await asyncio.wait_for(event.wait(), timeout=min(_WAIT_POLL_INTERVAL, remaining))
            except TimeoutError:
                pass

        if record is None or not record.status.is_terminal:
            # 极端情况下领导者异常结束且没有终态：交由调用方重试
            raise RTRuntimeError(
                f"leader invocation {idempotency_key!r} finished without a terminal record"
            )
        self._check_function_binding(record, function_name, idempotency_key)
        log.info(
            "invoke dedup-hit function=%s status=%s attempts=%d",
            function_name, record.status.value, record.attempts,
        )
        return record

    @staticmethod
    def _check_function_binding(
        record: InvocationRecord, function_name: str, idempotency_key: str
    ) -> None:
        if record.function_name != function_name:
            raise IdempotencyKeyConflictError(
                f"idempotency_key {idempotency_key!r} already bound to "
                f"{record.function_name!r}"
            )

    def shutdown(self) -> None:
        self.store.close()

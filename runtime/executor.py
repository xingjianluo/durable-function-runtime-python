"""单次函数执行器：在独立线程中运行同步/异步函数，支持超时隔离。"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from runtime.errors import TimeoutExceededError
from runtime.models import FunctionSpec


class Executor:
    """在受限线程池中执行函数，单个卡住的调用不会拖垮其他调用。"""

    def __init__(self, max_workers: int = 8) -> None:
        self.max_workers = max_workers
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="fn-exec",
        )

    def run(
        self,
        spec: FunctionSpec,
        payload: Any,
        *,
        timeout: float | None,
    ) -> Any:
        """执行一次函数尝试，成功返回返回值，失败抛出原始异常。

        同步与异步函数统一在工作线程中运行；超时则放弃等待并抛出
        TimeoutExceededError，其他调用不受影响。
        """
        if spec.is_async:
            future = self._pool.submit(self._run_async, spec.func, payload)
        else:
            future = self._pool.submit(self._run_sync, spec.func, payload)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutExceededError(spec.name, timeout or 0.0) from None
        except ExecutionFailure as fail:
            raise fail.cause from None

    @staticmethod
    def _run_sync(func: Any, payload: Any) -> Any:
        try:
            return func(payload)
        except BaseException as exc:  # 原样回传，由 service 决定是否重试
            raise ExecutionFailure(exc) from None

    @staticmethod
    def _run_async(func: Any, payload: Any) -> Any:
        try:
            return asyncio.run(func(payload))
        except BaseException as exc:
            raise ExecutionFailure(exc) from None

    def shutdown(self) -> None:
        # 不等待卡住的工作线程，避免关闭时被拖死
        self._pool.shutdown(wait=False, cancel_futures=True)


class ExecutionFailure(Exception):
    """工作线程内原始异常的包装（边界传递用）。"""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(repr(cause))

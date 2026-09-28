"""共享 pytest 配置：每个测试使用独立的临时数据库。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime import FunctionRuntime, NonRetryableError, RetryPolicy  # noqa: E402


class _DefaultKeyFilter(logging.Filter):
    """formatter 引用 idempotency_key 字段；未显式传入时补 '-'。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.__dict__.setdefault("idempotency_key", "-")
        return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtestloop(session):
    for handler in logging.getLogger().handlers:
        handler.addFilter(_DefaultKeyFilter())
    yield


@pytest.fixture
def rt(tmp_path):
    runtime = FunctionRuntime(db_path=str(tmp_path / "test.db"))

    def echo(payload):
        return {"doubled": payload * 2}

    def require_int(payload):
        if not isinstance(payload, int):
            raise ValueError("payload must be int")

    async def async_echo(payload):
        return {"async": payload}

    def boom(payload):
        raise RuntimeError("kaboom inside function")

    def fatal(payload):
        raise NonRetryableError("permanent failure")

    def slow(payload):
        import time

        time.sleep(1.0)
        return payload

    runtime.register("echo", echo, validator=require_int,
                     retry_policy=RetryPolicy(max_attempts=1))
    runtime.register("async_echo", async_echo, validator=require_int,
                     retry_policy=RetryPolicy(max_attempts=1))
    runtime.register("boom", boom,
                     retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0))
    runtime.register("fatal", fatal,
                     retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0))
    runtime.register("slow", slow,
                     retry_policy=RetryPolicy(max_attempts=2, timeout_seconds=0.2,
                                              backoff_seconds=0))
    yield runtime
    runtime.shutdown()

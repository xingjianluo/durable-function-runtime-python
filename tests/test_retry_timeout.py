"""重试与超时：可重试 vs 不可重试、次数耗尽、超时耗尽、终态明确。"""

from __future__ import annotations

import time

from tests._asyncio_utils import async_test
from runtime import (
    InvocationStatus,
    NonRetryableError,
    RetryPolicy,
)


@async_test
async def test_retry_until_success(rt):
    attempts = {"n": 0}

    def flaky(payload):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError(f"transient {attempts['n']}")
        return "recovered"

    rt.register("recover", flaky,
                retry_policy=RetryPolicy(max_attempts=5, backoff_seconds=0.01))

    record = await rt.invoke("recover", None, "retry-ok")
    assert record.status is InvocationStatus.SUCCEEDED
    assert record.result == "recovered"
    assert record.attempts == 3
    assert record.error is None


@async_test
async def test_retries_exhausted_terminal(rt):
    record = await rt.invoke("boom", None, "retry-exhausted")
    assert record.status is InvocationStatus.FAILED
    assert record.error["error_type"] == "retries_exhausted"
    assert record.error["attempts"] == 3
    assert record.error["last_error"]["retryable"] is True
    assert record.error["last_error"]["exception_type"] == "RuntimeError"


@async_test
async def test_non_retryable_fails_immediately(rt):
    record = await rt.invoke("fatal", None, "retry-nonretryable")
    assert record.status is InvocationStatus.FAILED
    assert record.error["error_type"] == "non_retryable_error"
    assert record.error["retryable"] is False
    assert record.attempts == 1  # 没有进行任何重试


@async_test
async def test_attempt_timeout_is_retryable_but_eventually_exhausted(rt):
    t0 = time.monotonic()
    record = await rt.invoke("slow", None, "retry-timeout")
    elapsed = time.monotonic() - t0

    assert record.status is InvocationStatus.FAILED
    assert record.error["error_type"] == "retries_exhausted"
    assert record.error["last_error"]["error_type"] == "timeout"
    assert record.attempts == 2
    # 没有傻等线程结束：总耗时应远小于两次 sleep(1)
    assert elapsed < 0.8, f"elapsed={elapsed:.2f}s"


@async_test
async def test_total_deadline_exhausted_terminal(rt):
    attempts = {"n": 0}

    def slow_flaky(payload):
        attempts["n"] += 1
        time.sleep(0.15)
        raise RuntimeError("still failing")

    rt.register(
        "deadline_fn",
        slow_flaky,
        retry_policy=RetryPolicy(
            max_attempts=10,
            backoff_seconds=0.1,
            timeout_seconds=0.1,
            total_deadline_seconds=0.45,
        ),
    )

    t0 = time.monotonic()
    record = await rt.invoke("deadline_fn", None, "retry-deadline")
    elapsed = time.monotonic() - t0

    assert record.status is InvocationStatus.TIMED_OUT
    assert record.error["error_type"] == "timeout_exhausted"
    assert elapsed < 1.5
    assert attempts["n"] >= 1

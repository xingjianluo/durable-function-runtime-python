"""注册/调用基础能力：同步与异步函数、三类错误必须可区分。"""

from __future__ import annotations

import pytest

from tests._asyncio_utils import async_test
from runtime import (
    FunctionNotFoundError,
    InvalidPayloadError,
    InvocationStatus,
)


@async_test
async def test_sync_function_invocation(rt):
    record = await rt.invoke("echo", 21, "basic-sync")
    assert record.status is InvocationStatus.SUCCEEDED
    assert record.result == {"doubled": 42}
    assert record.attempts == 1


@async_test
async def test_async_function_invocation(rt):
    record = await rt.invoke("async_echo", 7, "basic-async")
    assert record.status is InvocationStatus.SUCCEEDED
    assert record.result == {"async": 7}


@async_test
async def test_function_not_found_is_distinct(rt):
    with pytest.raises(FunctionNotFoundError) as exc:
        await rt.invoke("does-not-exist", None, "err-missing")
    assert exc.value.error_type == "function_not_found"
    assert exc.value.retryable is False


@async_test
async def test_invalid_payload_is_distinct(rt):
    with pytest.raises(InvalidPayloadError) as exc:
        await rt.invoke("echo", "not-an-int", "err-payload")
    assert exc.value.error_type == "invalid_payload"
    assert exc.value.retryable is False


@async_test
async def test_function_internal_error_is_distinct(rt):
    record = await rt.invoke("boom", 1, "err-boom")
    assert record.status is InvocationStatus.FAILED
    assert record.error["error_type"] == "retries_exhausted"
    assert record.error["last_error"]["error_type"] == "function_execution_error"
    assert record.error["last_error"]["exception_type"] == "RuntimeError"
    assert record.attempts == 3

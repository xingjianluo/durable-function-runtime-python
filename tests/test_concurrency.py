"""并发触发：同键并发只执行一次，所有等待者拿到同一份结果。"""

from __future__ import annotations

import asyncio
import time

from tests._asyncio_utils import async_test
from runtime import InvocationStatus, RetryPolicy


@async_test
async def test_concurrent_same_key_single_execution(rt):
    call_count = {"n": 0}
    entered = asyncio.Event()

    async def gated(payload):
        call_count["n"] += 1
        entered.set()
        await asyncio.sleep(0.15)
        return {"served_by_call": call_count["n"]}

    rt.register("gated", gated, retry_policy=RetryPolicy(max_attempts=1))

    async def call():
        return await rt.invoke("gated", {"x": 1}, "conc-001")

    results = await asyncio.gather(*[call() for _ in range(10)])

    assert call_count["n"] == 1
    assert all(r.status is InvocationStatus.SUCCEEDED for r in results)
    assert all(r.result == {"served_by_call": 1} for r in results)
    assert all(r.attempts == 1 for r in results)
    # 所有等待者指向同一终态语义：存储里只有一条记录
    stored = rt.store.load("conc-001")
    assert stored.status is InvocationStatus.SUCCEEDED
    assert stored.result == {"served_by_call": 1}


@async_test
async def test_concurrent_failure_shared_by_all(rt):
    call_count = {"n": 0}

    async def flaky_then_fail(payload):
        call_count["n"] += 1
        await asyncio.sleep(0.02)
        raise RuntimeError("downstream outage")

    rt.register("flaky_then_fail", flaky_then_fail,
                retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.01))

    results = await asyncio.gather(
        *[rt.invoke("flaky_then_fail", None, "conc-fail") for _ in range(6)],
        return_exceptions=True,
    )

    assert call_count["n"] == 3  # 只有领导者真正尝试 3 次
    for r in results:
        assert not isinstance(r, Exception)
        assert r.status is InvocationStatus.FAILED
        assert r.error["error_type"] == "retries_exhausted"
        assert r.attempts == 3


@async_test
async def test_stuck_call_does_not_block_other_calls(rt):
    """一个卡住的调用不能拖垮其他调用。"""

    async def quick(payload):
        return {"ok": True}

    rt.register("quick", quick, retry_policy=RetryPolicy(max_attempts=1))

    # slow 函数单次超时 0.2s、重试 2 次；它跑它的，别的调用必须立刻成功
    slow_task = asyncio.create_task(rt.invoke("slow", 1, "conc-stuck"))
    await asyncio.sleep(0.05)  # 让 slow 先开始

    t0 = time.monotonic()
    quick_results = await asyncio.gather(
        *[rt.invoke("quick", None, f"conc-quick-{i}") for i in range(5)]
    )
    elapsed = time.monotonic() - t0

    slow_record = await slow_task

    assert elapsed < 0.2, f"other calls were blocked for {elapsed:.2f}s"
    assert all(r.result == {"ok": True} for r in quick_results)
    assert slow_record.status is InvocationStatus.FAILED
    assert slow_record.error["last_error"]["error_type"] == "timeout"


@async_test
async def test_distinct_keys_run_concurrently(rt):
    order = []

    async def numbered(payload):
        order.append(payload)
        await asyncio.sleep(0.1)
        return {"n": payload}

    rt.register("numbered", numbered, retry_policy=RetryPolicy(max_attempts=1))

    results = await asyncio.gather(
        *[rt.invoke("numbered", i, f"distinct-{i}") for i in range(5)]
    )
    assert sorted(r.result["n"] for r in results) == [0, 1, 2, 3, 4]

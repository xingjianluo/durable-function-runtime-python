"""幂等去重：同键只真正执行一次；进程重启后终态仍命中。"""

from __future__ import annotations

from tests._asyncio_utils import async_test
from runtime import FunctionRuntime, InvocationStatus, RetryPolicy


@async_test
async def test_duplicate_key_executes_once(rt):
    call_count = {"n": 0}

    def counting(payload):
        call_count["n"] += 1
        return {"n": call_count["n"], "payload": payload}

    rt.register("counting", counting, retry_policy=RetryPolicy(max_attempts=1))

    first = await rt.invoke("counting", {"v": 1}, "dup-001")
    second = await rt.invoke("counting", {"v": 1}, "dup-001")
    third = await rt.invoke("counting", {"v": 1}, "dup-001")

    assert first.status is InvocationStatus.SUCCEEDED
    assert first.result == {"n": 1, "payload": {"v": 1}}
    assert second.result == first.result
    assert third.result == first.result
    assert call_count["n"] == 1
    assert all(r.attempts == 1 for r in (first, second, third))


@async_test
async def test_failed_terminal_is_also_deduplicated(rt):
    call_count = {"n": 0}

    def always_fail(payload):
        call_count["n"] += 1
        raise RuntimeError("permanent-ish")

    rt.register("always_fail", always_fail,
                retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0))

    r1 = await rt.invoke("always_fail", None, "dup-fail")
    r2 = await rt.invoke("always_fail", None, "dup-fail")

    assert r1.status is InvocationStatus.FAILED
    assert r2.status is InvocationStatus.FAILED
    assert r1.error == r2.error
    assert r1.attempts == 2 and r2.attempts == 2
    assert call_count["n"] == 2  # 两次尝试，但函数只被“调用一轮”


def test_terminal_survives_process_restart(tmp_path):
    """模拟进程重启：新建 FunctionRuntime 实例指向同一个 db 文件。"""
    import asyncio

    db = str(tmp_path / "restart.db")

    async def scenario():
        call_count = {"n": 0}

        def once(payload):
            call_count["n"] += 1
            return {"value": payload}

        rt1 = FunctionRuntime(db_path=db)
        rt1.register("once", once, retry_policy=RetryPolicy(max_attempts=1))
        r = await rt1.invoke("once", 99, "restart-key")
        assert r.status is InvocationStatus.SUCCEEDED
        rt1.shutdown()

        # 新进程视角：终态记录仍在，重复触发不执行函数
        rt2 = FunctionRuntime(db_path=db)
        rt2.register("once", once, retry_policy=RetryPolicy(max_attempts=1))
        r2 = await rt2.invoke("once", 99, "restart-key")
        rt2.shutdown()
        return r2, call_count["n"]

    r2, count = asyncio.run(scenario())
    assert r2.status is InvocationStatus.SUCCEEDED
    assert r2.result == {"value": 99}
    assert count == 1


@async_test
async def test_different_keys_run_separately(rt):
    a = await rt.invoke("echo", 3, "key-a")
    b = await rt.invoke("echo", 4, "key-b")
    assert a.result == {"doubled": 6}
    assert b.result == {"doubled": 8}

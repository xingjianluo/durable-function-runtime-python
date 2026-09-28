"""重复调用、并发触发、重启恢复下的幂等保证。"""

import threading

import pytest

from runtime.models import RetryPolicy, Status


def test_repeated_call_executes_once(service) -> None:
    counter = {"n": 0}

    def fn(payload):
        counter["n"] += 1
        return payload

    service.register("once", fn)

    r1 = service.invoke("once", 7, idempotency_key="dup-1")
    r2 = service.invoke("once", 7, idempotency_key="dup-1")
    r3 = service.invoke("once", 999, idempotency_key="dup-1")

    assert counter["n"] == 1
    assert r1.status is Status.SUCCEEDED
    assert r1.value == 7
    # 即使后续 payload 不同，也只能拿到第一次的同一份结果
    assert r2 == r1
    assert r3.value == 7
    assert r1.attempts == 1


def test_concurrent_duplicates_get_same_result(service) -> None:
    started = threading.Event()
    release = threading.Event()
    counter = {"n": 0}

    def slow(payload):
        counter["n"] += 1
        started.set()
        release.wait(timeout=5)
        return {"winner": payload}

    service.register("slow", slow)

    results: list = []
    errors: list = []

    def worker() -> None:
        try:
            results.append(
                service.invoke("slow", "payload", idempotency_key="concurrent-1")
            )
        except BaseException as exc:  # 记录但不吞
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    assert started.wait(timeout=2)
    # 等非领导者都进入等待后再放行
    threading.Event().wait(0.3)
    release.set()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert counter["n"] == 1
    assert len(results) == 8
    assert all(r.status is Status.SUCCEEDED for r in results)
    assert all(r.value == {"winner": "payload"} for r in results)
    assert all(r.attempts == 1 for r in results)


def test_result_survives_process_restart(tmp_path) -> None:
    path = str(tmp_path / "invocations.json")

    def fn(payload):
        return payload + 1

    from runtime.service import RuntimeService
    from runtime.store import InvocationStore
    from runtime.executor import Executor

    svc1 = RuntimeService(InvocationStore(path), Executor())
    svc1.register("add1", fn)
    r1 = svc1.invoke("add1", 41, idempotency_key="restart-1")
    assert r1.value == 42
    counter = {"n": 0}

    def fn2(payload):
        counter["n"] += 1
        return payload + 1

    svc1.shutdown()

    # 模拟进程重启：全新实例读取同一持久化文件
    svc2 = RuntimeService(InvocationStore(path), Executor())
    svc2.register("add1", fn2)
    r2 = svc2.invoke("add1", 41, idempotency_key="restart-1")
    svc2.shutdown()

    assert r2.status is Status.SUCCEEDED
    assert r2.value == 42
    assert r2.attempts == 1
    assert counter["n"] == 0  # 重启后重复触发也不会再执行


def test_failed_terminal_result_is_also_idempotent(service) -> None:
    counter = {"n": 0}

    def always_fail(payload):
        counter["n"] += 1
        raise RuntimeError("boom")

    service.register(
        "fail",
        always_fail,
        retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0),
    )

    r1 = service.invoke("fail", None, idempotency_key="fail-1")
    r2 = service.invoke("fail", None, idempotency_key="fail-1")

    assert r1.status is Status.FAILED_RETRY_EXHAUSTED
    assert r1 == r2
    assert counter["n"] == 2  # 仅首次调用产生 2 次尝试，重复调用不再执行


def test_different_keys_execute_independently(service) -> None:
    counter = {"n": 0}

    def fn(payload):
        counter["n"] += 1
        return payload

    service.register("indep", fn)
    r1 = service.invoke("indep", 1, idempotency_key="key-a")
    r2 = service.invoke("indep", 2, idempotency_key="key-b")

    assert r1.value == 1 and r2.value == 2
    assert counter["n"] == 2


def test_logs_contain_key_and_terminal_status(service, caplog_runtime) -> None:
    service.register("logged", lambda p: p)
    service.invoke("logged", 1, idempotency_key="log-1")

    messages = [r.getMessage() for r in caplog_runtime.records]
    assert any("log-1" in m for m in messages)
    assert any("succeeded" in m for m in messages)
    finished = [m for m in messages if m.startswith("invoke finished")]
    assert finished and "log-1" in finished[0]


def test_empty_idempotency_key_rejected(service) -> None:
    service.register("f", lambda p: p)
    with pytest.raises(ValueError):
        service.invoke("f", None, idempotency_key="")

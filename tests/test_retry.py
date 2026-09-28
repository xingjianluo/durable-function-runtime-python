"""失败重试、不可重试、超时耗尽与故障隔离。"""

import asyncio
import threading
import time

from runtime.errors import PermanentError, ValidationError
from runtime.models import RetryPolicy, Status


def test_retry_succeeds_after_transient_failures(service) -> None:
    counter = {"n": 0}

    def flaky(payload):
        counter["n"] += 1
        if counter["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    service.register(
        "flaky",
        flaky,
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0),
    )
    result = service.invoke("flaky", None, idempotency_key="r-ok")

    assert result.status is Status.SUCCEEDED
    assert result.value == "ok"
    assert result.attempts == 3
    assert counter["n"] == 3


def test_retry_exhausted_lands_in_terminal_state(service) -> None:
    counter = {"n": 0}

    def always_fail(payload):
        counter["n"] += 1
        raise ValueError("always")

    service.register(
        "fail",
        always_fail,
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0),
    )
    result = service.invoke("fail", None, idempotency_key="r-exhaust")

    assert result.status is Status.FAILED_RETRY_EXHAUSTED
    assert result.terminal
    assert result.attempts == 3
    assert counter["n"] == 3
    assert result.error_type == "ValueError"
    assert "always" in (result.error_message or "")


def test_non_retryable_error_fails_immediately(service) -> None:
    counter = {"n": 0}

    def permanent(payload):
        counter["n"] += 1
        raise PermanentError("business rule")

    service.register(
        "perm",
        permanent,
        retry_policy=RetryPolicy(max_attempts=5, backoff_seconds=0),
    )
    result = service.invoke("perm", None, idempotency_key="r-perm")

    assert result.status is Status.FAILED_NON_RETRYABLE
    assert result.attempts == 1
    assert counter["n"] == 1
    assert result.error_type == "PermanentError"


def test_non_matching_exception_type_is_not_retried(service) -> None:
    counter = {"n": 0}

    def fail_with_keyerror(payload):
        counter["n"] += 1
        raise KeyError("nope")

    # 只把 RuntimeError 算作可重试，KeyError 直接终态
    policy = RetryPolicy(
        max_attempts=4, backoff_seconds=0, retry_on=(RuntimeError,)
    )
    service.register("typed", fail_with_keyerror, retry_policy=policy)
    result = service.invoke("typed", None, idempotency_key="r-typed")

    assert result.status is Status.FAILED_NON_RETRYABLE
    assert result.attempts == 1
    assert counter["n"] == 1


def test_timeout_exhausted_lands_in_timeout_terminal(service) -> None:
    def hang(payload):
        time.sleep(5)

    service.register(
        "hang",
        hang,
        retry_policy=RetryPolicy(
            max_attempts=2, timeout_seconds=0.2, backoff_seconds=0
        ),
    )
    t0 = time.time()
    result = service.invoke("hang", None, idempotency_key="r-timeout")
    elapsed = time.time() - t0

    assert result.status is Status.FAILED_TIMEOUT
    assert result.attempts == 2
    assert result.error_type == "TimeoutExceededError"
    assert elapsed < 2  # 不能被卡住的函数拖死


def test_stuck_call_does_not_block_other_calls(service) -> None:
    release = threading.Event()

    def hang(payload):
        release.wait(timeout=10)
        return "released"

    def quick(payload):
        return "quick"

    service.register("hang", hang)
    service.register("quick", quick)

    quick_results: list = []

    def call_quick() -> None:
        quick_results.append(
            service.invoke("quick", None, idempotency_key=f"q-{time.time_ns()}")
        )

    hang_thread = threading.Thread(
        target=lambda: service.invoke(
            "hang", None, idempotency_key="stuck-1"
        )
    )
    hang_thread.start()
    time.sleep(0.2)  # 确保卡住的调用先占用一个工作线程

    workers = [threading.Thread(target=call_quick) for _ in range(4)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=5)

    assert len(quick_results) == 4
    assert all(r.value == "quick" for r in quick_results)

    release.set()
    hang_thread.join(timeout=5)


def test_async_function_retries_and_succeeds(service) -> None:
    counter = {"n": 0}

    async def flaky_async(payload):
        counter["n"] += 1
        await asyncio.sleep(0.01)
        if counter["n"] < 2:
            raise RuntimeError("async transient")
        return {"async": True}

    service.register(
        "flaky_async",
        flaky_async,
        retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0),
    )
    result = service.invoke(
        "flaky_async", None, idempotency_key="r-async"
    )

    assert result.status is Status.SUCCEEDED
    assert result.value == {"async": True}
    assert result.attempts == 2


def test_retry_logs_include_key_attempts_and_final_status(
    service, caplog_runtime
) -> None:
    counter = {"n": 0}

    def flaky(payload):
        counter["n"] += 1
        if counter["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    service.register(
        "flaky",
        flaky,
        retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0),
    )
    service.invoke("flaky", None, idempotency_key="r-log")

    messages = [r.getMessage() for r in caplog_runtime.records]
    assert any("attempt=1/2" in m and "r-log" in m for m in messages)
    assert any("next_attempt=2/2" in m and "r-log" in m for m in messages)
    finished = [m for m in messages if m.startswith("invoke finished")]
    assert finished
    assert "r-log" in finished[0]
    assert "status=succeeded" in finished[0]
    assert "attempts=2" in finished[0]


def test_validation_error_is_distinct_and_not_retried(service) -> None:
    counter = {"n": 0}

    def fn(payload):
        counter["n"] += 1
        return payload

    def validator(payload):
        if not isinstance(payload, int):
            raise ValueError("must be int")

    service.register("validated", fn, validator=validator)

    try:
        service.invoke("validated", "not-int", idempotency_key="r-valid")
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValidationError")

    assert counter["n"] == 0

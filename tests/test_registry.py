"""同步/异步函数注册、函数不存在等注册表行为。"""

import pytest

from runtime.errors import FunctionNotFoundError
from runtime.models import RetryPolicy
from runtime.registry import FunctionRegistry


async def _async_identity(x):
    return x


def _sync_identity(x):
    return x


def test_register_sync_and_async() -> None:
    reg = FunctionRegistry()
    reg.register("sync_fn", _sync_identity)
    reg.register("async_fn", _async_identity)

    assert "sync_fn" in reg
    assert reg.get("sync_fn").is_async is False
    assert reg.get("async_fn").is_async is True


def test_register_as_decorator() -> None:
    reg = FunctionRegistry()

    @reg.register("decorated")
    def fn(x):
        return x + 1

    assert reg.get("decorated").func is fn
    assert fn(1) == 2


def test_register_with_policy() -> None:
    reg = FunctionRegistry()
    policy = RetryPolicy(max_attempts=5, timeout_seconds=2.0)
    reg.register("p", _sync_identity, retry_policy=policy)
    assert reg.get("p").retry_policy == policy


def test_get_unknown_raises_distinct_error() -> None:
    reg = FunctionRegistry()
    with pytest.raises(FunctionNotFoundError) as exc:
        reg.get("missing")
    assert exc.value.name == "missing"


def test_duplicate_registration_rejected() -> None:
    reg = FunctionRegistry()
    reg.register("dup", _sync_identity)
    with pytest.raises(ValueError):
        reg.register("dup", _async_identity)

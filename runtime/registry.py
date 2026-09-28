"""函数注册表：按名称注册同步/异步函数及参数校验器。"""

from __future__ import annotations

import inspect

from runtime.errors import FunctionNotFoundError
from runtime.models import AsyncFn, FunctionSpec, RetryPolicy, SyncFn, Validator


class FunctionRegistry:
    def __init__(self) -> None:
        self._functions: dict[str, FunctionSpec] = {}

    def register(
        self,
        name: str,
        func: SyncFn | AsyncFn,
        *,
        validator: Validator | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("function name must be a non-empty string")
        if not callable(func):
            raise TypeError(f"function '{name}' must be callable")
        if validator is not None and not callable(validator):
            raise TypeError(f"validator for '{name}' must be callable")
        self._functions[name] = FunctionSpec(
            name=name,
            func=func,
            is_async=inspect.iscoroutinefunction(func),
            validator=validator,
            retry_policy=retry_policy or RetryPolicy(),
        )

    def get(self, name: str) -> FunctionSpec:
        spec = self._functions.get(name)
        if spec is None:
            raise FunctionNotFoundError(f"function not registered: {name!r}")
        return spec

    def is_registered(self, name: str) -> bool:
        return name in self._functions

    def list_functions(self) -> list[str]:
        return sorted(self._functions)

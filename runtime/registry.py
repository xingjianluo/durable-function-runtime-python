"""函数注册表：按名称注册同步/异步函数。"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from runtime.errors import FunctionNotFoundError
from runtime.models import FunctionSpec, RetryPolicy


class FunctionRegistry:
    def __init__(self) -> None:
        self._functions: dict[str, FunctionSpec] = {}

    def register(
        self,
        name: str,
        func: Callable[..., Any] | None = None,
        *,
        validator: Callable[[Any], None] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> Callable[..., Any]:
        """注册函数；可作为装饰器或普通函数使用。"""

        def _do_register(fn: Callable[..., Any]) -> Callable[..., Any]:
            if not name:
                raise ValueError("function name must not be empty")
            if name in self._functions:
                raise ValueError(f"function already registered: {name}")
            spec = FunctionSpec(
                name=name,
                func=fn,
                is_async=inspect.iscoroutinefunction(fn),
                validator=validator,
                retry_policy=retry_policy or RetryPolicy(),
            )
            self._functions[name] = spec
            return fn

        if func is None:
            # 作为装饰器使用：@registry.register("name")
            return _do_register
        return _do_register(func)

    def get(self, name: str) -> FunctionSpec:
        """按名称获取函数描述，不存在则抛 FunctionNotFoundError。"""
        try:
            return self._functions[name]
        except KeyError:
            raise FunctionNotFoundError(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._functions

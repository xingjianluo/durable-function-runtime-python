"""轻量异步测试辅助：不依赖 pytest-asyncio。"""

from __future__ import annotations

import asyncio
import functools


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper

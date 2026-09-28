"""共享测试夹具：每个用例独立的持久化文件与运行时。"""

import logging

import pytest

from runtime.executor import Executor
from runtime.service import RuntimeService
from runtime.store import InvocationStore


@pytest.fixture
def service(tmp_path):
    store = InvocationStore(str(tmp_path / "invocations.json"))
    svc = RuntimeService(store, Executor(max_workers=8))
    yield svc
    svc.shutdown()


@pytest.fixture
def caplog_runtime(caplog):
    caplog.set_level(logging.INFO, logger="runtime")
    return caplog

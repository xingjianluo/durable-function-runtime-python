"""HTTP 接口集成：错误分类在 HTTP 层仍可区分，重复调用只执行一次。"""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_DB", str(tmp_path / "api.db"))
    import app as app_module

    importlib.reload(app_module)
    app_module.flaky.calls = 0  # 每个用例重置演示函数计数
    with TestClient(app_module.app) as c:
        yield c
    app_module.rt.shutdown()


def test_root_lists_functions(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert set(resp.json()["functions"]) == {"echo", "flaky", "strict"}


def test_invoke_success_and_dedup(client):
    body = {"function_name": "echo", "payload": {"a": 1}, "idempotency_key": "api-1"}
    r1 = client.post("/invoke", json=body)
    assert r1.status_code == 200
    assert r1.json()["status"] == "SUCCEEDED"

    r2 = client.post("/invoke", json=body)
    data2 = r2.json()
    assert data2["status"] == "SUCCEEDED"
    assert data2["result"] == {"echo": {"a": 1}}

    stored = client.get("/invocations/api-1").json()
    assert stored["attempts"] == 1


def test_function_not_found_http(client):
    r = client.post("/invoke", json={
        "function_name": "ghost", "payload": None, "idempotency_key": "api-404"})
    assert r.status_code == 404
    assert r.json()["error_type"] == "function_not_found"


def test_function_error_retries_then_fails_http(client):
    # flaky 演示函数第三次才成功；用不同计数方式验证重试耗尽需要一个一直失败的函数
    from runtime import NonRetryableError

    app_mod = importlib.import_module("app")

    def always_bad(payload):
        raise RuntimeError("boom")

    from runtime import RetryPolicy
    app_mod.rt.register("always_bad", always_bad,
                        retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0))
    r = client.post("/invoke", json={
        "function_name": "always_bad", "payload": None, "idempotency_key": "api-fail"})
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "FAILED"
    assert data["error"]["error_type"] == "retries_exhausted"


def test_non_retryable_http(client):
    r = client.post("/invoke", json={
        "function_name": "strict", "payload": 1, "idempotency_key": "api-fatal"})
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "FAILED"
    assert data["error"]["error_type"] == "non_retryable_error"
    assert data["attempts"] == 1

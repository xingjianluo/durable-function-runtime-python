"""HTTP 层：错误分类与终态结果返回。"""

import pytest
from fastapi.testclient import TestClient

from runtime.api import create_app


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def test_invoke_success_and_dedup(client) -> None:
    body = {"name": "echo", "payload": {"x": 1}, "idempotency_key": "api-1"}
    r1 = client.post("/invoke", json=body)
    r2 = client.post("/invoke", json=body)

    assert r1.status_code == 200
    data = r1.json()
    assert data["status"] == "succeeded"
    assert data["value"] == {"echo": {"x": 1}}
    assert data["attempts"] == 1
    assert r2.json() == data


def test_function_not_found_returns_404(client) -> None:
    r = client.post(
        "/invoke",
        json={"name": "ghost", "payload": None, "idempotency_key": "api-2"},
    )
    assert r.status_code == 404
    assert r.json()["error_type"] == "function_not_found"


def test_validation_error_returns_400(client) -> None:
    r = client.post(
        "/invoke",
        json={"name": "slow", "payload": 123, "idempotency_key": "api-3"},
    )
    assert r.status_code == 400
    assert r.json()["error_type"] == "validation_error"


def test_non_retryable_terminal_in_body(client) -> None:
    r = client.post(
        "/invoke",
        json={
            "name": "permanent_fail",
            "payload": None,
            "idempotency_key": "api-4",
            "max_attempts": 3,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "failed_non_retryable"
    assert data["attempts"] == 1
    assert data["error_type"] == "PermanentError"


def test_retry_exhausted_terminal_in_body(client) -> None:
    r = client.post(
        "/invoke",
        json={
            "name": "flaky",
            "payload": {"fail_until": 99},
            "idempotency_key": "api-5",
            "max_attempts": 3,
            "backoff_seconds": 0,
        },
    )
    data = r.json()
    assert data["status"] == "failed_retry_exhausted"
    assert data["attempts"] == 3


def test_get_result_endpoint(client) -> None:
    client.post(
        "/invoke",
        json={"name": "echo", "payload": 1, "idempotency_key": "api-6"},
    )
    r = client.get("/results/api-6")
    assert r.status_code == 200
    assert r.json()["value"] == {"echo": 1}

    missing = client.get("/results/unknown")
    assert missing.status_code == 404
    assert missing.json()["error_type"] == "result_not_found"


def test_functions_listed(client) -> None:
    r = client.get("/functions")
    assert r.status_code == 200
    assert "echo" in r.json()["functions"]

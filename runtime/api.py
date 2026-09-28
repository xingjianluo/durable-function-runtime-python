"""HTTP 接口：注册演示函数并暴露调用/查询端点。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from runtime.errors import FunctionNotFoundError, ValidationError
from runtime.models import RetryPolicy
from runtime.service import RuntimeService


class InvokeRequest(BaseModel):
    name: str = Field(..., description="已注册函数名")
    payload: Any = None
    idempotency_key: str = Field(..., description="幂等键")
    max_attempts: int | None = Field(None, ge=1)
    backoff_seconds: float | None = Field(None, ge=0)
    timeout_seconds: float | None = Field(None, gt=0)


def create_app(service: RuntimeService | None = None) -> FastAPI:
    app = FastAPI(title="durable-function-runtime", version="0.1.0")
    svc = service or RuntimeService()
    app.state.service = svc
    _register_demo_functions(svc)

    @app.post("/invoke")
    def invoke(req: InvokeRequest) -> dict[str, Any]:
        try:
            policy = None
            if req.max_attempts is not None:
                policy = RetryPolicy(
                    max_attempts=req.max_attempts,
                    backoff_seconds=req.backoff_seconds or 0.0,
                    timeout_seconds=req.timeout_seconds,
                )
            result = svc.invoke(
                req.name,
                req.payload,
                idempotency_key=req.idempotency_key,
                retry_policy=policy,
            )
        except FunctionNotFoundError as exc:
            return _error(404, "function_not_found", str(exc))
        except ValidationError as exc:
            return _error(400, "validation_error", str(exc))
        return result.to_dict()

    @app.get("/results/{idempotency_key}")
    def get_result(idempotency_key: str) -> dict[str, Any]:
        result = svc.get_result(idempotency_key)
        if result is None:
            return _error(404, "result_not_found", idempotency_key)
        return result.to_dict()

    @app.get("/functions")
    def list_functions() -> dict[str, Any]:
        return {"functions": sorted(svc.registry._functions)}  # noqa: SLF001

    return app


def _error(status_code: int, error_type: str, message: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error_type": error_type, "error_message": str(message)},
    )


def _register_demo_functions(svc: RuntimeService) -> None:
    """注册一组演示函数，便于本地验证。"""
    import asyncio

    def echo(payload: Any) -> Any:
        return {"echo": payload}

    async def async_echo(payload: Any) -> Any:
        await asyncio.sleep(0.01)
        return {"async_echo": payload}

    call_counts: dict[str, int] = {}

    def flaky(payload: Any) -> Any:
        # 前两次抛可重试错误，第三次成功；用 payload.fail_until 可定制
        fail_until = 2
        if isinstance(payload, dict) and "fail_until" in payload:
            fail_until = payload["fail_until"]
        key = str(payload)
        n = call_counts.setdefault(key, 0) + 1
        call_counts[key] = n
        if n <= fail_until:
            raise RuntimeError(f"transient failure #{n}")
        return {"ok": True, "attempts": n}

    def permanent_fail(payload: Any) -> Any:
        from runtime.errors import PermanentError

        raise PermanentError("business rule violated, do not retry")

    def slow(payload: Any) -> Any:
        import time

        seconds = payload.get("seconds", 5) if isinstance(payload, dict) else 5
        time.sleep(seconds)
        return {"slept": seconds}

    def _require_dict(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")

    svc.register("echo", echo)
    svc.register("async_echo", async_echo)
    svc.register("flaky", flaky)
    svc.register("permanent_fail", permanent_fail)
    svc.register(
        "slow",
        slow,
        validator=_require_dict,
        retry_policy=RetryPolicy(timeout_seconds=1.0),
    )

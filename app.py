"""FastAPI HTTP 入口：注册演示函数并暴露调用接口。"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from runtime import (
    FunctionRuntime,
    FunctionNotFoundError,
    IdempotencyKeyConflictError,
    InvalidPayloadError,
    NonRetryableError,
    RetryPolicy,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

DB_PATH = os.environ.get("RUNTIME_DB", "runtime.db")
rt = FunctionRuntime(db_path=DB_PATH)
app = FastAPI(title="Durable Function Runtime")


# ---------- 错误分类：函数不存在 / 入参非法 / 键冲突 各自独立响应 ----------

@app.exception_handler(FunctionNotFoundError)
async def _handle_not_found(_: Request, exc: FunctionNotFoundError):
    return JSONResponse(status_code=404, content=exc.to_dict())


@app.exception_handler(InvalidPayloadError)
async def _handle_invalid(_: Request, exc: InvalidPayloadError):
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.exception_handler(IdempotencyKeyConflictError)
async def _handle_conflict(_: Request, exc: IdempotencyKeyConflictError):
    return JSONResponse(status_code=409, content=exc.to_dict())


@app.exception_handler(Exception)
async def _handle_unknown(_: Request, exc: Exception):
    logging.getLogger("app").exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={
            "error_type": "internal_error",
            "message": f"{type(exc).__name__}: {exc}",
            "retryable": False,
        },
    )


# ---------- 演示函数 ----------

def echo(payload):
    return {"echo": payload}


def flaky(payload):
    """前两次抛可重试错误，第三次成功。"""
    flaky.calls += 1
    if flaky.calls < 3:
        raise RuntimeError(f"temporary failure #{flaky.calls}")
    return {"succeeded_on_attempt": flaky.calls, "payload": payload}


flaky.calls = 0


def strict(payload):
    """显式不可重试的业务错误。"""
    raise NonRetryableError(f"permanent business rejection for {payload!r}")


rt.register(
    "echo",
    echo,
    retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0),
)
rt.register(
    "flaky",
    flaky,
    retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.05, timeout_seconds=5),
)
rt.register(
    "strict",
    strict,
    retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.05),
)


# ---------- HTTP 接口 ----------

class InvokeRequest(BaseModel):
    function_name: str = Field(..., examples=["echo"])
    payload: object = None
    idempotency_key: str = Field(..., min_length=1)


@app.get("/")
def read_root():
    return {
        "service": "durable-function-runtime",
        "functions": rt.registry.list_functions(),
    }


@app.post("/invoke")
async def invoke(req: InvokeRequest):
    record = await rt.invoke(req.function_name, req.payload, req.idempotency_key)
    # 终态非 SUCCEEDED 仍返回记录正文（含 error 分类），用 202 表达“已处理但未成功”
    status_code = 200 if record.status.value == "SUCCEEDED" else 202
    return JSONResponse(status_code=status_code, content=record.to_dict())


@app.get("/invocations/{idempotency_key}")
def get_invocation(idempotency_key: str):
    record = rt.store.load(idempotency_key)
    if record is None:
        return JSONResponse(
            status_code=404,
            content={"error_type": "invocation_not_found", "idempotency_key": idempotency_key},
        )
    return record.to_dict()

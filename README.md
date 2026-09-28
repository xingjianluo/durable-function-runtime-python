# Durable Function Runtime（Python）

一个本地可运行的**函数执行运行时**：按注册名调用用户函数（同步/异步均可），
并通过幂等键、重试与超时控制让「重复调用、失败重试、并发触发」下的执行状态可预期。

## 能力概览

- **注册与调用**：按名称注册同步函数或 `async` 函数，可为每个函数配置参数校验器与重试策略。
- **错误三分类，互不混淆**：
  - `function_not_found`：函数名未注册（HTTP 404，不可重试）；
  - `invalid_payload`：幂等键缺失或入参校验失败（HTTP 400，不可重试）；
  - `function_execution_error` / `non_retryable_error` / `timeout`：函数执行阶段的错误（HTTP 202 返回终态记录，内含分类字段）。
- **幂等去重**：同一个幂等键只真正执行一次；并发重复请求由单个领导者执行，其余等待并拿到**同一份结果**。
- **跨进程持久化**：终态记录落盘到 SQLite，进程重启后用同一幂等键重复触发仍命中旧结果，不会再次执行。
- **重试与超时**：区分可重试错误与不可重试错误；重试次数耗尽 → `FAILED`，总截止时间耗尽 → `TIMED_OUT`；同步函数在独立守护线程中执行并带超时，一个卡住的调用不会阻塞事件循环或拖垮其他调用。

## 执行流程

```
invoke(function_name, payload, idempotency_key)
  │
  ├─ 缺少 idempotency_key ──────────────► InvalidPayloadError
  ├─ 函数未注册 ────────────────────────► FunctionNotFoundError
  │
  ├─ 存储中已存在该键的【终态记录】──────► 直接返回同一份记录（去重命中，含重启后）
  │
  ├─ 本进程已有同键调用在执行 ───────────► 等待领导者完成，返回其终态记录
  │
  └─ 当前请求成为领导者：
        1. 执行入参校验，失败 ──────────► InvalidPayloadError（不执行函数）
        2. 写入 RUNNING 记录（SQLite，作为幂等凭证）
        3. 执行器按 RetryPolicy 循环尝试：
             成功 ─────────────────────► SUCCEEDED（终态，只出现一次）
             不可重试错误 ─────────────► FAILED（终态，attempts=1）
             可重试错误且还有次数 ──────► 退避后重试（RETRYING）
             尝试次数用尽 ─────────────► FAILED / retries_exhausted（终态）
             总 deadline 用尽 ─────────► TIMED_OUT / timeout_exhausted（终态）
        4. 每次尝试都持久化 attempts/status/error，并唤醒所有等待者
```

等待者通过「事件 + 轮询终态记录」两种方式被唤醒，因此同一台机器上多个进程
同时用一个幂等键发起调用时，也只有抢到插入的进程真正执行。

## 幂等规则

- 幂等键是必填字符串，作用域为整个运行时（所有函数共享一个键空间）。
- 同一幂等键**与函数名绑定**；用它去调用另一个函数会得到 `idempotency_key_conflict`（HTTP 409）。
- 终态只有三种：`SUCCEEDED`、`FAILED`、`TIMED_OUT`。终态记录不可变、可重复读取。
- 进程异常退出时遗留的非终态（RUNNING/RETRYING）记录，会在下次启动时清理；
  这意味着「未完成」的调用允许重新执行，而「已完成」的结果永久保留。
- 入参校验只在首次真正执行时进行；重复键直接返回旧终态，不会因新 payload 再校验。

## 重试与超时规则

`RetryPolicy`（每个函数可单独配置）：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `max_attempts` | 最大尝试次数（含首次），1 表示不重试 | 3 |
| `backoff_seconds` | 线性退避基数，第 n 次失败后等待 `n * backoff_seconds` | 0 |
| `timeout_seconds` | 单次尝试超时；超时错误可重试 | 无限制 |
| `total_deadline_seconds` | 整体截止时间（含退避等待），耗尽后终态为 TIMED_OUT | 无限制 |

错误是否重试：

- 函数抛普通异常 → 包装为 `function_execution_error`，**可重试**；
- 单次尝试超时 → `timeout`，**可重试**；
- 函数显式 `raise NonRetryableError(...)`（从 `runtime` 导入）→ **不可重试**，立即 FAILED；
- `max_attempts` 用尽 → 终态 `FAILED`，`error.error_type = retries_exhausted`，
  其中 `last_error` 保留最后一次失败原因，`attempts` 为实际尝试次数；
- `total_deadline_seconds` 用尽 → 终态 `TIMED_OUT`，`error.error_type = timeout_exhausted`。

同步函数每次尝试运行在**独立守护线程**中（`join(timeout)` 控制超时），
不占用 asyncio 共享线程池；卡住的线程即使无法杀死，也只会留在后台，
不阻塞事件循环与其他调用。

## 目录结构

```
runtime/
  errors.py    # 错误类型与分类（error_type / retryable）
  models.py    # InvocationStatus / RetryPolicy / FunctionSpec / InvocationRecord
  registry.py  # 函数注册表
  store.py     # SQLite 持久化（终态保留、重启清理非终态）
  executor.py  # 执行器：尝试循环、退避、超时、线程隔离
  service.py   # FunctionRuntime 门面：并发去重 + 校验 + 编排
app.py         # FastAPI HTTP 入口与演示函数
main.py        # uvicorn 启动脚本
tests/         # 单测与 HTTP 集成测试
```

## HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /` | 服务信息与已注册函数列表 |
| `POST /invoke` | 调用函数，body：`{"function_name", "payload", "idempotency_key"}` |
| `GET /invocations/{idempotency_key}` | 按幂等键查询调用记录与终态 |

状态码：`200` 成功；`202` 已落终态但非成功（正文含 error 分类）；
`404` 函数/记录不存在；`400` 入参错误；`409` 幂等键与函数绑定冲突；`500` 未预期内部错误。

## 本地验证方法

需要 Python 3.14+，依赖通过 [uv](https://docs.astral.sh/uv/) 管理。

### 1. 运行单元测试 / 集成测试

```bash
uv run pytest                      # 全部 23 个用例
uv run pytest -k concurrency       # 只看并发场景
uv run pytest -k idempotency       # 只看重复调用/重启恢复
uv run pytest -k retry             # 只看重试与超时耗尽
```

测试日志会打印**幂等键、当前尝试次数（重试次数）、最终状态**，例如：

```
INFO runtime.service  | idem=conc-fail | invoke accepted ...
INFO runtime.executor | idem=conc-fail | invoke start attempt=1/3 status=RUNNING
WARNING ... attempt=1/3 retryable=True error=RuntimeError: downstream outage
INFO runtime.executor | idem=conc-fail | invoke end attempts=3 status=FAILED reason=retries_exhausted
INFO runtime.service  | idem=conc-fail | invoke terminal status=FAILED attempts=3
```

### 2. 启动 HTTP 服务手工验证

```bash
uv run python main.py
# 或自定义端口与数据库路径：
RUNTIME_DB=runtime.db uv run uvicorn app:app --host 127.0.0.1 --port 8000
```

```bash
# 首次调用
curl -s -X POST http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"function_name":"echo","payload":{"a":1},"idempotency_key":"demo-1"}'

# 用同一个幂等键重复触发（attempts 仍为 1，不重新执行）
curl -s -X POST http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"function_name":"echo","payload":{"a":1},"idempotency_key":"demo-1"}'

# 内置 flaky 函数：前两次失败、第三次成功，可观察 attempts=3
curl -s -X POST http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"function_name":"flaky","payload":"hi","idempotency_key":"demo-2"}'

# 不可重试错误：attempts=1，status=FAILED
curl -s -X POST http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"function_name":"strict","payload":1,"idempotency_key":"demo-3"}'

# 查询终态
curl -s http://127.0.0.1:8000/invocations/demo-1
```

重启服务后再次用 `demo-1` 调用，会直接返回旧结果（`attempts=1`），
以此验证跨进程的幂等保证。数据库文件由 `RUNTIME_DB` 控制（默认 `runtime.db`）。

### 3. 在代码中直接使用

```python
import asyncio
from runtime import FunctionRuntime, NonRetryableError, RetryPolicy

rt = FunctionRuntime(db_path="runtime.db")
def charge(p):
    return {"charged": p["amount"]}

def require_amount(p):
    if "amount" not in p:
        raise ValueError("amount required")

rt.register(
    "charge",
    charge,
    validator=require_amount,
    retry_policy=RetryPolicy(max_attempts=3, backoff_seconds=0.1,
                             timeout_seconds=5, total_deadline_seconds=30),
)

record = asyncio.run(rt.invoke("charge", {"amount": 100}, "order-123"))
print(record.status.value, record.attempts, record.result or record.error)
```

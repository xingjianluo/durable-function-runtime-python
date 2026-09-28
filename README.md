# Durable Function Runtime (Python)

一个本地可运行的函数执行运行时：按注册名调用用户函数（同步/异步均可），通过幂等键保证
重复触发只真正执行一次，支持失败重试、超时隔离与明确的失败终态。进程重启后幂等保证依然有效。

## 目录结构

```
runtime/
  errors.py    # 错误分类：函数不存在 / 入参校验失败 / 执行错误 / 重试与超时
  models.py    # Status 终态枚举、RetryPolicy、FunctionSpec、CallResult
  registry.py  # 函数注册表（同步/异步自动识别）
  store.py     # 幂等记录存储：JSON 原子落盘 + 进程内事件去重
  executor.py  # 线程池执行器：单次超时，卡住的调用互不影响
  service.py   # 编排：去重 → 校验 → 执行 → 重试 → 终态落盘
  api.py       # FastAPI HTTP 接口与演示函数
tests/         # 注册表 / 幂等 / 重试超时 / HTTP 四组测试
main.py        # 本地启动入口
```

## 执行流程

一次 `invoke(name, payload, idempotency_key=...)` 的处理顺序：

1. **函数查找**：按注册名取函数描述，不存在 → 抛 `FunctionNotFoundError`
   （HTTP 层映射为 `404 function_not_found`）。该类错误不占用幂等键。
2. **入参校验**：若注册时提供了 `validator(payload)`，校验失败 → 抛 `ValidationError`
   （HTTP 层映射为 `400 validation_error`），同样不执行、不落盘。
3. **幂等去重**（`InvocationStore.begin`）：
   - 该键已有**终态结果**（含成功与各类失败终态）：直接返回同一份 `CallResult`，不再执行；
   - 该键**正在执行中**：在进程内共享事件上等待，最终拿到领导者写入的同一份结果；
   - 该键首次到达：当前线程成为领导者，继续下一步。
4. **执行与重试**（受 `RetryPolicy` 控制）：每次尝试交给独立工作线程，
   同步函数直接执行，异步函数在工作线程内用独立事件循环 `asyncio.run` 执行。
5. **终态落盘**：执行结论以 JSON 原子写（临时文件 + `os.replace` + `fsync`）持久化，
   并唤醒所有等待同一幂等键的请求。**进程重启后**重新读取文件，重复键依然不会再次执行。

### 终态（`runtime.models.Status`）

| 状态 | 含义 |
| --- | --- |
| `succeeded` | 成功，`value` 为函数返回值 |
| `failed_retry_exhausted` | 可重试错误在 `max_attempts` 次尝试后仍失败 |
| `failed_non_retryable` | 命中不可重试错误，首次失败即终态 |
| `failed_timeout` | 最后一次尝试仍超过单次超时上限 |

`CallResult` 同时记录 `attempts`（实际尝试次数）、`error_type`（原始异常类名）与
`error_message`，因此失败原因可以被区分，而不是混成一种错误。

## 幂等规则

- 幂等键由调用方提供，非空；**同键 = 同一次调用**，终态结果（无论成功或失败）只产生一次。
- 重复触发：返回首次结果，连首次传入的 `payload` 也不会被重新使用。
- 并发触发：同一进程内由键级领导锁 + 共享事件保证只有一个线程执行，其余线程等待并拿到
  同一份结果。
- 进程重启：结果已原子落盘（默认 `.runtime/invocations.json`），重启后同键直接复用。
- 不同幂等键之间完全独立。

## 重试与超时规则

`RetryPolicy(max_attempts, backoff_seconds, timeout_seconds, retry_on)`：

- `max_attempts`：总尝试次数（含首次），`1` 表示不重试；默认 `3`。
- `backoff_seconds`：尝试之间的固定退避；默认 `0`。
- `timeout_seconds`：**单次尝试**超时；超时放弃等待该次尝试（后台线程不被 join），
  还有剩余次数则重试，否则落为 `failed_timeout`。`None` 表示不限制。
- `retry_on`：异常类型元组，默认 `(Exception,)`。不属于可重试类型的异常首次失败即
  落为 `failed_non_retryable`。
- 用户函数可抛出 `runtime.errors.PermanentError` 显式声明“永不重试”，无论策略如何配置。

**故障隔离**：执行发生在有界线程池中（默认 8 个工作线程）。一个卡住的函数只占用它自己的
工作线程，调用方在超时后立即拿到终态，其他调用在其余线程上照常执行（见
`tests/test_retry.py::test_stuck_call_does_not_block_other_calls`）。

## 编程方式使用

```python
from runtime.service import RuntimeService
from runtime.models import RetryPolicy
from runtime.errors import PermanentError, FunctionNotFoundError, ValidationError

svc = RuntimeService()  # 持久化默认写入 .runtime/invocations.json

@svc.register("add", retry_policy=RetryPolicy(max_attempts=3, timeout_seconds=5))
def add(payload: dict) -> int:
    if "x" not in payload:
        raise PermanentError("missing x")  # 不可重试
    return payload["x"] + 1

result = svc.invoke("add", {"x": 41}, idempotency_key="order-123")
print(result.status, result.value, result.attempts)
# Status.SUCCEEDED 42 1

# 重复 / 并发调用使用同一个幂等键，只执行一次，拿到同一份结果
svc.invoke("add", {"x": 41}, idempotency_key="order-123")
```

带参数校验的注册：

```python
def require_int(payload):
    if not isinstance(payload, int):
        raise ValueError("payload must be int")

svc.register("only_int", lambda p: p * 2, validator=require_int)
# 校验失败抛 ValidationError，与 FunctionNotFoundError / 函数内部异常互相区分
```

## HTTP 接口与本地验证

启动服务（注册了 `echo`、`async_echo`、`flaky`、`permanent_fail`、`slow` 演示函数）：

```bash
uv run main.py
# 打开 http://127.0.0.1:8000/docs
```

调用：

```bash
curl -s http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -d '{"name":"flaky","payload":{"fail_until":2},"idempotency_key":"demo-1",
       "max_attempts":3,"backoff_seconds":0}'
# {"idempotency_key":"demo-1","function_name":"flaky",
#  "status":"succeeded","value":{"ok":true,"attempts":3},"attempts":3,...}

# 重复触发：同键直接返回上面的同一份结果
curl -s http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -d '{"name":"flaky","payload":{"fail_until":2},"idempotency_key":"demo-1"}'

curl -s http://127.0.0.1:8000/results/demo-1   # 查询终态
curl -s http://127.0.0.1:8000/functions        # 列出已注册函数
```

错误分类示例：

```bash
# 函数不存在 -> 404 {"error_type":"function_not_found",...}
curl -i -s http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"name":"ghost","idempotency_key":"e1"}'

# 入参校验失败 -> 400 {"error_type":"validation_error",...}
curl -i -s http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"name":"slow","payload":1,"idempotency_key":"e2"}'

# 重试耗尽 -> 200 携带终态 status=failed_retry_exhausted
curl -s http://127.0.0.1:8000/invoke -H 'Content-Type: application/json' \
  -d '{"name":"flaky","payload":{"fail_until":99},"idempotency_key":"e3",
       "max_attempts":3}'
```

## 运行测试

```bash
uv sync                                  # 安装含 dev 组在内的依赖
uv run pytest                            # 全部 28 个用例
uv run pytest -o log_cli=true            # 同时查看单测日志
```

测试覆盖：

- **重复调用**：同键只执行一次、失败终态同样去重、不同键独立；
- **并发触发**：8 个线程同时打同一幂等键，只有一个真正执行，全部拿到同一份结果；
- **重启恢复**：使用独立临时目录，新建 `RuntimeService` 实例模拟进程重启后同键不再执行；
- **重试耗尽**：可重试错误耗尽次数后落到 `failed_retry_exhausted`，
  `PermanentError` / 不匹配 `retry_on` 的异常落到 `failed_non_retryable`；
- **超时隔离**：超时耗尽落 `failed_timeout`，卡住的调用不阻塞其他调用；
- **错误区分**：`FunctionNotFoundError`、`ValidationError` 与函数内部异常分别断言；
- **日志**：用例断言日志中出现幂等键、`attempt=x/y` 重试次数与最终 `status`。

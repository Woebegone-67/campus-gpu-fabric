# Request isolation：定向验证记录

## 记录性质与边界

本文件是 **Codex 工具观察摘要及准确执行命令，不是保存的完整原始终端日志**。以下测试、静态检查与反证的输出仅返回到当时的工具调用，没有重定向到 `/tmp` 或其他日志文件；未发现或宣称存在可提供的原始日志副本。本次归档没有重新运行任何测试。

验证对象是独立工作树 `/Users/xz/Projects/campus-gpu-fabric/.worktrees/request-isolation`、分支 `work/request-isolation`，以 `c3fad1bac8a208b74de99b1c769dee28e771ebc5` 为基底的未提交修复。验证时仅修改了两个生产文件并新增一个测试文件：

- `src/campus_fabric/api/test_control/faults.py`
- `src/campus_fabric/domain/lifecycle.py`
- `tests/concurrency/test_fault_request_isolation.py`

所有下列命令的工作目录均为上述独立工作树。复用相邻冻结工作树已经安装的 Python/测试工具；`PYTHONPATH=src:.` 使本次 Python 执行导入独立工作树的修复源码。没有安装或升级依赖，没有更改正在执行正式负载的原工作树源码、配置或进程。

这是请求隔离、故障消费及 HTTP CP2 的定向回归，**不是正式吞吐测量，也不构成 CP4 通过**。工具输出中的临时测试数据库由 pytest 创建，与正式运行数据库隔离。

## 1. 修复版本：49 项定向测试

准确执行命令：

```sh
PYTHONPATH=src:. /Users/xz/Projects/campus-gpu-fabric/.worktrees/storage-architecture/.venv/bin/python -m pytest tests/concurrency/test_fault_request_isolation.py tests/concurrency/test_http_event_loop.py tests/domain/test_http_cp2.py -q
```

工具执行会话：`51141`。观察到进程退出码 `0`，输出摘要：

```text
.................................................                        [100%]
49 passed, 1 warning in 3.47s
```

唯一警告来自依赖：`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` 本次没有据此改变依赖。

新增的 11 个参数化测试实例实际覆盖：

- 无规则、禁用规则、剩余次数为 0 时，真实 SQLite 写事务保持期间，公共 capacity 与真实 job 详情都能返回。
- capacity 或 job 详情存在活动规则时，故障消费等待写锁，另一个公共查询与无效 Bearer 的 401 仍可完成；释放锁后只消费一次。
- 两个独立 Kernel/连接同时读到剩余次数为 1，最终只有一个消费成功。
- 初次读取后规则被替换、禁用、reset 清除或由另一消费者耗尽，必须按写事务中的当前规则重新检查。
- 无规则快照读取之后才提交的新配置，不影响已经在线性化点完成的检查，并且完整留给后续请求消费。

原有 HTTP CP2 用例同时覆盖故障模板匹配、鉴权不消耗规则、并发次数、延迟注入仅影响墙钟、重启持久化与 reset 等行为。

## 2. 静态检查

准确执行命令：

```sh
/Users/xz/Projects/campus-gpu-fabric/.worktrees/storage-architecture/.venv/bin/ruff check src/campus_fabric/api/test_control/faults.py src/campus_fabric/domain/lifecycle.py tests/concurrency/test_fault_request_isolation.py
```

观察到退出码 `0`，工具输出：

```text
All checks passed!
```

此外执行了以下只读差异检查命令：

```sh
git diff --check; git diff --stat; git diff -- src/campus_fabric/api/test_control/faults.py src/campus_fabric/domain/lifecycle.py; git status --short
```

`git diff --check` 未输出空白错误。后续 diff/status 输出确认：两个生产文件共 15 行增加、2 行删除，另有新增并发测试文件尚未跟踪；没有执行提交或推送。

## 3. 反证一：仅恢复原同步中间件

目的：区分“事件循环线程隔离”与“无规则只读路径”的作用。仅在单独测试进程内，用 `git show HEAD:...` 取得未修复中间件，在内存中恢复原 `intercept_fault`；保留当前修复版 `take_fault`。没有修改工作树文件。

准确执行命令：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/xz/Projects/campus-gpu-fabric/.worktrees/storage-architecture/.venv/bin/python - <<'PY'
import subprocess
import pytest
from campus_fabric import app
source = subprocess.run(['git', 'show', 'HEAD:src/campus_fabric/api/test_control/faults.py'], check=True, capture_output=True, text=True).stdout
baseline = {'__name__': 'baseline_faults'}
exec(compile(source, 'baseline_faults.py', 'exec'), baseline)
app.intercept_fault = baseline['intercept_fault']
code = pytest.main(['tests/concurrency/test_fault_request_isolation.py', '-q', '-k', 'test_active_fault_waits and capacity'])
assert code == 1, f'Expected old synchronous middleware to fail the real-lock regression, got {code}'
print('EXPECTED_REGRESSION_CONFIRMED: original synchronous middleware fails while patched no-fault check remains in place')
PY
```

工具执行会话：`62352`。观察到 pytest 的预期失败：

```text
FAILED tests/concurrency/test_fault_request_isolation.py::test_active_fault_waits_off_loop_without_blocking_other_public_reads[GET /v1/capacity]
AssertionError: Fault consumption blocked the event loop
1 failed, 10 deselected, 1 warning in 6.58s
EXPECTED_REGRESSION_CONFIRMED: original synchronous middleware fails while patched no-fault check remains in place
```

错误出现在新测试第 124 行：`assert not release.is_set()`。真实 SQLite 写锁尚未按测试流程释放时，原同步核验堵住事件循环；直到 5 秒 watchdog 释放锁后，另一个公共查询才完成。pytest 返回 `1`，符合反证脚本断言，因此外层脚本退出码为 `0`。这里的外层退出成功表示**原缺陷被测试捕获**，不是旧代码通过。

这一工具输出另有 `PytestAssertRewriteWarning`，说明反证脚本在启动 pytest 前已导入 `anyio`；它不属于正常 49 项回归中的警告，也不是该失败的原因。

## 4. 反证二：仅恢复原无条件写锁检查

目的：证明仅把故障核验移到线程池仍不足以让无故障的公共查询在其他写事务期间完成。仅在另一个独立测试进程内，用基底版本的 `LifecycleMixin.take_fault` 替换当前方法；保留修复版中间件的线程隔离。没有修改工作树文件。

准确执行命令：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/xz/Projects/campus-gpu-fabric/.worktrees/storage-architecture/.venv/bin/python - <<'PY'
import subprocess
import pytest
from campus_fabric.domain.lifecycle import LifecycleMixin
source = subprocess.run(['git', 'show', 'HEAD:src/campus_fabric/domain/lifecycle.py'], check=True, capture_output=True, text=True).stdout
baseline = {'__name__': 'baseline_lifecycle'}
exec(compile(source, 'baseline_lifecycle.py', 'exec'), baseline)
LifecycleMixin.take_fault = baseline['LifecycleMixin'].take_fault
code = pytest.main(['tests/concurrency/test_fault_request_isolation.py::test_inactive_fault_public_reads_finish_while_sqlite_writer_is_held[None]', '-q'])
assert code == 1, f'Expected old always-write fault check to fail the real-lock regression, got {code}'
print('EXPECTED_REGRESSION_CONFIRMED: original no-fault write lock fails while patched thread isolation remains in place')
PY
```

工具执行会话：`24399`。观察到 pytest 的预期失败：

```text
FAILED tests/concurrency/test_fault_request_isolation.py::test_inactive_fault_public_reads_finish_while_sqlite_writer_is_held[None]
AssertionError: Public reads waited for the unrelated writer
1 failed in 6.64s
EXPECTED_REGRESSION_CONFIRMED: original no-fault write lock fails while patched thread isolation remains in place
```

错误出现在新测试第 86 行：`assert not release.is_set()`。没有配置故障规则，但原 `take_fault` 仍获取写锁，公共 capacity/job 读取只能等到 watchdog 释放不相关写事务后完成。同样，pytest 的 `1` 是预期反证结果，外层脚本退出码 `0` 不代表原实现通过。

两次反证分别运行在独立 Python 进程，修改只存在于各自进程内；结束后不影响修复源码或其他测试。没有为了补日志重跑正常回归或反证，也没有由此推导正式性能改善比例。

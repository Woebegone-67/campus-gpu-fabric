# CP3 A：崩溃恢复、执行一致性与独立账本核验

## 交付范围

本分支 `work/a-cp3` 从 CP2 联合放行版本 `aa63fb6` 开始。CP2 的自动验收和双方人工确认已完成；本轮不修改它的历史回执，也不重置其运行实例。

A 的交付覆盖 CP3 八项要求中的三项：`CP3-CRASH-RECOVERY`、`CP3-CPU-EQUIVALENCE`、`CP3-INDEPENDENT-MODEL`。最终运行状态和构建身份保存在 `evidence/cp3/a_core_receipt.json`。该回执只代表 A 侧；`evidence/cp3/receipt.json` 继续列出其余联合门槛，不把未运行项写成通过。

## 实际修复：启动意图与进程创建之间的窗口

CPU 驱动先写持久启动意图，再创建子进程、记录 PID。若在中间崩溃，原记录可能没有 PID。旧实现无法区分“尚未产生进程”和“产生了进程但来不及写 PID”，因此只能持续隔离，执行无法恢复。

新执行采用 protocol 2 启动栅栏：每个 attempt 有独立文件锁；worker 在锁内检查签名取消记录，写入 started 标志，并在整个执行期间持有锁。恢复端只有在取到该锁且没有 started 标志时，才写入持久取消记录并确认未启动执行已经被隔离。尚未进入锁的旧 child 随后会读到取消记录并退出。已经启动、持锁或不能判断的旧执行仍需停妥确认，不提前释放资源。

锁、取消记录和启动记录绑定现有执行身份；控制端只有一个驱动持有实例锁。旧 protocol 记录保持原来的保守隔离语义。没有新增公开 API、DTO 字段或前端必填输入。

## 三条独立证据链

1. **真实崩溃窗口。** 在 SQLite COMMIT 前后分别终止分配、工作绑定租约、工作绑定任务，共六种事务窗口。重开数据库后只能看到整个旧状态或整个新状态；同键重试得到相同回执，不能重复占用或创建。执行端另测启动记录、进程启动、保存点、控制发送和恢复代次等窗口，保留原始观测。
2. **真实 CPU 结果。** 三组确定性输入均执行两次中断和接续；分别与全新不中断 worker 以及独立矩阵运算结果比较。还检查整型工件、owner 隔离、资源归还及服务重启。固定参考 CPU 程序的等价性不等于任意用户程序、GPU 或容器的恢复能力。
3. **独立有限模型。** 以另外实现的状态机核对生成命令和真实并发历史，检验原子资源账本、幂等、期限、故障和 owner 边界；并发操作枚举符合真实先后约束的串行化。负向控制主动破坏结果，要求判定器能拒绝。模型范围是有限的单节点整卡实例，不声称覆盖所有异构放置或所有线程交错。

## 复验

环境与锁文件沿用 README。完整入口：

```sh
uv sync --locked
npm --prefix web ci
make cp3-a
```

该入口先运行 CP0–CP2 回归及现有 B 集成检查，再顺序执行三个 CP3 注册验证器。专项复验：

```sh
uv run python tests/acceptance/cp3/cp3_crash_recovery.py --output evidence/cp3/crash_recovery_receipt.json
uv run python tests/acceptance/cp3/cp3_cpu_equivalence.py --output evidence/cp3/cpu_equivalence_receipt.json
uv run python tests/acceptance/cp3/cp3_independent_model.py --output evidence/cp3/independent_model_receipt.json
```

所有实例和端口均隔离；CPU/崩溃测试要求 macOS 或 Linux。源代码、配置、文档或提交身份改变会使旧回执过期，应在稳定构建上重跑。日志和原始检查记录在 `evidence/cp3/`；CI 上传对应 artifact。本分支不把回执里的本机进程号当成可复用运行资源。

外层超时先中断所属测试进程组，让验证器执行清理，再在宽限结束后强制终止；超时必定失败。若日志显示强制终止或主机故障，独立会话中的 CPU 子进程仍须按该临时实例的执行日志核对和清理，不得重置其他人的实例。正常测试会清理自身进程。

## 当前技术边界和下一步

- 真实 CPU 恢复针对同一主机上的服务进程崩溃。跨主机、主机重启或不能确认旧进程时，不自动认定旧执行已经消失。
- 保存点签名用于本机参考执行器工件完整性，不能描述成远程可信执行证明。reported、verified、resumed 仍是三件独立事实。
- 现有事件记录可检查状态转移和资源账本，但未携带重建整个权威数据库的全部输入。完整重放门槛必须另行给出输入来源、重建对象和逐字段对照，不能用资源计数相同替代完整状态重建。
- B 尚需在集成构建上提交前缀/重放、解释覆盖、封存策略实验、效果门槛及产品结算证据；明确分工见 `docs/handoff/CP3_A_TO_B.md`。CP3 联合通过前停在当前 checkpoint，不自动进入 CP4。

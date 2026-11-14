# CP2 独立恶劣分支审查

本记录由独立测试代理完成。测试实现与生产生命周期实现分工独立；只通过 typed command port、授权读取投影和提交事件检查领域行为。审查依据为冻结官方 OpenAPI、`docs/SEMANTICS.md`、`configs/semantics.json` 与状态不变量。确切截止顺序、两种预算计数、严格 checkpoint 失败策略等是团队已声明的语义选择，并非原题额外保证。

## 已执行的验证

2026-11-14，本工作树执行结果：

| 范围 | 入口 | 实际结果 |
|---|---|---|
| 领域恶劣历史与 32 连接竞争 | `tests/domain/test_cp2_adversarial.py`、`tests/concurrency/test_cp2_history.py` | 43 项通过，1.56 秒；见 `adversarial_junit.xml` |
| HTTP 抢占/恢复分支 | `tests/acceptance/cp2/cp2_preemption.py` | 7 组真实临时 loopback HTTP 旅程通过 |
| HTTP 节点故障 | `tests/acceptance/cp2/cp2_node_failure.py` | 3 组真实临时 loopback HTTP 旅程通过 |
| 时钟边界 | `tests/acceptance/cp2/cp2_clock_boundaries.py` | 7 项独立命令历史通过（含 timeout/recall 两种先后） |
| reset/旧代次隔离 | `tests/acceptance/cp2/cp2_reset_isolation.py` | 4 项独立命令历史通过 |
| 本代理新增 Python 文件 lint | `ruff check` | 通过 |

四个预注册入口均重新启动实验或重新执行测试，不通过读取旧 PASS 收据代替当前执行。它们记录运行前后源码身份，运行时源码变化则标记 STALE。收据分别为本目录下 `cp2_preemption_receipt.json`、`cp2_node_failure_receipt.json`、`cp2_clock_boundaries_receipt.json`、`cp2_reset_isolation_receipt.json`。后续源码变化需重新执行入口，不能继承此处的 PASS。

复现命令（项目依赖安装后，在项目根目录执行）：

```sh
uv run pytest -q tests/domain/test_cp2_adversarial.py tests/concurrency/test_cp2_history.py
uv run python tests/acceptance/cp2/cp2_preemption.py
uv run python tests/acceptance/cp2/cp2_node_failure.py
uv run python tests/acceptance/cp2/cp2_clock_boundaries.py
uv run python tests/acceptance/cp2/cp2_reset_isolation.py
```

本次实际测试使用既有 Python 3.12 虚拟环境并设置 `PYTHONPATH=src`；HTTP 子进程使用同一解释器。各入口的临时数据库、端口及随机凭据独立，不访问 CP1 双人验收实例。

## 发现并推动修复的实质问题

1. **恢复/重试的执行命令类型不匹配。** `_dispatch` 向 `ExecutorCommand.job` 传入 `JobSpecification`，契约实际要求 `JobCreateParameters`；首次恢复或重试抛出 Pydantic 异常，事务不能完成。生产实现改为正确的 typed payload，恢复和重试回归通过。
2. **已失败代次的不同 ID 回调可以延后重试。** t=0 失败后等待 t=1 重试，t=0.5 同 attempt 的另一个 failure 回调重新安排 t=1.5；重复发送可持续推迟。新增 `test_distinct_late_failure_cannot_postpone_already_queued_retry`，实现拒绝已结束且无物理活跃执行的代次消息。
3. **已确认停止的旧回调可以破坏待恢复工作。** 收回截止后 job 为 Preempted，但旧 attempt 的新 failure 回调仍被接受并将其改为 Failed。新增 `test_callback_after_confirmed_interruption_does_not_fail_suspended_work`；修复后原工作仍可合法恢复。
4. **reset 后重命名节点可绕过未确认停机隔离。** CPU 协议中尚未收到实际 stop 收据，reset 清空业务数据后，使用新 node ID 重新登记同一 GPU，旧实现只按 node ID 隔离而允许重新获配。新增 `test_cpu_protocol_reset_quarantine_cannot_be_bypassed_by_renaming_node`；实现保留 retired physical GPU 身份，并按节点或物理卡交集隔离。该回归曾实际观察到错误的 Allocated，修复后保持 Pending。

代码审查还指出首个 CPU started 回调可能对 `checkpoint=None` 调用 `.get`；实现补充空值处理。新增 CPU 协议测试已覆盖首次 started。

HTTP 审查代理另外发现 CPU 停止组合问题：recall → ready 请求停止 → cancel/timeout 等待终结 → stopped 可能只终结 job，却把 allocation 留在 PreemptRequested 并保留占用。本代理新增 5 项组合协议回归，覆盖 cancel、timeout 在 recall 前后、显式 release 的终态优先；根代理修复后首次执行全部通过。测试同时要求延迟确认前的取消/释放请求产生真实提交事件，不能报为 noop；停止确认前必须继续持有资源。该问题的初始复现归属于 HTTP 审查，不能算成本代理独立发现。

测试本身也作了两项校准：不强行规定合法 409 必须使用 `stage_conflict` 而不能使用 `lease_unavailable`；不要求原样返回原始幂等回执时新增 `idempotency_replayed` 标志。校准后仍核对同一事件集合与仅一次资源归还。这些属于测试过度约束，不列为产品缺陷。

## 覆盖的关键事实

- ready、failed、timeout、恰好截止及 expiry 同刻；ready 报告与 artifact verified / execution applied 保持不同事实。
- 通知立即禁止新放置和新 job；连续重叠收回不提前开放；整卡收回同时覆盖 MIG 切片。
- 等待替代资源时重复 resume 不扣预算、不刷新原租约；获得替代执行时才扣 restart；执行失败的 retry 独立计数。
- 总 job timeout 包括暂停与重试；expiry/timeout 在同刻压过重试，未启动的代次不计数。
- 大步和分步时钟不仅最终投影一致，非 clock 生命周期事件的时间、状态变化、资源差量及 attempt 也一致。
- Draining 保持旧执行而拒绝新放置；NotReady 清退；Ready 不复活旧工作、不清除未结束的贡献者收回。
- WorkIntent 保留首次到达、同 allocation/job 身份及完整 attempt 历史；逻辑模型恢复剩余工期后可成功结算，但不谎称实际工件恢复。
- 32 独立 SQLite 连接并发 resume、checkpoint 重放与 resume/release 竞争；事件差量逐前缀重建资源占用，检查无负资源、双占和重复归还。

## 证据边界与未代替的验收

这里的 CPU **协议**测试使用合成的可信执行器观察，证明控制面不会用逻辑时钟、unknown 消息或公开 URI 代替实际停止/工件核验；它们不是启动真实 CPU 进程的证据。实际保存、停止、恢复和结果文件必须由独立 CPU executor/driver 验收材料证明。

此记录不声称隐藏评测全通过、生产多机可用、真实 GPU 可用，也不代替 B 的六工作区接线和 CP2 三角色动态 UI 验收。CP1 双人通过不能自动转为 CP2 UI 通过。只有本轮实际运行并记录的单项标记通过；完整 CP2 门检由根交付汇总其余证据。

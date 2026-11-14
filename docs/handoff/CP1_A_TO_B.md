# CP1 交接：A 后端已可接线，等待 B 联合验收

当前停在 CP1（正常旅程），不进入 CP2。A 完成了真实 HTTP 后端；B 的六工作区、策略/工作流编排及两人交叉操作仍由 B 的分支交付。现有前端显示 CP0 内容是尚未接线，不能据此认为后端仍是占位实现。

## 先运行

在自己的 clone 内：

```bash
git fetch origin
git status --short
# 留在 B 当前开发分支（例如 feat/route-b），保留自己的改动。
# 工作区干净后合入 A 的已验证接口
# 如有同文件修改，按实际 diff 解决，不覆盖 B 的代码。
git merge origin/work/a-core
make setup
make cp1-a
FABRIC_INSTANCE=owner-b FABRIC_INVENTORY_FILE=configs/cp1_inventory.json make api PORT=8081
make web PORT=8081 WEB_PORT=5174
```

最后两条在不同终端运行。默认服务只绑定本机，普通请求带 `X-Owner: alice`。模拟库存首次初始化包含 A10 整卡、A100 整卡/切片及 CPU 节点；没有真实设备声明。使用 README 的 `init_cp1_demo.py` 可生成独立三角色 bearer 凭据。程序不自动读取 `.env`。

## 已实现什么

- 一个 SQLite 事务提交状态、资源占用事件、回执及工作意图绑定；重启读取原数据库，不重建运行状态。
- CPU、内存、存储、GPU 型号/单卡显存/profile/节点标签原子满足；整卡与其切片不能同时占用。
- 获配起 TTL、续租、释放、排队、逻辑时钟逐截止推进。job 完成/取消后保留租约，除非工作意图明确选择自动归还。
- allocation 幂等键保存原始状态码/响应；重复提交不会变成第二笔借用。X-Request-Id 仅是关联 ID。
- WorkIntent 首次到达在内核记录；同一阶段创建与绑定原子完成。成功阶段重试先返回保存回执，再考虑旧版本问题。
- Owner 请求隔离，列表/事件/metrics 同样过滤；请求中的角色头不能提权。admin/contributor 由服务端 token 映射授予，测试密钥不代表普通身份。
- 不可变 ReadSnapshot / DecisionSnapshot；外部策略只能提交经过内核复核的 DecisionPlan。

CP2 的 checkpoint/resume、贡献者收回、node-state、fault 仍返回经鉴权的 501。CPU 保存/恢复、重试/重启预算不在 CP1 的完成范围。正常任务采用 **constant-work-v1：10 秒逻辑工期**，不运行 image/command，不把 timeout 当工期，也不产生虚构的计算结果工件。`/_test/clock` 仅用于受控开发验收；界面不携带测试密钥。正常管理员可用独立产品入口 POST `/x/v1/admin/clock`（`{advance_seconds}`，管理员 bearer）显式推进逻辑时间，用户角色无权调用。界面须标为模拟时钟，不宣传为物理运行加速。

## 三个接线点

### 1. 服务端工作流

`app.state.kernel` 实现 contracts/ports.py 的 CommandPort、SnapshotPort、ReadPort、WorkflowPort。推荐 B 的服务端 workflow 调用这些端口，不另建持久业务状态，也不通过测试端点实现用户操作。

```python
receipt = kernel.register_intent(registration, snapshot.version,
                                actor=authenticated_actor, request_id=request_id)
receipt = kernel.create_allocation_and_bind(intent_id, "allocate-v1", current_version,
                                            actor=authenticated_actor, request_id=request_id)
# 等 allocation 真正 Allocated 后再执行；Pending 不等于资源已拿到。
receipt = kernel.create_job_and_bind(intent_id, "job-v1", current_version,
                                     actor=authenticated_actor, request_id=request_id)
```

响应丢失时重试原 stage ID；不创建新 allocation。不同操作复用同一 stage ID 返回冲突。失败且无提交的阶段不留下新对象，可刷新版本后重试；已成功的阶段回执持久保留。同一 intent 已有绑定时，新阶段也不重复借用。公开 POST `/v1/jobs` 没有新增幂等保证。已绑定 WorkIntent 的租约只允许通过对应 create_job 阶段启动，普通 job POST 返回409，以免绕开绑定和结算。

`settlement_action` 在创建意图时选择 `keep_lease`、`release_on_success` 或 `release_on_terminal`。完成页同时显示作业状态和租约状态；“任务成功”不能显示成“GPU 已归还”。CP1 不自动生成真实结果文件。

### 2. HTTP 接口

官方路径保持原样。GET `/v1/capacity`、allocations、jobs、events、metrics 由 A 提供最小 transport bridge，B 后续可以用投影实现替换；不能同时注册两个相同方法/路径。A 没有改写 B 的 projections/queries/web/workflows 目录。

| 扩展 | CP1 请求/响应 |
|---|---|
| POST `/x/v1/work-intents` | 直接 WorkIntentRegistration JSON；可选 If-Match=`epoch:revision`；202 为 CommitReceipt |
| GET `/x/v1/work-intents` | `{intents, version, ...分页字段}` |
| GET `/x/v1/work-intents/{id}` | `{intent, version}` |
| POST `/x/v1/work-intents/{id}/actions` | `{action, expected_version, stage_id?}`；allocate/create_job 必须有稳定 stage_id；release/cancel 直接操作已绑定对象，不宣称阶段回执保证 |
| POST `/x/v1/contributions` | 直接 NodeInventory JSON；认证身份必须为该 owner 的 contributor/admin；201 为 CommitReceipt |

动作 expected_version 是 `{epoch, revision}`。缺少/非法字段返回400，身份失败401/403，越权对象404（renew统一409），版本或阶段冲突409。提交成功与完整旅程成功是两个状态。

列表默认最多500条，`limit=1..500`；对象列表 after=`epoch:revision:offset`，期间版本变化返回409，应刷新列表。事件 after=`epoch:sequence`，next_after 指向最后实际交付的事件；不要手动跳到全局最新 sequence。capacity 的 totals 是全库存总数，即使 nodes 分页。metrics 基于全部已授权对象计数。

### 3. 策略接入

默认 `FABRIC_SCHEDULING_MODE=builtin`：priority 降序、到达先后、稳定 ID，节点/设备 first-fit，立即调度可行请求。它是正常链的基线，不是最终创新策略。

B 需要自主控制派发时，用**新实例**设置 `FABRIC_SCHEDULING_MODE=external`：请求先保持 Pending，调用 `policy.plan(kernel.decision_snapshot())`，再以可信 system actor 提交 decision.commit。内核重验 epoch/revision、当前时刻、需求身份、全部节点资源与设备形状，拒绝过时或虚构放置。CP1 只接受 allocate/defer；所带前置条件支持该 allocation 的 owner、allocation_state、lease_valid，逐项检查；其他条件或动作明确拒绝，不静默忽略。模式写入数据库，重启时不允许静默改变。外部模式下，时钟、释放和新增请求之后何时调用策略由 B 的服务端编排控制；没有编排时 Pending 会继续等待。

模拟器的 completion_at 不进入 DecisionSnapshot；不要从数据库或内部 executor 读取工期来给策略打分。快照没有未来到达、回收或场景 seed。

## 已执行验证与剩余联合门槛

`make cp1-a` 会启动独立 HTTP 进程，保存实际输入/输出与 A 范围回执；它使用临时凭据和隔离数据库，结束时清理进程。原始记录不会包含 bearer/test 密钥。

测试覆盖32并发争最后资源、同键竞争/异载荷冲突、资源事件独立重算、Owner隔离、租约边界与双还、模拟超时、提交后硬退出与相同阶段重放。具体计数及当前构建以运行生成的 `evidence/cp1/a_core_receipt.json` 为准。

联合 CP1 仍要完成：

1. B 将真实接口接入六工作区，正常申请—运行—取消/结算可操作。
2. 两位操作者交换角色，验证部分成功和重试、页面刷新、终态租约提示。
3. 按 `configs/checkpoints.json` 写入真实联合证据，运行 `make checkpoint CP=CP1`。没有 UI/双人证据时该命令继续失败，A 后端通过不能覆盖它。

本次没有继续研发新策略、回收恢复或真实 CPU。确认联合 CP1 后再进入 CP2。

HTTP 命令使用 `Kernel.commit_with_payload` 从同一事务取得回执与原响应快照，即使随后发生 reset，也能完成该次响应。`receipt_payload` 是当前 epoch 的诊断读取，不应单独用于构造并发写响应。

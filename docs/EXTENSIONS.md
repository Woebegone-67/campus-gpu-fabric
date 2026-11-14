# 产品扩展接口与接线状态

扩展不属于官方 21 项操作，不改变冻结的 OpenAPI。写适配归 A，读投影和产品工作流归 B；所有持久写入仍经唯一 CommandPort/WorkflowPort。当前交接见 [CP2 A→B](handoff/CP2_A_TO_B.md)，下表区分已挂接路径与后续设计。

## 已挂接路径

后续账户改造：普通 `member` 同时拥有 `use_compute` 与 `contribute_resources`；旧 researcher/contributor 保留原范围。下表的个人动作也接受具备对应能力的 member，仍检查 owner。实际新契约、兼容和UI接线见 [同账户能力交接](handoff/ACCOUNT_CAPABILITIES.md)。

| 方法与路径 | 允许身份 | 当前契约与边界 | 负责人 |
|---|---|---|---|
| GET `/x/v1/session` | 当前已认证账户 | 可信 actor/owner、业务能力、独立管理员标记；不读取账本、不缓存、不返回凭据 | A |
| POST `/x/v1/work-intents` | researcher owner | 直接 `WorkIntentRegistration`；可选 `If-Match: epoch:revision`；202 `CommitReceipt` 只表示登记 | A 写＋B 编排 |
| GET `/x/v1/work-intents` | researcher/admin | caller 范围分页，版本化意图列表 | B |
| GET `/x/v1/work-intents/{intent_id}` | owner/admin | 已授权工作与版本 | B |
| POST `/x/v1/work-intents/{intent_id}/actions` | owner/admin | `allocate/create_job` 必需 `expected_version` 与稳定 `stage_id`；`release/cancel/resume` 使用 `expected_epoch`，或保留原严格 `expected_version`；返回 `CommitReceipt` | A 写＋B 编排 |
| GET `/x/v1/decisions/{decision_id}` | owner/admin | 决策快照、计划与实际提交回执的已授权投影 | B |
| GET `/x/v1/contributions` | contributor/admin | 自己的供给；admin 全局授权范围 | B |
| POST `/x/v1/contributions` | contributor/admin | 直接 `NodeInventory`；身份、库存及当前 `observed_at` 检查；201 `CommitReceipt` | A |
| POST `/x/v1/contributions/recall` | contributor/admin | `{gpu_ids,notice_seconds,duration_seconds}`；贡献者只操作自己的 GPU；201 `CommitReceipt` | A |
| GET `/x/v1/artifacts?uri=…` | 有 owner 的已认证身份 | CPU 实例登记工件；核对 caller owner 与当前 epoch；其他情况404 | A 工件读取适配 |
| GET `/x/v1/admin/overview` | admin | 当前版本容量、运行、异常投影 | B |
| POST `/x/v1/admin/clock` | admin | `{advance_seconds}`；普通 `clock.advance/source=manual`，无需测试密钥 | B HTTP→A 内核 |
| GET `/x/v1/experiments/{run_id}` | run owner/admin | 查询已授权实验；当前 devhost 没有运行记录，返回404 | B |
| GET `/x/v1/work-intents/{id}/artifacts/{artifact_id}` | owner/admin | 已挂接查询路径，但 devhost 未注入 artifact reader；不等同于上方可用 CPU URI 读取入口 | B |
| GET `/x/v1/acceptance` | admin | 当前读取器仍给未接入联合证据的目标 `NOT_RUN` | B |

“已挂接”表示服务器存在处理路径，不表示 CP2 六工作台已经接线验收。完整响应形状以 `api/queries/` 和内部 DTO 为准。三角色使用同一身份适配；测试密钥不能代替普通用户、贡献者或管理员身份。

## 尚未实现的设计入口

早期蓝图中的 `POST /x/v1/contributions/{contribution_id}/recall`、`POST /x/v1/experiments` 尚未实现；也没有 `GET /x/v1/experiments` 列表入口。当前工件和 recall 使用上表实际路径；实验提供器由 B 后续接入。actions 没有独立的 `retry_stage` 或 `settle`：重试原动作/阶段，或使用已登记 settlement 与 release。

## 事务、授权与显示

### 工作操作的版本条件

用户对当前工作的 `resume`、`cancel`、`release` 可发送
`{"action":"resume","expected_epoch":0}`。`expected_epoch` 必须是非负安全整数，
不能省略、使用字符串或布尔值，也不能与 `expected_version` 同时提供。
内核在同一写事务中检查实例 epoch、账户权限、工作归属、当前绑定对象及操作合法性；
无关 CPU 时钟或其他工作带来的 revision 变化不会单独拒绝这类操作。
跨 reset 的旧 epoch 返回 409 `stale_version`，不产生业务效果，客户端应重新查询，
不能替换成新 epoch 自动重试。

原有显式 `expected_version` 请求保持严格的 epoch/revision CAS；版本过期仍返回
409 `stale_version`。`allocate/create_job` 继续要求原版本与稳定阶段 ID，不接受只有
`expected_epoch` 的请求。恢复仍复查保存核验、原租约截止、资源与预算；没有资源时
等待，不提前增加执行代次或恢复计数。此接口没有承诺未知网络结果可安全重复提交；
响应丢失时先查询状态，不盲目重试。

### 工作名称

`POST /x/v1/work-intents` 的 `WorkIntentRegistration` 与查询返回的 `WorkIntent`
增加可选 `display_name`。字段省略或为 `null` 表示未命名；非空值必须是字符串，
服务端先去除首尾空白，再校验长度为 1–80 个 Unicode 字符。空串、纯空白、超长字符串
及非字符串值返回 400，不创建工作。保留内部空格与大小写，不要求名称唯一。

名称与工作规格一起在原登记事务内持久化；列表和详情直接返回服务端保存的名称。
名称不替代 `intent_id`，不参与权限、URL 或资源调度，也不作为幂等键。
同一 owner、同一 `intent_id`、相同规范化名称与其余输入的重试返回原登记回执；
修改名称后重用同一 ID 返回 409 `stage_conflict`，不会重命名或新建工作。
省略与 `null` 等价，旧版本未包含名称的登记幂等记录仍可重试。

旧记录不需要迁移或补写名称，读出时 `display_name` 为 `null`。中断、恢复、结算和
服务重启不改变已保存名称；列表、详情及恢复等操作继续执行原 owner 隔离。
当前没有重命名接口，也没有按名称全局搜索接口；页面可以筛选已经读取的当前页。
上述字段仅属于产品扩展，冻结的官方 OpenAPI 保持不变。

首次到达由内核登记时确定。allocation/job 的创建、绑定和阶段回执分别在同一事务落地；丢失响应时重试同一 stage。不能把“先创建后补关联”或重复公开 job POST 当作阶段幂等。动作不允许任意修改冻结规格。

贡献者产品路径直接提交回收领域命令，不通过 HTTP 调用 `/_test/contributor-demand`。模拟供给登记不证明真实硬件已接入。实验必须用独立实例/数据库/epoch，与运营资源隔离。

公共 checkpoint ready 是报告；真实工件核验与新进程实际接续分别记录。CPU 的取消、释放、超时或回收需等待真实停止确认，`pending_terminal` 表示待结清结果；不能仅依据写请求202就把资源显示成可复用。CPU artifact URI 只接受适配器的已登记对象，不代理任意本地路径或外部 URL。

列表、事件、metrics 和工件遵守 owner 隔离。页面刷新根据服务器版本重取数据；版本冲突与未知提交结果分别处理。联合验收读取器尚未消费顶层 CP2 汇总，不能手工把界面标签改成“通过”。

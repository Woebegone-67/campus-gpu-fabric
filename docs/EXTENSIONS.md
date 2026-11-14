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
| POST `/x/v1/work-intents/{intent_id}/actions` | owner/admin | `action=allocate/create_job/release/cancel/resume`；必需 `expected_version`；前两项必需稳定 `stage_id`；返回 `CommitReceipt` | A 写＋B 编排 |
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

首次到达由内核登记时确定。allocation/job 的创建、绑定和阶段回执分别在同一事务落地；丢失响应时重试同一 stage。不能把“先创建后补关联”或重复公开 job POST 当作阶段幂等。动作不允许任意修改冻结规格。

贡献者产品路径直接提交回收领域命令，不通过 HTTP 调用 `/_test/contributor-demand`。模拟供给登记不证明真实硬件已接入。实验必须用独立实例/数据库/epoch，与运营资源隔离。

公共 checkpoint ready 是报告；真实工件核验与新进程实际接续分别记录。CPU 的取消、释放、超时或回收需等待真实停止确认，`pending_terminal` 表示待结清结果；不能仅依据写请求202就把资源显示成可复用。CPU artifact URI 只接受适配器的已登记对象，不代理任意本地路径或外部 URL。

列表、事件、metrics 和工件遵守 owner 隔离。页面刷新根据服务器版本重取数据；版本冲突与未知提交结果分别处理。联合验收读取器尚未消费顶层 CP2 汇总，不能手工把界面标签改成“通过”。

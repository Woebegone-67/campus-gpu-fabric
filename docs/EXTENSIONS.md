# 产品扩展交汇表 v1

本表冻结A/B接线位置；以下为完整产品设计；**CP1 已实现的子集与准确请求格式见 [CP1 接线说明](handoff/CP1_A_TO_B.md)**，其余尚未实现，不属于官方21项操作，也不改变官方API。写适配归A，读适配归B；工作流编排归B，但写入经A的CommandPort/WorkflowPort。

| 方法与路径 | 允许角色 | 请求 / 响应契约 | 负责人 |
|---|---|---|---|
| POST `/x/v1/work-intents` | researcher | `WorkIntentRegistration`（含`WorkSpecification`与稳定intent_id）；202返回工作ID和`CommitReceipt`，不代表已经获配 | A写＋B编排 |
| GET `/x/v1/work-intents` | researcher/admin | caller范围分页；`ReadSnapshot`中的意图列表 | B |
| GET `/x/v1/work-intents/{intent_id}` | owner/admin | 同版本`WorkIntent`、相关回执/决定引用 | B |
| POST `/x/v1/work-intents/{intent_id}/actions` | owner/admin | `action`为retry_stage/resume/cancel/release/settle；`stage_id`与`expected_version`；返回`CommitReceipt` | A写＋B编排 |
| GET `/x/v1/work-intents/{intent_id}/artifacts/{artifact_id}` | owner/admin | 按已登记artifact ID领取结果或日志；不是任意路径/URL代理 | B读接口，A提供工件访问适配 |
| GET `/x/v1/decisions/{decision_id}` | owner/admin | 决策时快照引用、`DecisionPlan`与实际`CommitReceipt`；已授权投影 | B |
| GET `/x/v1/contributions` | contributor/admin | 仅自己的供给；admin可查全局授权范围 | B |
| POST `/x/v1/contributions` | contributor/admin | 模拟供给定义，规范化为内部ContributionRegisterParameters；返回`CommitReceipt` | A |
| POST `/x/v1/contributions/{contribution_id}/recall` | contributor/admin | 受权gpu_ids、notice_seconds、duration_seconds；返回`CommitReceipt` | A |
| GET `/x/v1/admin/overview` | admin | 当前revision的容量/运行/异常投影 | B |
| POST `/x/v1/experiments` | admin | 已登记scenario/model/policy版本及seed；202返回独立run_id | A受权写入口＋B实验运行器 |
| GET `/x/v1/experiments/{run_id}` | run owner/admin | `EvidenceRun`及对应产物索引 | B |
| GET `/x/v1/acceptance` | admin | 已执行回执与当前构建匹配状态；没有记录显示NOT_RUN | B |

表中的owner指对象的授权拥有者，不是允许客户端自报角色。正常角色通过统一身份适配进入内部`AuthenticatedActor`。开发本地身份映射与正式bearer模式均由A处理；前端不携带X-Test-Key。模拟供给注册不等于证明真实硬件已接入。

工作意图的首次到达由内核登记时确定。服务端编排器使用稳定stage执行“创建allocation并绑定”“创建job并绑定”，回执同事务落地；响应丢失只重试原stage。actions不能任意修改已冻结任务规格或绕过状态/预算检查。扩展HTTP数据转换为contracts中的内部对象，不直接信任客户端提供actor、已提交版本或成功状态。

贡献者只有权操作自己登记或获授权的供给；内部复用回收领域命令，不通过HTTP调用`/_test/contributor-demand`。实验必须有独立实例/数据库/epoch，与运营资源隔离。

以上响应均带请求ID和当前版本；读取对象越权按身份语义处理，写动作冲突返回明确回执。默认固定500条以内分页（具体分页模型在查询适配中统一），不将全量历史塞入每次页面刷新。跨路由字段调整由双方共同更新本表和types，业务核心HTTP原件不动。

CP1 新增 POST `/x/v1/admin/clock`：管理员 bearer + `{advance_seconds}`，经普通 clock.advance/source=manual 命令；不需要、不接受以测试密钥代替管理员身份。用于逻辑模拟演示，区别于物理执行时钟。

# 双页界面的数据、权限与组件接管说明

日期：2026-11-15。审计基线：`059f477b9ea24578dda9426a3385e6ec97ba1cc1`。

后续接入变更：`work/account-capabilities` 已实现普通 member 双能力与 `GET /x/v1/session`；见 [实际契约和迁移说明](../handoff/ACCOUNT_CAPABILITIES.md)。下文有关单角色和缺少 session 的描述保留为上述基线的审计事实，不再代表该后续分支；新UI仍未实现。

本文是 UI 设计说明的工程附件，供 B 将「总览 → 工作台」落到当前系统。本文只描述当前实现与下一步接线要求，不代表新界面已经实现或验收通过。B 提供的黑色星点界面目前只有截图，不在本基线源码中；不能据此判断其组件、接口或交互已经接通。

本文以实际源码为准。`web/README_B.md` 中的 CP1 阶段功能边界，以及 `docs/EXTENSIONS.md` 的部分 CP2 阶段说明，早于当前实现；例如贡献收回、隔离实验和报告下载应以本文列出的路由及组件核对。历史验收文档保持原样，不能通过改写它们把新 UI 标为已通过。

## 1. 实现边界：共用平台不等于已经拥有共用权限

产品应围绕同一个人的两种活动组织：**使用算力、贡献算力**。管理员是额外的管理权限，不是所有人都需要经历的第三套首页。个人总览可以同时表达「我的工作」和「我的贡献」，前提是当前账号确实获得两种能力。

当前实现还有一项必须显式处理的差距：

| 事实 | 当前实现 | 对设计的约束 |
|---|---|---|
| 身份角色 | `AuthenticatedActor.role` 是单值 `researcher` / `contributor` / `administrator`，另有仅内部使用的角色 | 不能在浏览器把角色改成数组，就声称账号同时拥有两种权限 |
| 工作写操作 | 内核与工作流限制 researcher / administrator，且核对 owner | contributor 凭证不能通过点「使用算力」变成 researcher |
| 贡献操作 | 贡献列表、登记、收回要求 contributor / administrator | researcher 不能通过切换页面获得贡献权限 |
| 管理员 | 服务端核对 administrator；无 owner 的全局身份不能冒用其他 owner | 「管理」是受权入口，不是普通用户的任意视角切换 |
| 当前用户信息 | 没有已实现的 `/me`、`/session` 或等价可信角色/能力投影 | `Identity.profile`、本地文件中的名称和 `admin` 字符串只可作为界面提示，不能做授权依据 |

依据：[身份契约](../../src/campus_fabric/contracts/models.py)、[HTTP 身份核验](../../src/campus_fabric/auth/http.py)、[内核](../../src/campus_fabric/domain/kernel.py)、[工作流](../../src/campus_fabric/workflows/orchestrator.py)、[贡献写路由](../../src/campus_fabric/api/mutations/http.py)。

**本轮可做：**统一壳层和信息架构；按当前已验证的访问结果呈现内容；保留「权限尚未确认」与「没有记录」的区别。页面可以说明当前身份尚未获某能力，但不应一进入首页就同时请求所有管理员接口，造成大片 403。

**需要 A / B 联合落地后才能宣称完成：**A 确认一个账号获得多种能力的授权方式、owner 关系和身份失效语义；B 在可信投影契约确认后呈现入口。若短期仍使用同一 owner 的两套凭证，那是显式切换身份，不是无缝的单会话双能力账号；切换时必须清空旧身份数据。不要用管理员凭证替代普通人的双能力设计。

## 2. 旧六区如何迁移

「总览」回答现在有什么需要关注；「工作台」承接处理和深入查看。原来的技术分类可以继续作为内部组件边界，不必继续成为六个并列的产品入口。

| 旧入口 / 实际文件 | 新位置 | 继续复用的内容 | 迁移时要消除的混杂 |
|---|---|---|---|
| 运行工作台：[Run.tsx](../../web/src/pages/Run.tsx) | 工作台 → 我的工作；新建工作 | 稳定工作 ID、登记、工作列表、租约/任务查询 | 租约与执行任务变成工作详情，避免普通用户先理解三套对象才能使用 |
| 资源与贡献：[Resources.tsx](../../web/src/pages/Resources.tsx) | 工作台 → 我的贡献；需要选资源时的资源视图 | 节点容量、自己贡献的设备、收回流程 | 公共可见资源池不等于自己的贡献；节点 JSON 登记应进入高级/部署入口 |
| 任务连续性：[Continuity.tsx](../../web/src/pages/Continuity.tsx) | 工作台 → 某项工作详情 | 工作身份、各次执行、中断、核验、接续、结果、资源归还 | 不要求用户自行复制 ID 才能看自己的工作；保留 ID 查询作为辅助入口 |
| 决策解释：[Decisions.tsx](../../web/src/pages/Decisions.tsx) | 工作详情 → 分配说明 / 相关决策；管理员诊断 | 选择理由、覆盖范围、实际事件与提交回执 | 计划选中、实际提交、任务运行必须分别表达；个人可见的局部候选不代表全池穷尽 |
| 事件与实验：[Events.tsx](../../web/src/pages/Events.tsx) | 工作详情 → 活动记录；管理员 → 实验与报告 | 工作关联事件、授权范围内的事件列表、导出 | 个人工作活动页不应附带 CP3 报告和实验控制台 |
| 系统验收：[Acceptance.tsx](../../web/src/pages/Acceptance.tsx) | 管理员 → 系统与验收 | 构建、能力、证据、全局状态、逻辑时钟 | 验收阶段和内部实现术语不成为普通用户首页主体 |

首页不需要复制全部六区，也不需要为每个对象新建一个大卡片。它提供少量有根据的关注事项、正在进行的工作、最近结果与贡献状态；点击对应事项进入带上下文的详情页。页面滑动只改变空间，不提交、恢复、取消或释放任何业务对象。

## 3. 现有接口清单与可用数据

下表列出当前可核对的接口，不把尚未设计的通知服务写成已有能力。读接口装配见 [query routes](../../src/campus_fabric/api/queries/routes.py)、[query services](../../src/campus_fabric/api/queries/service.py)；写接口见 [mutation routes](../../src/campus_fabric/api/mutations/http.py)。

| 用途 | 已有接口 | 实际字段 / 语义 | UI 使用规则 |
|---|---|---|---|
| 服务在线 | `GET /healthz` | HTTP 服务健康响应 | 在线不等于身份有效、任务可运行或验收通过 |
| 服务模式 | `GET /v1/version` | `execution_mode`、`checkpoint`、`capabilities`、版本字段 | 模式未知时不开放登记；按能力展示恢复等选项 |
| 工作列表 | `GET /x/v1/work-intents?limit=25&after=…` | `items` / `intents`、`version`、`now`、`scope`、`owner_id`、分页元数据、`legal_actions` | 当前 Run 每页 25；首页不能把首屏条数叫全部工作数 |
| 单项工作 | `GET /x/v1/work-intents/{intent_id}` | `item` / `intent`，同一响应内含关联 `allocations`、`jobs`、`events`、`legal_actions`、版本与时间 | 优先复用这一份关联快照，不为同一工作再拼三个不同时间的请求 |
| 租约 | `GET /v1/allocations`、`GET /v1/allocations/{id}` | 对象及授权范围；详情还保留兼容根字段 | 工作详情内呈现资源是否仍持有，不能只看任务成功状态 |
| 任务 | `GET /v1/jobs`、`GET /v1/jobs/{id}` | `state`、`attempt`、`execution_mode`、`retry_count`、`result_uri`、`failure` 等 | 与工作、租约状态分开；一次工作可以经历多次执行 |
| 可见容量 | `GET /v1/capacity` | `nodes`、`items`、`totals.capacity`、`totals.available`、`observed_at`、版本 | 总量基于可见库存；节点分页与总量覆盖范围不同；不等于硬件监控利用率 |
| 我的贡献 | `GET /x/v1/contributions?limit=50&after=…` | 贡献节点 `items`、`contributor_owner_id`、GPU 设备与版本分页 | contributor 仅自己的节点；admin 可见更广。当前页面只取 50，不能假称无限全量 |
| 当前身份计数 | `GET /v1/metrics` | `counts`、`work_by_state`、`jobs_by_state`、`allocations_by_state`、`scope`、`measurement_kind=authorized_record_counts` | 可用于明确范围的数量摘要；`performance_measurements` 是空，不可绘成真实 GPU 性能图 |
| 管理员总览 | `GET /x/v1/admin/overview` | 状态计数 + `capacity.nodes/totals`，来自同一受权快照 | 只对管理员；不要在前端重新跨请求拼一个伪原子总览 |
| 工作决策 | `GET /x/v1/decisions/{id}` | `item`、`commit_receipts`、`actual_events`、`commit_status`、范围信息 | 相关 ID 来自工作事件；不捏造“所有决策列表”端点 |
| 活动记录 | `GET /v1/events?limit=50&after=…` | `items/events`、`next_cursor`、`next_after`、`has_more` | 事件游标不是对象列表游标；按服务端返回向后读取 |
| CPU 工件 | `GET /x/v1/artifacts?uri=…` | `fabric-cpu://…` 对应的原始 JSON 响应文本 | 当前 CPU 结果及 checkpoint 下载使用这一条；服务端核验 owner 与当前 epoch |
| 登记工件读口 | `GET /x/v1/work-intents/{id}/artifacts/{artifact_id}` | 受权工件读扩展，需要已接入的 reader 和登记项 | 路由存在不保证任意 artifact ID 可用；不要用它替代已接通的 CPU URI 路径 |
| 验收证据 | `GET /x/v1/acceptance` | `items`、`build_matches`、`status` | 仅管理员；未运行、未达标、当前构建匹配分别保留 |
| 冻结策略报告 | `GET /x/v1/policy-validation` | `analysis`、`runs`、`partition`、`historical`、`build`、`serving_build`、`current_build_acceptance_eligible`、`integrity_scope` 等 | 历史报告不能装成当前版本通过；开发回归不能装成正式效果验证 |
| 策略报告工件 | `GET /x/v1/policy-validation/artifacts/{artifact_id}` | 已登记工件原始 JSON | 逐项下载时服务端核验；不要把目录读取描述为已全量重审全部工件 |
| 隔离实验 | `GET /x/v1/experiments/catalog`、`GET /x/v1/experiments`、`GET /x/v1/experiments/{id}` | 目录、保留的运行记录、单次实验 | 管理员；实验列表不使用运营账本版本来承诺实验分页一致性 |
| 实验创建 / 工件 | `POST /x/v1/experiments`；`GET /x/v1/experiments/{id}/artifacts/{artifact_id}` | 创建输入 `scenario_id/policy/seed`；登记后的产物 | 与运营实例隔离；存在任务回执不等于实验已完成 |

CPU 工件真实装配见 [app.py](../../src/campus_fabric/app.py)。类型与内部读模型见 [models.py](../../src/campus_fabric/contracts/models.py)、[read_models.py](../../src/campus_fabric/projections/read_models.py)。

### 3.1 不存在或尚未接通的产品字段

以下内容可以画进明确标注的设计稿，但接正式数据前必须补契约，不能由前端猜值：

- 工作名称、可持久化的重命名：`WorkIntent` 没有 `title` / `display_name`。第一版可使用可读短 ID + 复制完整 ID；本地临时别名必须标明只在当前会话有效。
- 实时完成百分比、剩余时间：`WorkIntent` / `JobView` / `AttemptSnapshot` 没有稳定的 `percent`、`completed_steps`、`total_steps` 或 ETA 投影。
- 通知收件箱、已读状态、跨设备已读同步、邮件/系统通知订阅：当前没有相应持久化模型与 API。
- 可信当前角色/可授予能力：没有会话投影；版本里的 `capabilities` 是服务能力，不是某个人的权限。
- 全局工作搜索、按状态的服务端筛选：现有页面主要在当前页内筛选；不能把输入框标为「搜索全部工作」而实际只搜索 25 条。
- 个人贡献积分、收入、节省费用、碳减排、GPU 利用率历史：当前无对应经过定义的测量。可以留未来位置，不能生成看似真实的数字。
- WebSocket / SSE 实时推送：当前查询以明确读取与刷新为主，不能仅加动画就标为「实时」。

内部执行器存在 progress 事件，不等于公共查询已经承诺可显示完成百分比；即使结果工件含计算步数，也不能倒推一条运行期间从未观测到的进度曲线。

## 4. 总览的关注事项如何有依据地产生

第一版可从已授权快照生成**待关注状态摘要**，不称为新的服务端通知系统。推荐最小单元包括：对象 ID、状态证据、用户可理解的原因、可执行的下一步、来源版本、读取时间、明确的范围。

| 候选关注项 | 依据 | 正确呈现 | 不能做 |
|---|---|---|---|
| 需要手动处理 | 工作 `state=manual_action`；失败 stage / failure 内容 | 「这项工作需要处理」，进入详情看原因 | 因一条 HTTP 报错就把工作永久标成失败 |
| 正在等待资源 | 工作 waiting，结合租约/任务关联信息 | 等待原因仅显示已知事实；未提供预计时间时写未提供 | 自行估计排队名次或等待分钟数 |
| 已获资源但任务未创建 | 持有租约，尚无关联任务；stage 显示相应阶段 | 「资源已分配，任务尚未启动」，仅在 legal action 允许时提供继续 | 再申请一份新资源以掩盖部分成功 |
| 收到收回通知 | allocation `PreemptRequested` | 展示通知状态、保存/停止进展 | 直接声称已迁移、已经安全释放 |
| 中断后待接续 | 租约/工作/attempt/checkpoint 事实 + 可用 resume 操作 | 说明保存报告、核验和接续各自状态 | 把 ready 报告等同于可恢复、把点击恢复等同于接续成功 |
| 停止仍未确认 | allocation `pending_terminal`，或 attempt stopping / quarantined | 「等待停止确认」，说明资源尚未确认归还 | 提前把卡计为空闲、把 lease 画成释放完成 |
| 已完成 / 失败但仍占资源 | 终态工作与持有资源同时存在 | 分开显示结果和归还状态，进入处理 | 用一个绿色完成圆点覆盖两个事实 |
| 新结果可查看 | `result.artifact_uris` 或 job `result_uri` 真实存在 | 「结果可查看」；下载已登记工件 | 未生成工件时出现有效下载按钮 |
| 数据未能更新 | 查询失败、身份未确认、模式未读到 | 全局/局部数据提示，与业务状态分开 | 把网络中断计入业务失败数 |

实现时先复用 [Settlement.tsx](../../web/src/components/Settlement.tsx)、[Continuity.tsx](../../web/src/pages/Continuity.tsx)、[CP2.tsx](../../web/src/components/CP2.tsx) 中已经区分的语义。可抽成有输入类型的纯展示 selector，但不要在浏览器另写一个调度器或权威状态机。

**覆盖范围必须诚实：**`/v1/metrics` 可提供授权记录的完整状态计数，但不能定位每条具体关注项；首 25 项工作只能产生这 25 项的关注摘要。不能写「你有 3 项待处理」却漏掉未加载页里的项目。完整首页应由 B 增补经过评审的授权聚合读模型，输出范围、版本、完整性、总数和有限的摘要条目；A 提供必要的原子读取/身份边界。新路由名称及 JSON 契约尚未冻结，本文不把它标成已存在。完成前可明确写「最近加载的工作」并链接完整列表，不通过后台逐条抓全库来凑一个仪表盘。

**“已读”最小实现：**可以在当前身份的内存里折叠某条提示，刷新后恢复；文案叫「本次收起」，而非「已处理」。真正业务状态变化必须由服务端确认。持久化已读、待办分派、提醒订阅是后续能力，不挤入本次换肤验收。

## 5. 进度、时间和执行模式

### 5.1 进度以阶段和事实表达

建议第一版展示「已登记 → 等待资源 → 运行 / 中断处理 → 结果可查看」的阶段描述，同时保留真实 state；阶段并非所有工作都必经的固定直线，失败、取消、恢复可能产生分支。可用不确定进度的轻量活动标识，不能按浏览器经过时间填充 0–100%。

`WorkIntent.attempts` 保存多次执行；新一轮 attempt 不应覆盖旧一轮，更不能创建一个新工作来伪装恢复。Checkpoint 要区分：收到报告（`report_status`）→ 工件核验（`verification_status`）→ 实际接续（`continuation_status` / `resumed_attempt_id`）。三种状态不可合为一个「已保存」徽标。

### 5.2 时间源不得混用

- 工作的 `first_arrival_at`、结果 `recorded_at`、attempt 时间与响应 `now/logical_time` 属于逻辑时间体系。
- 公共租约/任务投影将部分字段转为 RFC 3339，并另带 `created_at_logical`、`expires_at_logical`、`finished_at_logical` 等。用相应逻辑字段与 `now` 比较，不拿日期字符串减逻辑秒。
- `useQuery.receivedAt` 是浏览器成功收到结果的本机时间，仅用于「上次读取于…」，不是任务状态实际发生时间。
- 页面可将长小数格式化为适量小数显示，原始证据保持完整。不得因此改变比较用值或下载值。
- CPU 模式时钟推进随实际运行；逻辑模拟的等待不因人在页面停留而自动完成。模拟时钟工具只保留在受权管理员区。

### 5.3 当前运行能力应在用户决策处说明

模式来自 [service.tsx](../../web/src/service.tsx) 对 `/v1/version` 的解释：

| 模式 | 实际行为 | 新建工作 / 结果页的必要表达 |
|---|---|---|
| `logical_simulation` | 逻辑模拟；不启动填写的容器镜像、命令或 env；不生成真实 CPU 计算工件 | 「逻辑模拟」；不能用“训练中”暗示真实模型训练 |
| `cpu_reference` | 启动服务端固定 CPU 参考程序；GPU 库存仍是模拟；不执行用户填写的镜像/命令/env | 「CPU 参考运行」；简短说明运行固定程序，结果有真实 JSON 工件，但不代表 GPU 训练性能 |
| 未知 / 服务不可达 | 无法确定能力 | 显示无法确认运行方式；关闭提交，保留已填草稿 |

镜像与命令仍是登记规格的一部分，但当前不会执行。可以移到高级规格中；若保留可编辑，必须就近明确用途。不能只保留 `python train.py` 示例，让用户误解成可提交任意训练程序。把限制压到页尾的小字不足以纠正这个误解。

## 6. 写操作必须保留的行为

复用 [api.ts](../../web/src/api.ts) 中现有 `ApiClient`、`registerWork`、`workAction`，不要为新动画另外实现一套提交器。

| 用户动作 | 已有调用 | 不可丢失的约束 |
|---|---|---|
| 登记工作 | `POST /x/v1/work-intents` | 首次提交生成稳定 `intent_id`，保存不可变登记输入；不确定结果时查询同一 ID，不生成第二份工作 |
| 重试资源申请 | `POST /x/v1/work-intents/{id}/actions`，`action=allocate` | 使用当前 `expected_version` 与稳定 `stage_id=allocate-v1` |
| 重试创建任务 | 同上，`action=create_job` | 使用稳定 `stage_id=job-v1`；已分配资源与任务创建失败分开 |
| 接续 / 取消 / 释放 | 同上，`resume/cancel/release` | 由 `legal_actions` 决定当前快照是否建议可用；提交仍由服务端复查 |
| 租约低层操作 | renew、resume 对应 POST；释放 DELETE `/v1/allocations/{id}` | 保留现有语义，只在确有必要的详情/高级区暴露，不能同时制造两套相互矛盾的操作 |
| 取消任务 | `DELETE /v1/jobs/{id}` | 取消任务不直接等于租约已释放 |
| 登记贡献 | `POST /x/v1/contributions` | `NodeInventory` 且 owner 必须匹配；当前 CPU 页面关闭在线手工登记，库存通过启动配置接入 |
| 收回贡献 | `POST /x/v1/contributions/recall` | `gpu_ids/notice_seconds/duration_seconds`；以真实物理 GPU 身份收回，切片不可当独立物理卡重复计算 |

现有提交器只针对明确的 `stale_version` 作有限重试；同 epoch 读到新版本后才重试工作动作，epoch 改变则中止。网络中断、超时或服务端 5xx 代表结果不确定，不能自动重放可能已经成功的写操作。工作登记的默认路径不需要前端先抢读一个版本再无谓制造竞态。

HTTP 202、回执 `committed`、工作 Running、完成、资源 Released 是不同层次。设计反馈应说「已接受，正在确认」或显示已观察到的状态，不在回执出现时播放“任务完成”庆祝动画。取消/释放要明确对象与后果，手势永远不等于确认。

## 7. 查询、缓存与身份切换

当前 [hooks.ts](../../web/src/hooks.ts) 已提供身份 + 路径查询键、generation 检查与 `AbortController`，可防止旧查询覆盖新查询。迁移时应保留，新增首页也遵守同一机制。

1. **身份是缓存边界。** 至少包含 owner、凭证实例与路径；不能仅用 `intent_id` 作为全局缓存键。凭证不写日志、不放 URL。
2. **切换身份立即撤下旧数据。** 工作、贡献、工件预览、错误、操作回执、选择项和注意事项一起重置。旧身份的迟到成功响应不能重新出现。
3. **取消请求还不够。** 请求可能已经完成而其回调尚未处理，写入 UI / 触发下载前仍检查请求世代与当前身份。
4. **不把两个快照相加。** 新查询替换旧版本；epoch 改变时丢弃旧游标和对象关联。对象列表游标携带 `epoch:revision:offset`，版本失效时回到第一页并说明列表已更新；事件使用自己的 `epoch:sequence` 游标语义。
5. **首页数量和清单可暂时来自不同读取时间。** 若不是原子聚合，应显示各自读取状态，不能声明严格一致。管理员已有原子 overview，应直接使用。
6. **刷新不清空草稿。** 当前 `useQuery` 刷新会进入 loading 并清空其 data，新壳层如希望保留旧画面，应显式设计“旧快照，正在更新”；一旦身份变化必须立即清空，不能沿用这一保留策略。
7. **后台刷新属于新增行为。** 当前没有统一轮询器。后续可仅在可见空间与有进行中对象时轮询；离页暂停、请求合并、错误退避，恢复焦点时查询。必须显示最后成功读取时间，避免“实时”假象。具体频率由性能预算和验收确定。
8. **提交过程中锁身份。** 当前 `onMutating` / IdentityPicker 已限制身份操作；动画过渡不能绕过这层锁。

当前 [App.tsx](../../web/src/App.tsx) 条件挂载页面，换工作区会卸载原页；因此“来回滑动仍保留表单、筛选、滚动位置”是新增要求，并非当前已具备。建议把同一身份下的草稿与列表视图状态提升到壳层的内存 store；只保持必要状态，不把所有隐藏页面一直挂着并让它们同时发请求。未提交草稿不得伪装成已登记工作。

身份文件只在浏览器内存解析。整页刷新会丢失凭证，需要重新导入；新 UI 不能悄悄改成 localStorage / sessionStorage 持久化令牌。[IdentityPicker.tsx](../../web/src/components/IdentityPicker.tsx) 的导入、应用和清除行为应保留；文件名称不是授权、页面上显示 alice 也不是服务端认证成功的证据。

## 8. 路由与双空间状态

现有外部/内部链接来自 [navigation.ts](../../web/src/navigation.ts)：

- `#continuity?intent={id}`：指定工作。
- `#decisions?decision={id}&intent={id}`：相关决策及返回工作上下文。
- `#events?intent={id}`：指定工作的活动记录。
- 现有顶层 `#run`、`#resources`、`#acceptance` 等工作区入口。

新路由可以采用新的命名，但要把以上链接解析为工作台内对应位置；不要将旧链接全部落到总览。保留原 ID 和用户已选对象，避免导航触发重新登记。工作详情从总览进入时，返回应回到原来的关注列表位置；从列表进入时，应回到原筛选与滚动位置。

建议路由状态和纯表现状态分开：

| 状态 | 推荐位置 | 原因 |
|---|---|---|
| 总览 / 工作台、当前对象、详情标签 | 可分享 URL / 历史状态 | 可刷新、可回退、可从通知直接定位 |
| 手势拖动百分比、背景透明度 | 短寿命组件状态 | 不入 URL，不驱动数据读写 |
| 同一身份的草稿、列表筛选、滚动位置 | 内存视图 store | 来回切换继续工作；身份切换清理 |
| 凭证、原始身份文件 | 现有内存身份管理 | 不落浏览器持久存储，不进入路由 |
| 工作是否完成、租约是否归还 | 服务端查询结果 | 不能用页面访问、动画完成或本地按钮状态替代 |

浏览器前进/后退应先恢复合法路由，再查询当前身份的授权状态。遇到其他人的对象返回不透露存在性的 404 时，不能用旧缓存“帮助找回”其内容。切换空间的动画可以先结束；对象详情读不到时展示明确错误和返回入口，而不是停在半透明的过渡画面。

## 9. 下载、原始 JSON 与大整数

这些保护已经实现，应把它们作为设计迁移的保留项，而不是在新下载组件里重做精简版：

- [CP2.tsx](../../web/src/components/CP2.tsx)：CPU 结果与 checkpoint 预览/下载。
- [PolicyValidation.tsx](../../web/src/components/PolicyValidation.tsx)：CP3 登记工件下载。
- [Experiments.tsx](../../web/src/components/Experiments.tsx)：隔离实验工件下载。
- [common.tsx](../../web/src/components/common.tsx)：`downloadJsonText` 与普通界面导出。

**下载原始工件必须使用 `ApiResult.raw`。** 服务端 JSON 可能含超过 JavaScript 安全整数范围的 uint64；先 `JSON.parse` 再 `JSON.stringify` 会改变数值位数，破坏结果复核。下载与原始文本预览都保留原响应文本。若要美化大整数展示，必须使用已评审的无损解析方式或字符串投影，不能让格式化后的文本替代原始文件。

普通页面的 `downloadJson(data, …)` 是已解析 UI 数据的导出，不应命名为“原始工件”。例如当前事件导出是当前响应页的数据，界面筛选也主要是当前页筛选；不能写“下载全部审计历史”。准确原始下载与可读的摘要导出可以并存，但名称与用途必须区分。

下载任务持有 AbortController，并在成功后检查身份、对象和请求是否仍然当前；导航或身份变更后到达的旧工件不得自动落盘。按钮状态应包含读取/核验中、成功触发、失败和可重试；“浏览器已收到下载请求”不等于用户已经打开或核对文件。当前工件由服务端返回，不把 `fabric-cpu://` 作为可直接导航的公共 URL。

## 10. 组件拆分建议

以下名称是**建议新组件**，不是现有文件。可按 B 的实现习惯调整，关键是边界与复用关系。

| 建议组件 / 层 | 职责 | 可复用的现有实现 | 不应承担 |
|---|---|---|---|
| `ApplicationShell` | 身份、空间切换、路由、模式状态 | App、IdentityPicker、service context | 调度决策、业务状态推断 |
| `Overview` | 当前授权范围的关注摘要、进行中项目、结果入口 | useQuery + 经定义的展示 selectors | 持久化通知系统、伪造实时数据 |
| `AttentionItem` | 原因、对象、下一步、来源新鲜度 | Settlement / Continuity 的语义 | 单凭颜色宣告完成或高风险 |
| `WorkList` / `WorkComposer` | 我的工作、分页、登记草稿 | Run 的列表和不可变登记过程 | 凭动画状态生成或改变工作 ID |
| `WorkDetail` | 单份关联快照下的概况、接续、结果与活动 | Continuity、Settlement、CP2 | 重复拉多个不一致的关联快照 |
| `ContributionList` / `RecallDialog` | 我的设备、收回对象与确认 | Resources 的物理卡映射与 recall | 把其他人卡、切片或全局库存混为自己的设备 |
| `DecisionExplanation` | 本次资源选择、原因与覆盖范围 | Decisions | 用局部记录推出全池最优/不可行结论 |
| `ReportWorkspace` | 管理员实验、历史报告与验收 | Experiments、PolicyValidation、Acceptance | 把负结果改成绿色“已优化” |
| `RawArtifactViewer` | 原文预览与有权下载 | ArtifactDownload / ValidationDownload / ExperimentDownload | JSON 数值往返、绕过身份校验 |
| `ViewStateStore` | 每个身份内的草稿、选中项与滚动恢复 | 目前需新增 | 持久化凭证、存权威业务事实 |

艺术背景、淡出动画与手势识别应是一层独立表现组件：不持有凭证、不知道租约写接口、不决定按钮权限。将来暂停动画、降低动效或完全关闭背景时，整个业务操作仍然完整可用。

## 11. A / B 接管边界与建议实施顺序

| 工作项 | B 可直接推进 | 需要 A 确认 / 提供 |
|---|---|---|
| 壳层、双空间、旧路由兼容、清爽工作台 | 新布局、导航、草稿内存、动效、空态和错误态 | 无需改变业务写语义 |
| 现有页面重新组合 | 提取组件、接现有读 API、保留回执与安全下载 | 如发现合同缺口再协商，不直接修改内核状态 |
| 首页关注摘要 | 明确有限范围的前端 selector；新聚合读模型设计与 query API | 原子读取、授权边界、必要端口支持；不能直接读写存储 |
| 单账号借用 + 贡献 | 产品入口与按能力呈现 | 身份授权模型、兼容方案、owner 关系；同时协调查询契约 |
| 当前角色/能力投影 | 提出字段与入口需求，消费可信投影 | 认证来源与可信 actor；不得信任浏览器角色 |
| 工作名称、通知已读 | 在未支持时降级为短 ID / 本次收起 | 若决定持久化，需 A 命令路径和数据契约；B 不自行写账本 |
| 运行百分比 / ETA | 先做好阶段进度与未知态 | 是否有可信采样与测量定义；没数据就不呈现百分比 |
| 新 UI 验收 | 定向覆盖导航、身份、状态、下载、可访问性 | 最终装配、构建与真人交叉验收；历史 CP 回执不可代签 |

推荐顺序：先做到现有能力不丢失的双空间骨架；再做真实状态驱动的首页与工作详情；随后补贡献与管理员区域；最后联调交互、视觉与少量新增读能力。新的授权模型不应藏在 UI 重构提交里，性能目标也不因页面变漂亮而被改成通过。

## 12. 合并前最小必要验证清单

本文仅规定检查项，**没有执行这些检查，不记录 PASS**。它们是 UI 重构的定向回归，不要求每次重跑全部 125 组策略实验。

1. 旧工作、决策和事件深链接仍能直达；前进/后退、刷新和身份重新导入后的行为可解释。
2. 总览 → 工作台 → 总览不会重新登记工作；草稿、筛选、滚动恢复符合设计；身份切换则全部隔离。
3. 身份切换发生于查询/工件下载完成前后，旧内容与旧下载均不出现；无浏览器凭证持久化。
4. 模拟、CPU 参考、未知模式分别显示正确；未知模式禁止提交；镜像与命令的非执行语义清楚。
5. 正常完成、失败仍占资源、收回通知、停止未确认、checkpoint 未核验、已接续六种状态不被简化混淆。
6. 同一工作登记未知结果、明确 stale version、epoch 改变、合法动作失效分别保留正确的重试边界。
7. 分页范围与计数范围准确；完整关注数尚无投影时不伪造全量通知。
8. 普通身份看不到管理员数据；其他人工作/工件的 404 不因缓存或搜索建议泄露信息；贡献收回仍校验所有权。
9. CPU 结果、checkpoint、实验工件、历史报告在真实浏览器完成下载；含大整数样本下载字节不经数值往返；原文和摘要导出命名准确。
10. 报告保留历史 / 开发 / 当前验收用途、负结果、未运行与未达标；没有因绿色样式误导结果。
11. 鼠标、键盘、触控板、禁用动效和窄屏都能到达同一业务操作；无手势亦可使用。具体手势阈值与浏览器冲突测试以主设计说明为准。

完成实现后由 A / B 对最终构建分别记录实际观察，再进入既定真人干净部署与交叉操作。设计评审通过、组件测试通过、浏览器观察完成和最终联合验收通过是四种不同结论。

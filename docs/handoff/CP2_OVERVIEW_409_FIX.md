# 管理员总览 409：A 线修复与 B 接线

## 问题与处理

来源是 B 提供的 `CP2_管理员总览409_A线修复交接_20261114.zip`，针对 CP2 基线 `aa63fb6`。报告中的 CPU 8 次请求有 7 次 409 是 B 的观测；本分支另用两条独立 SQLite 连接和屏障，强制在读取之间提交资源变更或时钟推进，验证旧路径产生 409。

根因：`GET /x/v1/admin/overview` 分别获取业务记录和库存，两个合法快照不一定同版本。修复后查询装配调用一次 `kernel.export_state(actor)`，由该快照同时构造 `ReadSnapshot` 和 `InventoryRead`。不重试、不暂停时钟、不返回空数据，也不删除版本校验。

内部增加 `OverviewRead` 及可选 `QueryServices.overview_reader`。默认应用装配使用单快照 reader；旧测试或外部装配仍可注入原有 reader，且版本不一致仍明确返回 409。`NodeInventory`、`WorkIntent` 保持完整严格验证；仅允许租约、任务导出投影去掉其内核私有字段。

管理员角色在读 provider 之前检查。响应保留 `version`、`now`、`logical_time`、`scope`、`owner_id`、各项计数与状态统计、容量明细和汇总、`measurement_kind`、`performance_measurements`、请求标识与 `no-store` 头。没有改公开 DTO、认证方式或前端调用参数。

## 同时修复的首次登记竞争

旧 CP3 提交 `6e3afe5` 的 GitHub CI 捕获首次 `POST /x/v1/work-intents` 偶发 `stale_version`：请求没有指定 `If-Match`，HTTP 层却先读取版本，再提交登记；CPU tick 可能先一步写入。现在这种登记在同一写事务内确定版本并提交；带显式 `If-Match` 的请求继续执行条件写入，后续 allocate/create_job 阶段也仍要求版本。

命令封装仍含版本，稳定工作 ID 的去重、参数冲突和首次到达时间在原事务边界核验。这个修复不把登记、分配和任务创建合并，不改变阶段回执或中断恢复语义，也没有用重试掩盖 CI 失败。

## B 如何接入

先保存自己的未提交工作，在自己的 CP3 分支执行：

```sh
git fetch origin
git merge origin/work/a-cp3
uv sync --locked
uv run pytest -q tests/blackbox/test_overview_atomic.py tests/blackbox/test_registration_atomic.py
uv run python tests/acceptance/cp3/cp3_overview_snapshot.py --output evidence/cp3/overview_snapshot_receipt.json
```

若查询装配出现合并冲突，保留 `make_kernel_services` 的 `overview_reader` 绑定，以及路由调用 `services.overview(actor)` 的单快照路径；不要退回两次独立读取。合并后的正式技术复验入口为 `make cp3-a`。

验证器会启动并清理自己的逻辑和 CPU 实例，不使用双方人工验收的端口、数据库或凭据。每种模式有两位 Owner 的运行及完成历史，随后各连续请求总览 50 次，要求全部 200 且不重试；CPU 时钟必须实际推进。只读 SQLite 对照检查业务状态没有被 GET 修改，非管理员访问要求 403。原始 HTTP 记录、检查项、失败原因和构建身份保存在上述 JSON。

## 界面复核与放行边界

B 合并后用自己的 CPU 实例：以 admin 打开“系统验收”并多次刷新总览；应持续读到库存与运行统计，不再出现版本冲突。切回 alice/bob 时不得获得管理员总览。确认记录对应合并后的提交和实例，不能沿用旧构建截图。

`evidence/cp3/a_core_receipt.json` 是 A 侧完整回归结果；`overview_snapshot_receipt.json` 是本问题的专项结果。只有当前构建实际通过的回执才算通过。CP2 的历史人工验收不被覆盖；本次修复也不替代 CP3 的策略、重放、解释及产品联合验收，不能据此进入 CP4。

# CP2 B → A：三角色、接续与隔离实验

B 分支 `feat/b-cp2` 合入 A `7af5d0c` 和 B 的 CP1 审计修复 `d1c51b7`。用户明确授权进入 CP2。当前代码可进行联合验收；自动回执与代理浏览器观察不能替代两位队员的人工记录。

## 已接线的行为

| 工作区 | CP2 行为与事实来源 |
|---|---|
| 运行工作台 | 读取服务执行模式和能力；登记中断策略、保存窗口、恢复与失败重试预算；展示同工作、同任务的新代次 |
| 资源与贡献 | 读取授权库存与贡献；贡献者发布节点并收回自己拥有的物理 GPU；原子提交，保留真实操作回执 |
| 任务连续性 | 分别展示报告、核验、接续事实；保存窗口、停止待确认、恢复等待、预算与原截止时间；下载授权 CPU 工件 |
| 决策解释 | 读取当时候选及实际提交；P 的当前机会损失分量与稳定同分规则；继续执行跨 Owner 脱敏 |
| 事件与实验 | 按工作定位事件、直接跳转决策、页首导出；管理员可运行预登记独立场景及下载输入/事件/决策/结算 |
| 系统验收 | 当前构建真实回执、缺失项明确 NOT_RUN；逻辑实例可推进时钟，CPU 实例不显示手动时钟控制 |

写操作始终经过 A 的授权命令。仅已确认的 `stale_version` 允许有限刷新重试；网络中断或不确定写结果先查询，不自动重复发送。新 attempt 出现不等于保存点已加载；`continuation_status=applied` 才显示真正应用。已请求释放但进程尚未确认停止时，仍显示占用。

P 与 B0/B1/B2 使用相同合法候选和 512 候选上限。P 保留 B1 准入次序，按当前可见候选计算其他 Owner 的放置机会损失，再用 B2 同分键。它不读取未来场景或隐藏工期；恢复成本缺乏已核验模型，明确未启用。当前没有 CP3 五族五 seed 的正式收益结论。

## 本机两个独立实例

逻辑 API `18083` / Web `15176`；真实 CPU API `18084` / Web `15177`。保留旧 CP1 端口 `18081/15174` 和 CP1 补修 `18082/15175`，不重置旧库。以下命令在本仓库执行，需要先 `make setup`。

```sh
# 逻辑 API，脚本仅首次创建本地身份文件
FABRIC_INSTANCE=cp2-b-logical FABRIC_MODE=logical_simulation \
FABRIC_SCHEDULING_MODE=external FABRIC_B_POLICY=P PORT=18083 \
bash web/dev-local.sh api

# 另一终端启动逻辑 Web
FABRIC_API_TARGET=http://127.0.0.1:18083 WEB_PORT=15176 bash web/dev-local.sh web

# CPU 身份只首次创建；存在时保留，勿覆盖
uv run python scripts/init_cp1_demo.py runtime/cp2-b-cpu
FABRIC_INSTANCE=cp2-b-cpu FABRIC_DATA_DIR="$PWD/runtime/cp2-b-cpu" \
FABRIC_AUTH_FILE="$PWD/runtime/cp2-b-cpu/auth.json" \
FABRIC_INVENTORY_FILE="$PWD/configs/cp1_inventory.json" \
FABRIC_MODE=cpu_reference FABRIC_SCHEDULING_MODE=external FABRIC_B_POLICY=P \
uv run python -m uvicorn --app-dir scripts cp2_cpu_preview:create_app --factory \
  --host 127.0.0.1 --port 18084

# 第四终端启动 CPU Web；先让第一个 Vite 完成依赖预处理
FABRIC_API_TARGET=http://127.0.0.1:18084 WEB_PORT=15177 bash web/dev-local.sh web
```

两个页面分别导入对应目录的 `client-identities.json`，选择 alice、bob、supplier、admin。该文件只在浏览器内存解析，不入 Git 或交付包；测试密钥不进入网页。服务均绑定本机。

CPU 演示工厂明确采用 1,000,000 步固定整数程序，每 1,000 步暂停 50ms（未中断约 50 秒），方便操作保存/收回。暂停是演示节奏，不能作为吞吐测量。保存、停止、接续与结果由真实进程执行；提交的 image/command/env 不会被执行，GPU 库存仍为模型。

CPU 参考实例的库存由启动配置载入，页面仍可执行贡献者收回。当前 A 登记接口要求 `observed_at` 严格等于提交时逻辑时间，持续走时的 CPU 模式无法可靠手填，因此页面不开放该模式的手工 JSON 登记；新增供给的在线操作在逻辑实例验收。要在 CPU 模式动态登记，需 A 提供原子观测与登记语义，不能靠无界重试掩盖。

## 六步联合实操

1. **研究者提交**：alice 在运行工作台选“保存点恢复”、恢复上限 2、保留租约，提交一项工作。记录工作/租约/任务 ID。查看连续性和资源占用。
2. **贡献者收回**：supplier 在资源与贡献找到被占用的物理 GPU，选择设备，通知窗口建议 3 秒，收回时长 8 秒。保存回执；确认研究者不能代替贡献者收回，也不能查看别人的工作。
3. **保存及等待**：逻辑模式下 alice 提交 ready/failed 报告，admin 推进至 deadline；CPU 自动保存，不能手填 URI 冒充核验。检查报告/核验/接续分列，以及停止待确认状态。点击恢复，重复等待请求不增代次或消耗预算。
4. **真正接续**：逻辑模式由 admin 推进至收回结束，CPU 自然推进。回到 alice 刷新，核对同工作、同任务与租约，新 generation，截止时间不延长；CPU 保存工件 verified 后实际加载才显示 applied。任务完成后下载结果并核对租约是否归还。
5. **可追问与隔离**：从工作直接打开事件和关联决策，展开实际提交；导出当前工作事件。切 bob 查询 alice 工作得到 404，不能下载 alice 工件。逻辑结果不应冒充真实 CPU 文件。
6. **实验与验收**：admin 在事件与实验选择预登记场景、策略、seed，运行并下载原始输入/决策/事件/汇总。检查业务账本不变。系统验收应如实显示未完成的双人回执；A、B 各自保存六区操作事实后才能联合放行。

另由自动后端门槛覆盖 failed、timeout、连续收回、无替代、预算耗尽、大步时钟、节点故障与 reset 隔离，避免只验收成功路径。

## 可复现检查与证据

```sh
make cp2-a       # A 八项 CP2 + CP0/CP1 回归，真正启动隔离 HTTP/CPU
make cp2-b       # B 查询/策略/工作流/页面回归与四策略 + CPU TCP 旅程
uv run python experiments/run_scenarios.py --help
uv run python tests/acceptance/cp2/cp2_six_workspaces.py --agent-qa-only
make checkpoint CP=CP2  # 缺双人证据应失败，不能强行改为 MET
```

运行器只接受预登记场景、策略 B0/B1/B2/P 和 0–9999 seed，单后台 worker、最多 4 项未完成和 128 条记录。每次创建独立临时 Kernel，不调用在线实例的 reset。完整工作队列包括失败与未完成；异常、构建变化、进程中断不会冒充成功。输入、策略、模型、计算器及源码哈希写入记录。仅管理员可启动/读取实验，下载时核对登记路径和内容哈希。

本地自动证据位于 `evidence/cp2/`；B 实际 HTTP 历史与回执在 `evidence/cp2/b/`。UI 核验器读取当前构建的 `ui_receipt.json` 和工件哈希，代理身份写 `observer_kind=agent`。正式 UI 门槛另要求 `human_handoff.json` 中 A/B 两位不同的真人及各自六区记录。核验器检查声明与工件完整性，不能证明实际操作者身份；不预填人工通过。

跨边界未启用项：恢复成本模型需后续契约支持，CP3 正式实验需冻结独立模型与验证集；两者都不应被当前 CP2 演示替代。A 官方文件、共享 DTO 与领域写路径保持原有归属。

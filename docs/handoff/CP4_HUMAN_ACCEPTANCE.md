# CP4：真人交叉验收与交付证据

本页是待执行清单，不是通过记录。只在最终候选提交冻结后操作；修订源码、测试或文档都会改变构建摘要。原 CP3 人验、自动角色切换、代理的干净克隆均不能替代本轮两位真人。

## 冻结与身份

记录 `uv run python scripts/check_checkpoint.py --build-id` 的完整输出。A 与 B 分别以自己的姓名或可识别账号签署自己的观察，不让代理填 `attested: true`。一人两账号仍只算一位操作者。

所有材料放在 `evidence/cp4/`。每个引用写仓库相对路径与文件 SHA-256，不能指向旧 CP3 目录、符号链接或文件自身。凭据不加入证据。截图来自真实 1440×900 CSS 浏览器视口，同时记录 `device_pixel_ratio`，保留原 PNG；例如1倍截图为1440×900、Retina 2倍为2880×1800。不能把其他视口截图缩放成符合要求的图片。

## 不同操作者的干净部署

由不是本轮候选准备者的真人，在新的目录克隆候选提交。按当前 README 完成依赖安装、契约检查、前端构建与两个服务启动。使用自己的目录和端口，不重置已有实例。

保留初始空工作区状态、实际提交/源码摘要、当前 README 摘要、每条命令及完整输出、服务健康/版本 HTTP 响应、未登录写入 401、浏览器首屏截图。记录真实机器、实际路径和操作者。登记到 `evidence/cp4/clean_deploy_observer.json`，使用 `cp4-clean-deploy-v1`；字段与检查代码见 `tests/acceptance/cp4/_delivery_review.py:clean_deploy`。

`uv run python tests/acceptance/cp4/cp4_clean_deploy.py --agent-qa` 可预先自动检查真实干净克隆、依赖和启动。它写入独立 `agent-clean-deploy/<时间>/agent_qa.json`，其中 `formal_human_gate_satisfied=false`；这一记录不能改名代替真人观察。

## 两人分别交叉完成六项任务

每位操作者都执行以下六项。每项保留实际按钮/输入顺序、响应、观察结果及原截图；不要仅写 PASS。使用服务数据完成操作，不把组件测试或手写 JSON 当业务执行。

| 任务 ID | 操作与必须看见的结果 |
|---|---|
| `submit-and-follow` | 从运行工作台登记新工作，进入任务连续性查询同一工作。额外完成空列表和部分成功场景：资源申请已成功而后续创建被拒绝，重试保持原租约，没有重复借卡。保留成功和失败响应、原租约 ID。 |
| `contributor-recall` | 贡献者实际收回自己的资源，并尝试收回另一贡献者的资源。前者成功，后者由服务端拒绝；在资源与连续性两区核对通知和状态。 |
| `checkpoint-and-recover` | 在 CPU 参考实例运行保存—停止—恢复。查询响应必须分别显示工件 `verification_status=verified`、`continuation_status=applied` 和新 `resumed_attempt_id`；逻辑 ready 不满足此项。 |
| `explain-decision` | 从工作关联导航打开实际决定，核对当时合法候选、选中动作与已提交事件；`commit_status=committed`，不是仅显示计划。 |
| `events-report-and-download` | 读取工作关联事件和冻结的策略报告；保留原执行构建，R=1.00 与覆盖缺口不得改写。点击浏览器下载，保存实际下载文件并逐字节对照服务端工件。 |
| `administrator-acceptance` | 读取验收状态，再连续刷新管理员总览十次。每次真实响应均为 200；出现任何 409 或间歇错误应保留，不删失败后补十次成功。 |

每人都要覆盖 `error`、`empty`、`partial-success` 三种条件，可分布在六项任务中。每个任务的原始历史使用 `cp4-ui-history-v1`：`build`、`task_id`、`human_id`、从1递增的 `actions`（含 `operation`、`observed`）、实际 `requests`（`method`、`path`、`status_code`、`response`）。下载还需 `downloads`，逐项引用 `file` 与原 `server_artifact`；部分成功还需 `retained_allocation_id` 与 `duplicate_allocation_created=false`，以及前后两次同工作、同一租约的实际详情。异步阶段失败可通过 HTTP 200 详情中的已提交资源阶段和带 rejected 回执的任务失败阶段证明，无须伪造 HTTP 错误状态。

联合记录 `evidence/cp4/user_tasks_observer.json` 使用 `cp4-user-tasks-v1`。根含 `build`、带时区 `observed_at`、空的 `unresolved_issues`、两位 `operators`。每人有不同 `human_id`、`observer_kind=human`、角色 `A` 或 `B`、本人确认的 `attested` 和六个 `tasks`。每项写 `id`、`status`、具体 `actual`、`viewport`、`workspaces`、`conditions`、`screenshot` 与 `history` 引用。缺项应留待完成，不能补造观察。

## 同构建视频候选

保留一次至少30秒的连续屏幕排练实录，覆盖实际场景的三个以上步骤。这里检查 CP4 候选一致性；不冒称完成 CP5 的3–5分钟最终演示或外部提交。

记录 `evidence/cp4/video_rehearsal_observer.json`，使用 `cp4-video-rehearsal-v1`：当前 `build`、真人 `operator`、`video`、`scene`、`deployment_observation`、`model`、`scene_id`、`credentials_visible=false`、时间及未解决项。场景文件使用 `cp4-rehearsal-scene-v1`，含相同构建、场景与执行模式，`recording_method=continuous_screen_recording`、`edited_for_outcome=false`、带实际时间码的 `steps`。视频、场景和部署观察的模型必须一致。验证器用 ffprobe 检查真实视频时长和流，不能用封面图、计划或录制脚本替代媒体。

## 图表与原始数据

`cp4_evidence_index.py --prepare-index` 只复制与复算封存数据，不重跑125次实验。它把原字节无损压缩，并同时保存压缩文件哈希与原文件哈希。原实验构建、模型、输入、策略、run 和原始快照保持原值；CP4 当前构建只代表审核和展示的版本。

索引覆盖策略报告总览、场景族表、运行表，以及当前研究报告的动作计数、F 分解、微型对照和 Owner 伤害结果。MCM 资料列为历史引用，不宣称本轮重做旧实验。当前报告新增数值图表后，必须先更新完整图表目录和计算记录，再验收。

显式 `--prepare-index` 或 `--deep-audit` 从原快照和决策重算，写 `figure_deep_audit.json`。页面和普通 `review()` 复核当前构建、该深审记录、索引、计算程序和每个保留源文件的哈希，不在每次页面请求重新解压上千兆原始JSON。源数据、图表或计算程序改变后旧深审立即失效；这是完整性审核，不宣称每次页面读取都重做演算。

## 实际审核命令

```sh
uv run python tests/acceptance/cp4/cp4_clean_deploy.py
uv run python tests/acceptance/cp4/cp4_user_tasks.py
uv run python tests/acceptance/cp4/cp4_evidence_index.py
uv run python tests/acceptance/cp4/cp4_video_rehearsal.py
```

四个入口只读源观察，写单项审核结果；缺记录为 NOT_RUN，过期、篡改、缺原始材料、代理冒充真人或记录不一致为 NOT_MET。它们能校验证据一致性，不能独立认证镜头外是谁操作；仍需要两位真人对自己的观察负责。正式 CP4 还必须满足可靠性、规模、延迟等其余门槛，不由这四项单独放行。

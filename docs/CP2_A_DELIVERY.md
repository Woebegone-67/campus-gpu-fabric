# CP2 A：回收、接续与真实 CPU 执行交付

日期：2026-11-14；开发分支 `work/a-cp2`。本页记录 A 后端交付，**不代表联合 CP2 已通过**。用户已在会话确认双方完成 CP1 人工验收，并明确授权进入 CP2；历史 CP1 审查报告保留当时状态。

## 本轮实现

- 官方 21 个操作均已挂接实际处理器。新增 checkpoint、resume、contributor-demand、node-state 与 fault，移除此前的 501 占位。
- 同一持久内核处理通知、保存截止、租约到期、作业超时、节点失联、恢复等待及两种预算。恢复保持 allocation/job 身份，只有新执行代次提交时增加对应预算。
- 普通贡献者可登记供给并收回自有 GPU；其请求经 contributor bearer 与内核所有权检查，不使用测试密钥。
- CPU reference 模式运行独立 Python 进程：真实保存、停止确认、工件核验、新代次接续及完整结果对照。持久 outbox、执行日志和代次校验处理重复投递与服务重启。
- 未确认停止继续持有资源；reset 清空公开工作状态后保留私有旧进程隔离，停止确认才解除。重新登记同一物理 GPU 的别名不会绕过隔离。
- 429/500/503 与 latency 故障按官方方法/路径模板匹配，次数有界并持久保存。故障在业务提交前生效；测试操作与产品扩展不作为注入目标。

实现集中在 `domain/kernel.py`、`domain/lifecycle.py`、`executors/`、`api/mutations/http.py`、`api/test_control/faults.py` 和 `app.py`，均位于 `src/campus_fabric/`。

## 对 B 的交汇变化

准确请求、字段、启动方式和操作清单见 [CP2 A→B 交接](handoff/CP2_A_TO_B.md)。主要新增：

| 对象 | 新增字段或入口 |
|---|---|
| `AllocationView` | `preemption`、`checkpoint`、`restart_count`、`pending_terminal` |
| `JobView` | `execution_mode`、`retry_count`、`checkpoint`、`result_uri`、`failure`、`finished_at` |
| 产品写入口 | `POST /x/v1/contributions/recall`；WorkIntent action `resume` |
| 工件读取 | `GET /x/v1/artifacts?uri=…`，只接受本实例登记工件，并核对 owner/epoch |
| 能力发现 | `/v1/version` 的 `execution_mode`、`capabilities`；`checkpoint_passed` 仍为 `false` |

跨入 B 目录的改动仅为 `workflows/devhost.py` 合并 lifespan：保留 B 的工作流循环，同时进入 A 的 CPU 驱动生命周期。未重写 B 策略、投影或六工作台。`web/src/generated/contracts.ts` 随共享 JSON Schema 再生成；它是类型交汇产物，不代表界面已展示新字段。

## 验证与证据

下列入口均使用临时目录、随机凭据及隔离端口，不操作 CP1 人工实例。当前开发过程中已运行的单项收据不能替代最终源码冻结后的统一汇总。

| 验证范围 | 入口与主要证据 |
|---|---|
| 21 操作、身份、贡献者与故障 | `uv run python tests/acceptance/cp2/cp2_all_operations.py` → `evidence/cp2/http_operations_receipt.json` |
| 真实 TCP + CPU 进程接续 | `uv run python tests/acceptance/cp2/cp2_cpu_reference.py` → `evidence/cp2/cpu_tcp_receipt.json` |
| 回收、时钟、节点、reset 反例 | `tests/acceptance/cp2/cp2_preemption.py`、`cp2_clock_boundaries.py`、`cp2_node_failure.py`、`cp2_reset_isolation.py` |
| 领域/独立历史 | `tests/domain/test_cp2_adversarial.py`、`tests/concurrency/test_cp2_history.py` |
| 真实适配器与内核驱动 | `tests/executors/` |

完整入口为 `make cp2-a`：执行契约、静态检查、完整 Python 测试、Web 构建/客户端测试、CP0 冒烟、CP1 回归和八项 CP2 注册验证器。实际数量、结果与构建身份以新生成的 `evidence/cp2/a_core_receipt.json` 和 `unit_tests.xml` 为准。独立审查曾复现取消/超时与回收共享停止确认时租约无法结清的问题；修复及顺序回归已纳入测试。CPU 时钟已持久保存单调时间锚点，同一主机启动周期内重启先结清停机期间到期事件，再消费迟到进程观察；主机重启改变 boot identity 时拒绝自动继续，跨 boot 恢复留待 CP3。

`make cp2-a` 成功仅表示 A 的八项后端门槛通过；同时生成 `evidence/cp2/receipt.json`，其中联合状态保持 `NOT_MET`，第九项 `CP2-SIX-WORKSPACES` 保持 `NOT_RUN`。文档中的 `web/dev-local.sh` 启动方式已用临时目录和随机本机端口实际检查：逻辑后端、Web 代理、四个本地身份生成及 CPU 集成 host 启动均通过；该启动检查没有提交 CPU 工作，不代替接续验收。最终验收以 `evidence/cp2/` 中与当前源码对应的实际记录为准。`configs/checkpoints.json` 仍是判据，不能为了通过改变分母或把未执行项改为 MET。CI、单项后端回执与联合门禁分别记录。

## 联合 CP2 尚未完成

| 工作 | 当前状态 |
|---|---|
| A 后端实现与独立专项验证 | 已实现；当前构建运行结果见 `evidence/cp2/a_core_receipt.json` |
| B 六工作台接入 CP2 状态、动作与错误语义 | `NOT_RUN`：需 B 接线和实际核验 |
| researcher / contributor / administrator 三角色 CP2 交叉操作 | `NOT_RUN` |
| 保存报告、工件已核验、真正接续三层事实在 UI 中区分 | `NOT_RUN` |
| 联合 CP2 当前构建放行 | 尚未放行 |

CP1 已记录的导航切换身份、导入身份文件缺少反馈等交互问题保留在后续界面验收范围；身份是否真正改变应以当前 bearer 和服务器授权结果核查，不能只看显示名称。

## 能力边界

`logical_simulation` 保留固定 10 秒模型，只有显式推进时钟才完成。`cpu_reference` 运行固定整数参考计算，**不执行提交的 image、command 或 env，也不执行 GPU/Kubernetes 工作负载**。库存文件仍是模拟拓扑；真实 CPU 进程证据不将该拓扑变成真实 GPU 集群。

工件 HMAC 验证的是受信本地执行目录中的来源与完整性，不是远程硬件认证。失去进程身份、无法确认停止或永久 dispatch 拒绝时维持隔离；自动重试不等于已经恢复。POSIX 进程控制适用于当前 macOS/Linux 路径，Windows 未验收。

本轮没有提交比赛、没有改变 CP1 人工实例，也未将后端通过视为 B 产品验收通过。代码与反例由 AI 协助实现和审查，属于团队内部验证。

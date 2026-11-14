# 织算 Campus Fabric

校园共享计算服务：申请、收回、接续与可追问的决定。

**当前：进入双页UI设计交接；CP4完整验收仍未通过。** 059f477b候选的可靠性与图表来源检查已有证据，规模/延迟未达，真人干净部署、交叉操作及同构建实录尚缺。当前放置评分路线冻结，R=1.00保留为未达；新工作选择机制有局部收益，也存在严重Owner损害反例，未推广到产品。参见 [CP4后续诊断与评审](evidence/cp4-next/README.md)、[策略瓶颈](docs/research/CP3_BOTTLENECK_AND_DIRECTION.md) 和 [CP4准入记录](docs/handoff/CP4_ENTRY.md)。原CP3分支与证据仍保留，见 [CP3历史收尾](docs/handoff/CP3_CLOSEOUT.md)。阶段准入、设计交接与正式通过分别记录。

**B接手UI：** 先读 [双页界面设计说明](docs/design/UI_DESIGN_SPEC.md) 与 [UI接管步骤](docs/handoff/UI_B_HANDOFF.md)，再按需读 [双指滑动与转场](docs/design/UI_GESTURE_AND_MOTION.md)、[实际数据与组件映射](docs/design/UI_DATA_AND_COMPONENT_MAP.md)。交接分支 `work/ui-handoff-20261115` 包含当前代码与详细说明；新双页、手势和同账户使用/贡献能力尚待实现。用户已授权文档推送后同步开始后端能力改造。

CP1 审计补修包含身份应用、重复查询、授权决策解释、工作关联导航及验收回执读取，详见 [CP1 补修交接](docs/handoff/CP1_B_FIXES.md)。原人工验收实例保留，补修版使用独立工作树和端口。

协作仓库：https://github.com/Woebegone-67/campus-gpu-fabric

仓库可见性以GitHub实时设置为准。当前私有访问需使用各自GitHub身份；xuyangma的write权限已生效。Codex桌面本地开发只需克隆后打开项目文件夹，不要求在GitHub应用安装列表选中仓库。切勿共享凭据。

| 负责人 | 主线 | 开发分支 | 先读 |
|---|---|---|---|
| A · Xiuzhu Du / `Woebegone-67` | 可信资源与执行 | `work/a-cp3` | [CP3接线](docs/handoff/CP3_A_TO_B.md) / [A任务书](docs/handoff/OWNER_A.md) |
| B · `xuyangma` | 服务决策与操作体验 | `feat/b-cp3` | [B开发任务书](docs/handoff/OWNER_B.md) |

## 启动与验收

CP4 新候选的独立启动、完整重放、规模负载及正式核验入口见
[CP4 候选实施](docs/handoff/CP4_CANDIDATE.md)。`make cp4` 实际运行可靠性检查并汇集七项证据；
`make cp4-collect` 只读复核现有材料。缺少真人、实录或未达到规模/延迟目标时仍返回非零，不能据此宣称 CP4 通过。
旧 CP3 报告可按候选说明显式配置只读归档及两个固定摘要；页面保留原实验构建与审核范围，历史报告不能替代当前构建验收。

需要Git、uv、Python 3.12、Node 22.12+（22.x）、npm和make。依赖固定在`uv.lock`与`web/package-lock.json`；无需 GPU 或 Kubernetes 即可运行逻辑模拟与固定参考 CPU 进程；CPU 进程控制要求 macOS/Linux。

```bash
git clone https://github.com/Woebegone-67/campus-gpu-fabric.git
cd campus-gpu-fabric
uv python install 3.12
make setup
make cp0
make checkpoint CP=CP0
```

`make cp0`检查生成契约是否过时，运行Python静态检查与测试、前端组件交互回归，构建Web，执行静态CP0验收，并启动真实HTTP子进程进行运行主机冒烟。回执在`evidence/cp0/`，属于本地生成物。`make checkpoint CP=CP0`检查这些回执是否对应当前构建。

运行两个终端：

```bash
# 终端1：新 CP2 逻辑实例，不触碰 CP1 运行目录
FABRIC_INSTANCE=cp2-b-logical FABRIC_MODE=logical_simulation \
FABRIC_DATA_DIR="$PWD/runtime/cp2-b-logical" \
FABRIC_AUTH_FILE="$PWD/runtime/cp2-b-logical/auth.json" \
FABRIC_INVENTORY_FILE="$PWD/configs/cp1_inventory.json" \
FABRIC_SCHEDULING_MODE=external FABRIC_B_POLICY=P PORT=18083 bash web/dev-local.sh api
# 终端2
FABRIC_API_TARGET=http://127.0.0.1:18083 WEB_PORT=15176 bash web/dev-local.sh web
```

访问`http://127.0.0.1:15176`；API为`http://127.0.0.1:18083`。首次启动在 `runtime/cp2-b-logical/` 创建本地演示凭据。在页面右上角“当前身份”导入该目录的 `client-identities.json`，再选择 alice、bob、supplier 或 admin。凭据只保留在浏览器内存；刷新后需重新导入。开发host仅绑定本机，Web代理不包含`/_test`。

每人使用自己的clone、端口与`runtime/<owner>`。上面的集成入口组装 B 查询与工作流，并保留 A CPU 驱动生命周期；A 的 `make api` 默认在8080提供内核接口，不自动推进 B 工作流。端口可通过 `PORT`、`WEB_PORT` 调整。不要把实验reset指向另一人的实例。环境变量需由启动 shell 显式设置，程序不会自动加载 `.env`。

## 从哪里开始读

0. [CP3 当前交付](docs/CP3_A_DELIVERY.md) 与 [CP3 接线](docs/handoff/CP3_A_TO_B.md)；[CP2 交付](docs/CP2_A_DELIVERY.md)及更早文档作为对应版本的历史记录。
1. 自己的任务书：[A](docs/handoff/OWNER_A.md) / [B](docs/handoff/OWNER_B.md)。
2. [共同checkpoint](docs/handoff/CHECKPOINTS.md)、[交接模板](docs/handoff/HANDOFF_TEMPLATE.md)。
3. [共享契约](docs/CONTRACTS.md)、[产品扩展接口](docs/EXTENSIONS.md)、[状态与占用](docs/STATE_MODEL.md)、[暂定语义及来源](docs/SEMANTICS.md)。
4. [终态产品](docs/PRODUCT.md)、[验收与证据](docs/ACCEPTANCE.md)。

`docs/blueprint/`保存本轮已选择的完整蓝图，按需查阅。正式依据是[challenge](spec/official/challenge.md)和[原始OpenAPI](spec/official/api/openapi.yaml)；[补充说明](spec/official/participant_supplement.pdf)解释题意。`spec/official/manifest.json`核对原字节，绝不静默修订官方文件。

## 工程地图

```text
spec/official/                冻结题目与公共API
contracts/                   21操作责任表、生成schema、正反样例
src/campus_fabric/
  contracts/                 Pydantic DTO与Protocol，内部交汇唯一来源
  domain/ storage/ clock/    A：权威状态、持久化、逻辑时间
  auth/ executors/           A：授权、模拟/真实执行适配
  api/mutations/            A：写HTTP
  api/test_control/         A：隔离测试控制
  api/queries/              B：授权查询
  api/extensions/           按queries/mutations归属
  projections/ policies/    B：读投影、策略纯函数
  workflows/                B：服务端工作流，持久写入经A命令端口
web/                         B：React/TypeScript六工作区
experiments/                 B：封存输入、基线/消融、分析
configs/                     集中语义与checkpoint标准
tests/ scripts/              各自自测与共同验收入口
docs/handoff/                可直接交给人或AI的任务书
evidence/                    实测回执；大批运行在本地生成
deploy/                      A：后续部署配置
```

更新`src/campus_fabric/contracts`后运行`make contracts`，同时提交JSON Schema与TypeScript产物。`make cp0`会拒绝过时生成物。契约模型核验数据形状和局部语义；物理可行性、原子账本、权限仍要由A内核独立保证。

## 协作与合入

首次从自己的分支开始：

```bash
git fetch origin
# A使用以下一行；B改为work/b-product
git switch work/a-cp3
git pull --ff-only
```

沿各自目录连续开发，小而完整地提交，通过PR合入main。跨边界变更先发准确契约diff；共享契约由A合入、双方检查。两人各自写测试和说明，不把全部验收杂务交给B。

提交前运行`make cp0`；后续使用`make checkpoint CP=CP1`等入口，缺失/过期/未运行证据应失败。不能拿CP0的501占位测试替代业务测试：实现对应操作时更新责任表、CP0状态检查和业务验收，保持“实际实现什么就声明什么”。

CI在push/PR运行 `make cp3-a`：A CP3 可靠性门槛、CP0–CP2 回归、B 页面组件与真实 TCP 接续检查。代码审阅责任写在`.github/CODEOWNERS`；它与远端分支保护是否启用是两回事。CP1–CP5的高标准定义不会因一个构建成功自动达成。

## 来源与边界

- 当前有两种隔离模式：`logical_simulation` 是固定 10 秒模型；`cpu_reference` 运行真实固定整数参考程序。两者均不运行提交的 image/command/env，也不代表真实 GPU 执行。CPU 模式的目录、请求模式和证据见 [CP2 接线](docs/handoff/CP2_A_TO_B.md)。
- 团队暂定语义集中在`configs/semantics.json`。正式答疑到达时局部调整，不改官方原件。
- 内部契约与产品扩展不增加基础客户端必填字段。WorkIntent计时从意图到达开始，不从获配后创建job开始。
- 仓库用于竞赛协作，尚未添加开源许可证。第三方库保留其许可；AI协助生成及测试 CP0–CP3 代码和文档，记录见各阶段交付文档。
- 此仓库不含旧MCM原始数据、历史实验、论文或凭据。

## 验证入口与历史记录

合并后的技术验收入口为 `make cp1-joint`，它实际启动 A+B 集成进程，检查自动编排、并发、响应丢失、硬重启及部分成功。它不代替双人操作。[CP1 联合验收](docs/review/CP1_ACCEPTANCE_DECISION.md)和[交接清单](docs/review/CP1_HUMAN_HANDOFF.md)保留早期审查状态；之后用户在会话确认双方完成操作并授权 CP2。该确认不替代新构建的 CP2 联合回执。

```bash
make cp1-a
# 使用明确模拟库存；默认本地 X-Owner 身份，不开放公网
FABRIC_INSTANCE=owner-a FABRIC_INVENTORY_FILE=configs/cp1_inventory.json make api
```

`make cp1-a` 在隔离目录启动真实 HTTP 进程，检查并发、租约、响应丢失和重启，写入 `evidence/cp1/a_core_receipt.json`。它不替代 `make checkpoint CP=CP1` 的双人 UI 与联合门槛。没有联合证据时，该门槛应继续失败。库存仅首次创建数据库时载入；reset 后保持空库存，重启不会偷偷补回。

配置三角色本地演示：

```bash
uv run python scripts/init_cp1_demo.py runtime/demo
FABRIC_DATA_DIR=runtime/demo FABRIC_AUTH_FILE=runtime/demo/auth.json FABRIC_INVENTORY_FILE=configs/cp1_inventory.json make api
```

`runtime/demo/client-identities.json` 是本机调用所需的请求头，只交给本地开发界面，不加入源码、构建或录像。测试密钥另行在服务端设置；普通用户和贡献者路径不需要测试后门。

CP2 A 完整验收与专项入口：

```sh
make cp2-a
# 以下为按需单项复验
uv run python tests/acceptance/cp2/cp2_all_operations.py
uv run python tests/acceptance/cp2/cp2_cpu_reference.py
```

两份脚本在独立临时实例运行真实 TCP 请求，后者启动真实 CPU 进程。回执写入 `evidence/cp2/`，源码更新后须重新核验构建匹配。B 六工作台、三角色交叉操作和联合 CP2 放行独立记录，不由后端检查自动通过。

B CP2 技术回归：`make cp2-b`。它不自动签署人工验收。隔离场景 CLI、CPU 演示启动及六工作台人工步骤见 [CP2_B_TO_A](docs/handoff/CP2_B_TO_A.md)。

CP3 A 完整入口：

```sh
make cp3-a
# 联合门槛：B 与联合证据未齐时必须失败，不能以 A 通过替代
make checkpoint CP=CP3
```

专项入口与证据范围见 [CP3 交付](docs/CP3_A_DELIVERY.md)。测试创建独立临时目录和端口；不需要重置现有人工验收实例。

B CP3 入口见 [B 交付与边界](docs/CP3_B_DELIVERY.md) 和 [冻结实验协议](experiments/CP3_POLICY_SUITE.md)。在已提交的稳定构建上依次冻结、运行、分析实验，再执行 `make cp3-b`；它合并同构建 A 回执，明确保留效果未达标与完整重放、场景覆盖缺口。报告可在“事件与实验”读取，结果和租约结算可在“任务连续性”首屏查看。管理员总览 409 的 A 侧修复已合入，联合验收会重跑真实 HTTP 回归。新人工实例使用独立目录与端口，旧 CP2 实例保留。

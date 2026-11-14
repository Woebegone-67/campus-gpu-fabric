# 织算 Campus Fabric

校园共享计算服务：申请、收回、接续与可追问的决定。

**当前：CP2 A 后端交付，等待 B 接线与联合验收。** 用户已确认双方完成 CP1 人工验收。A 已实现官方 21 操作、回收/恢复、节点与故障控制，以及真实 CPU reference 保存接续；B 六工作台和三角色的 CP2 验收仍为 `NOT_RUN`。先读 [CP2 交付记录](docs/CP2_A_DELIVERY.md) 与 [A→B 接线说明](docs/handoff/CP2_A_TO_B.md)。默认使用 builtin 基线，本轮不宣称新的调度收益。

协作仓库：https://github.com/Woebegone-67/campus-gpu-fabric

仓库可见性以GitHub实时设置为准。当前私有访问需使用各自GitHub身份；xuyangma的write权限已生效。Codex桌面本地开发只需克隆后打开项目文件夹，不要求在GitHub应用安装列表选中仓库。切勿共享凭据。

| 负责人 | 主线 | 开发分支 | 先读 |
|---|---|---|---|
| A · Xiuzhu Du / `Woebegone-67` | 可信资源与执行 | `work/a-cp2` | [CP2接线](docs/handoff/CP2_A_TO_B.md) / [A任务书](docs/handoff/OWNER_A.md) |
| B · `xuyangma` | 服务决策与操作体验 | `feat/route-b` | [B开发任务书](docs/handoff/OWNER_B.md) |

## 启动与验收

需要Git、uv、Python 3.12、Node 22.12+（22.x）、npm和make。依赖固定在`uv.lock`与`web/package-lock.json`；无需 GPU 或 Kubernetes 即可运行逻辑模拟与固定参考 CPU 进程；CPU 进程控制要求 macOS/Linux。

```bash
git clone https://github.com/Woebegone-67/campus-gpu-fabric.git
cd campus-gpu-fabric
uv python install 3.12
make setup
make cp0
make checkpoint CP=CP0
```

`make cp0`检查生成契约是否过时，运行Python静态检查与测试，构建Web，执行静态CP0验收，并启动真实HTTP子进程进行运行主机冒烟。回执在`evidence/cp0/`，属于本地生成物。`make checkpoint CP=CP0`检查这些回执是否对应当前构建。

运行两个终端：

```bash
# 终端1：新 CP2 逻辑实例，不触碰 CP1 运行目录
FABRIC_INSTANCE=cp2-a-logical FABRIC_MODE=logical_simulation \
FABRIC_DATA_DIR="$PWD/runtime/cp2-a-logical" \
FABRIC_AUTH_FILE="$PWD/runtime/cp2-a-logical/auth.json" \
FABRIC_INVENTORY_FILE="$PWD/configs/cp1_inventory.json" \
FABRIC_SCHEDULING_MODE=builtin PORT=18082 bash web/dev-local.sh api
# 终端2
FABRIC_API_TARGET=http://127.0.0.1:18082 WEB_PORT=15175 bash web/dev-local.sh web
```

访问`http://127.0.0.1:15175`；API为`http://127.0.0.1:18082`。首次启动在 `runtime/cp2-a-logical/` 创建本地演示凭据。在页面右上角“当前身份”导入该目录的 `client-identities.json`，再选择 alice、bob、supplier 或 admin。凭据只保留在浏览器内存；刷新后需重新导入。开发host仅绑定本机，Web代理不包含`/_test`。

每人使用自己的clone、端口与`runtime/<owner>`。上面的集成入口组装 B 查询与工作流，并保留 A CPU 驱动生命周期；A 的 `make api` 默认在8080提供内核接口，不自动推进 B 工作流。端口可通过 `PORT`、`WEB_PORT` 调整。不要把实验reset指向另一人的实例。环境变量需由启动 shell 显式设置，程序不会自动加载 `.env`。

## 从哪里开始读

0. [CP2 当前交付](docs/CP2_A_DELIVERY.md) 与 [CP2 接线](docs/handoff/CP2_A_TO_B.md)；[CP1 交接](docs/handoff/CP1_A_TO_B.md)作为历史记录。
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
git switch work/a-cp2
git pull --ff-only
```

沿各自目录连续开发，小而完整地提交，通过PR合入main。跨边界变更先发准确契约diff；共享契约由A合入、双方检查。两人各自写测试和说明，不把全部验收杂务交给B。

提交前运行`make cp0`；后续使用`make checkpoint CP=CP1`等入口，缺失/过期/未运行证据应失败。不能拿CP0的501占位测试替代业务测试：实现对应操作时更新责任表、CP0状态检查和业务验收，保持“实际实现什么就声明什么”。

CI会在push/PR运行CP0检查。代码审阅责任写在`.github/CODEOWNERS`；它与远端分支保护是否启用是两回事。CP1–CP5的高标准定义不会因一个构建成功自动达成。

## 来源与边界

- 当前有两种隔离模式：`logical_simulation` 是固定 10 秒模型；`cpu_reference` 运行真实固定整数参考程序。两者均不运行提交的 image/command/env，也不代表真实 GPU 执行。CPU 模式的目录、请求模式和证据见 [CP2 接线](docs/handoff/CP2_A_TO_B.md)。
- 团队暂定语义集中在`configs/semantics.json`。正式答疑到达时局部调整，不改官方原件。
- 内部契约与产品扩展不增加基础客户端必填字段。WorkIntent计时从意图到达开始，不从获配后创建job开始。
- 仓库用于竞赛协作，尚未添加开源许可证。第三方库保留其许可；AI协助生成及测试 CP0–CP2 代码和文档，记录见[CP0交付记录](docs/CP0_DELIVERY.md)、[CP1交付记录](docs/CP1_A_DELIVERY.md)与[CP2交付记录](docs/CP2_A_DELIVERY.md)。
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

# 织算 Campus Fabric

校园共享计算服务：申请、收回、接续与可追问的决定。

**当前：CP1，A + B 本地联调版。** 六工作区已接入持久后端，服务端工作流可完成登记—分配—模拟执行—结算。B0/B1/B2 策略已通过真实内核端口测试；默认演示仍使用 A 的 builtin 基线。CP2 的回收/恢复/故障与真实 CPU 执行尚未实现。先读 [B 第一版交接](docs/handoff/B_V1_DELIVERY.md) 和 [A 接线说明](docs/handoff/CP1_A_TO_B.md)。双人联合 CP1 尚未通过，不自动进入 CP2。

协作仓库：https://github.com/Woebegone-67/campus-gpu-fabric

仓库可见性以GitHub实时设置为准。当前私有访问需使用各自GitHub身份；xuyangma的write权限已生效。Codex桌面本地开发只需克隆后打开项目文件夹，不要求在GitHub应用安装列表选中仓库。切勿共享凭据。

| 负责人 | 主线 | 开发分支 | 先读 |
|---|---|---|---|
| A · Xiuzhu Du / `Woebegone-67` | 可信资源与执行 | `work/a-core` | [A开发任务书](docs/handoff/OWNER_A.md) |
| B · `xuyangma` | 服务决策与操作体验 | `feat/route-b` | [B开发任务书](docs/handoff/OWNER_B.md) |

## 启动与验收

需要Git、uv、Python 3.12、Node 22.12+（22.x）、npm和make。依赖固定在`uv.lock`与`web/package-lock.json`；无需 GPU 或 Kubernetes 即可运行 CP1 逻辑模拟。

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
# 终端1，仓库根目录
bash web/dev-local.sh api
# 终端2，仓库根目录
bash web/dev-local.sh web
```

访问`http://127.0.0.1:5174`；API为`http://127.0.0.1:8081`。首次启动在 `runtime/owner-b-v1/` 创建本地演示凭据。在页面右上角“当前身份”导入该目录的 `client-identities.json`，再选择 alice、bob、supplier 或 admin。凭据只保留在浏览器内存；刷新后需重新导入。开发host仅绑定本机，Web代理不包含`/_test`。

每人使用自己的clone、端口与`runtime/<owner>`。上面的 B 入口会组装查询与工作流；A 的原始 `make api` 仍在8080提供内核接口，不会自动推进 B 工作流。端口可通过 `PORT`、`WEB_PORT` 调整。不要把实验reset指向另一人的实例。环境变量需由启动 shell 显式设置，程序不会自动加载 `.env`。

## 从哪里开始读

0. [CP1 当前交付与接线](docs/handoff/CP1_A_TO_B.md)。
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
git switch work/a-core
git pull --ff-only
```

沿各自目录连续开发，小而完整地提交，通过PR合入main。跨边界变更先发准确契约diff；共享契约由A合入、双方检查。两人各自写测试和说明，不把全部验收杂务交给B。

提交前运行`make cp0`；后续使用`make checkpoint CP=CP1`等入口，缺失/过期/未运行证据应失败。不能拿CP0的501占位测试替代业务测试：实现对应操作时更新责任表、CP0状态检查和业务验收，保持“实际实现什么就声明什么”。

CI会在push/PR运行CP0检查。代码审阅责任写在`.github/CODEOWNERS`；它与远端分支保护是否启用是两回事。CP1–CP5的高标准定义不会因一个构建成功自动达成。

## 来源与边界

- 当前停在 CP1 本地联调。正常任务采用公开的固定 10 秒逻辑执行模型，不运行 image/command，不代表 CPU/GPU 实测或调度收益。
- 团队暂定语义集中在`configs/semantics.json`。正式答疑到达时局部调整，不改官方原件。
- 内部契约与产品扩展不增加基础客户端必填字段。WorkIntent计时从意图到达开始，不从获配后创建job开始。
- 仓库用于竞赛协作，尚未添加开源许可证。第三方库保留其许可；AI协助生成及测试 CP0/CP1 代码和文档，记录见[CP0交付记录](docs/CP0_DELIVERY.md)。
- 此仓库不含旧MCM原始数据、历史实验、论文或凭据。

## CP1 真实后端

合并后的技术验收入口为 `make cp1-joint`，它实际启动 A+B 集成进程，检查自动编排、并发、响应丢失、硬重启及部分成功。它不代替双人操作。当前验收结论见 [CP1 联合验收](docs/review/CP1_ACCEPTANCE_DECISION.md)，双人剩余操作见 [交接清单](docs/review/CP1_HUMAN_HANDOFF.md)。

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

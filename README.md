# 织算 Campus Fabric

校园临时共享算力：以贡献者收回期限为约束，按实际进度选择完成、保存或停止，让使用者沿同一项工作继续。

当前收尾分支：`release/final-20261115`。已合入新 UI，并完成最终一轮页面删减、工作流调整和实际浏览器验证。旧 B 交接任务已经结束，历史文档保留在 `docs/handoff/`，不再作为当前操作入口。

## 直接开始

- [上台前阅读手册](docs/release/MORNING_GUIDE.md)：产品原理、页面、账户和完整操作。
- [五分钟路演与问答](docs/release/ROADSHOW_5MIN.md)：自适应收回决策、强简单基线对照、真实接续与六项评分覆盖。
- [最终验收记录](docs/release/FINAL_ACCEPTANCE.md)：实际通过项、复现方法和冻结范围。
- [界面修改清单](docs/release/FINAL_UI_CHANGES.md)：本轮删除、调整和问题修复。
- [收回决策原理](docs/research/RECLAIM_AWARE_DESIGN.md)：四种动作、贡献者偏好、硬约束、观测与审计。
- [自适应策略对照](docs/research/RECLAIM_AWARE_RESULTS.md)：14 个模型条件、四个强基线、严格保存控制及不利结果。
- [本轮验证](docs/release/RECLAIM_ACCEPTANCE.md)：真实 CPU 决策、自动收回、浏览器倒计时与报告下载。
- [此前真实 CPU 损失核算](docs/research/FINAL_LOSS_ACCOUNTING.md)：原四条件三策略实验保留。
- 可打印：[阅读手册 PDF](docs/release/artifacts/织算_上台前阅读手册.pdf)、[路演与问答 PDF](docs/release/artifacts/织算_五分钟路演与问答.pdf)。

已有本机试用：CPU 实例 <http://127.0.0.1:15253>，逻辑模拟 <http://127.0.0.1:15252>。这两个地址只适用于已经启动本仓库的当前电脑。页面使用账户、密码登录，刷新后保持会话。当前本机初始密码保存在 `runtime/release-preview-morning-20261115/cpu/login-accounts.txt`，仅供本机查看，不纳入 Git。

## 从干净目录启动

需要 Git、uv、Python 3.12、Node 22.12+（22.x）、npm 和 make。无需真实 GPU；CPU 进程控制要求 macOS/Linux。端口被占用时选择另一组端口，不停止其他实例。

```bash
git clone --branch release/final-20261115 https://github.com/Woebegone-67/campus-gpu-fabric.git
cd campus-gpu-fabric
uv python install 3.12
make setup
uv run python scripts/cp2_joint_session.py start   --runtime runtime/my-demo   --logical-api-port 18262 --logical-web-port 15262   --cpu-api-port 18263 --cpu-web-port 15263
```

打开 CPU 页面 <http://127.0.0.1:15263>，使用 `runtime/my-demo/cpu/login-accounts.txt` 中的账户和初始密码登录。Alice（`alice`）、Bob（`bob`）、Chen（`chen`）都是普通用户，均可使用算力和共享自己的设备；本次演示设备由 Chen 持有。管理员账户是 `admin`。登录会话有效期为七天，刷新页面无需重新登录；“退出登录”会使当前会话失效。逻辑实例使用其 `logical/login-accounts.txt` 中的密码，与 CPU 实例分别登录。

演示不同用户时，使用独立的浏览器配置文件或不同浏览器。同一浏览器配置文件中的普通窗口共用登录状态，不能用它们模拟两个同时在线的账户。

CPU 模式执行固定参考程序并生成可核验结果；GPU 库存是模拟的。逻辑模式使用可推进的模拟时钟。两种模式的工作、资源、身份独立，均不执行任意用户镜像或 CUDA 训练。

停止自己刚创建的会话：

```bash
uv run python scripts/cp2_joint_session.py stop --runtime runtime/my-demo
```

## 验证与研究

```bash
make cp0
npm --prefix web run test:client
```

`make cp0` 检查契约、静态规则、Python 测试、UI 交互、构建及真实 HTTP 冒烟。此前 UI 收尾运行过一次完整后端检查；本次收回机制修改只运行相关回归、浏览器流程及必要构建检查。具体范围、退出结果和原始失败见各版本验收记录。

真实浏览器证据位于 `evidence/release-20261115/`。正式旧 CP3 报告仍是历史数据；本轮只验证其页面和原字节下载，没有重跑 125 组实验。原损失核算包含四条件、三策略的真实 CPU 对照；本轮新增生产决策器的参数化对照，模型结果与真实进程验证分开报告。原 P/B2=1.00、规模和延迟未达结果保留，不用新的指标覆盖旧负例。

评分以 [challenge.md](spec/official/challenge.md) 的 55/45 为准，路演为五分钟。原比赛候选标签保留不变；当前分支继续纳入账户登录、收回决策与页面修订。历史验收记录仍对应其中标注的版本；完整 CP4、独立真人部署、Safari 和物理触控板没有因本机测试而补记通过。默认收回方式为“尽快归还”；设备主人可明确选择“尽量保留计算”。相对强简单基线，前者在本轮模型中仅小幅获益，后者全条件平均更差，详见对照报告；两者都不构成真实 GPU 性能结论。

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

## 规范与历史

公共 API 原件在 `spec/official/api/openapi.yaml`；`spec/official/manifest.json` 保存来源摘要。核心约束见 [共享契约](docs/CONTRACTS.md)、[状态与占用](docs/STATE_MODEL.md)、[产品扩展](docs/EXTENSIONS.md)。

历次阶段交付和协作记录保留在 `docs/handoff/`。它们描述当时版本，不覆盖本页的最新启动方式、最终验收或五分钟路演。原架构性能困境见 [历史收尾](evidence/resource-locality/CLOSEOUT.md)，最终独立只读审查见 [复核报告](docs/release/FINAL_REVIEW.md)。

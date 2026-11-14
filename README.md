# 织算 Campus Fabric

校园共享计算服务：申请、收回、接续与可追问的决定。

**当前版本：CP0。** 已建立统一契约、工程边界、任务书和验收入口。开发host实际提供health/version；其余19个官方操作明确返回501，尚不是可参赛验收的Server。界面是六个工作区的开发骨架，没有预填任务或虚构运行数字。

公开协作仓库：https://github.com/Woebegone-67/campus-gpu-fabric

仓库可匿名读取与克隆。A和B提交代码时使用各自GitHub账号；xuyangma的write权限已生效，切勿共享凭据。

| 负责人 | 主线 | 开发分支 | 先读 |
|---|---|---|---|
| A · Xiuzhu Du / `Woebegone-67` | 可信资源与执行 | `work/a-core` | [A开发任务书](docs/handoff/OWNER_A.md) |
| B · `xuyangma` | 服务决策与操作体验 | `work/b-product` | [B开发任务书](docs/handoff/OWNER_B.md) |

## 启动与验收

需要Git、uv、Python 3.12、Node 22.12+（22.x）、npm和make。依赖固定在`uv.lock`与`web/package-lock.json`；无需GPU或Kubernetes即可执行CP0检查。

```bash
git clone https://github.com/Woebegone-67/campus-gpu-fabric.git
cd campus-gpu-fabric
uv python install 3.12
make setup
make cp0
make checkpoint CP=CP0
```

`make cp0`检查生成契约是否过时，运行Python静态检查与测试，构建Web，执行静态CP0验收，并启动真实HTTP子进程进行骨架冒烟。回执在`evidence/cp0/`，属于本地生成物。`make checkpoint CP=CP0`检查这些回执是否对应当前构建。

运行两个终端：

```bash
# 终端1，仓库根目录
make api
# 终端2，仓库根目录
make web
```

访问`http://127.0.0.1:5173`；API为`http://127.0.0.1:8080`。开发host仅绑定本机。Web代理不包含`/_test`，不得向浏览器下发测试密钥。CP0提供页面导航与health连接代码，其余产品操作留待后续实现；实际核验范围见交付记录。

每人使用自己的clone、端口与`runtime/<owner>`。A使用默认端口；B可运行`make api PORT=8081`和`make web PORT=8081 WEB_PORT=5174`，代理会一起切换。不要把实验reset指向另一人的实例。`.env.example`说明预留运行参数；CP0只使用Makefile的HOST/PORT/WEB_PORT，持久实例配置由A在CP1接入。

## 从哪里开始读

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

- 当前任务是CP0开工与协作准备，没有调度收益或真实GPU成绩。
- 团队暂定语义集中在`configs/semantics.json`。正式答疑到达时局部调整，不改官方原件。
- 内部契约与产品扩展不增加基础客户端必填字段。WorkIntent计时从意图到达开始，不从获配后创建job开始。
- 仓库已公开用于竞赛协作，尚未添加开源许可证。第三方库保留其许可；AI协助生成CP0代码/文档，记录见[CP0交付记录](docs/CP0_DELIVERY.md)。
- 此仓库不含旧MCM原始数据、历史实验、论文或凭据。

# CP4 B 线非 UI 验收入口

本轮从 A 修复提交 `e2a768fa3efd94ad5d8e26c69917fc46d585bcbf` 开始。用户已要求继续 CP4，暂缓产品 UI 改版、六条页面旅程和演示视频。本目录提供自动验收工具；不修改 A 权威账本，不签署另一位成员的人工验收，不把 CP3 历史缺口改写为通过。

## 运行顺序与版本

在独立 worktree 安装锁定依赖：`uv sync --locked`。所有入口使用新输出目录，失败记录也要保存。先完成源码修改与回归并提交，再运行证据任务；当前构建同时包含提交号、源码 SHA 和文件数，修改源码或切换提交会使旧回执不再对应当前构建。`evidence/cp4/` 已忽略，由验收交付包保存，勿提交身份文件、数据库或访问令牌。

```sh
# 基线回归仍包含既有前端测试；不代表执行 CP4 人工 UI 验收。
npm --prefix web ci
make cp0

# 真实 TCP 挑战，各 case 使用独立实例。
uv run python -m experiments.cp4_reliability --output evidence/cp4/candidate/reliability

# 从已提交 HEAD 新克隆、新建 venv，再安装依赖与运行正常/超时旅程。
# 把 /absolute/path/to/uv 替换为本机 uv 的绝对路径。
uv run python -m experiments.cp4_clean_deploy --uv /absolute/path/to/uv \
  --output evidence/cp4/candidate/clean-deploy

# 单独运行，避免与测试/实验抢占本机资源；到时停止接收新工作并回收在途任务。
uv run python -m experiments.cp4_load --profile full --max-wall-seconds 240 \
  --request-timeout 15 --output evidence/cp4/candidate/load
```

负载总目标是 128 节点、1024 物理 GPU、64 Owner、10000 jobs、50000 普通变更命令及 32 并发。超时或部分完成不算规模通过。当前负载覆盖创建租约/创建任务/取消/续租/释放及任务、租约详情查询；并未覆盖所有混合操作、查询类型与并发前缀。细节见 [CP4_LOAD.md](CP4_LOAD.md)。普通命令/查询延迟与启动、校验、清理分开，必须同时报告 p50/p95/p99、实际吞吐与错误。

参考延迟目标为受控 4 vCPU / 8 GiB 环境下普通查询及命令 p95 < 200 ms。本机诊断不能替代该目标。即使本机延迟很高，仍应保留原始负面结果并注明环境；不得把机器规格更高解释为已经通过参考验收。

## 来源核验

[CP4_PROVENANCE.md](CP4_PROVENANCE.md) 定义从封存输入、策略/模型/运行、原始工件、重算结果到登记输出的来源链。先用 `experiments.cp3_suite` 在当前构建封存并运行指定分区；开发分区与正式验证分区的证据必须分开。

```sh
uv run python -m experiments.cp3_suite freeze --partition development \
  --output evidence/cp4/candidate/development-suite
uv run python -m experiments.cp3_suite run \
  --suite evidence/cp4/candidate/development-suite --workers 2
uv run python -m experiments.cp3_suite analyze \
  --suite evidence/cp4/candidate/development-suite
# figure-index.json 必须来自实际输出，不能填写示例占位符。
uv run python -m experiments.cp4_provenance \
  --suite evidence/cp4/candidate/development-suite \
  --figure-index evidence/cp4/candidate/figure-index.json \
  --output evidence/cp4/candidate/provenance.json
```

开发运行与登记结果的来源通过，不等于 125 项正式验证、算法收益门槛、所有图表登记、全状态重放或 CP4 整体通过。缺少来源包、运行、图表索引应保留 `NOT_RUN`。

## 交付与判定

每次交付应包含：当前构建身份、依赖/部署文件清单、执行命令与退出码、脱敏 HTTP 历史、逐请求延迟、实际完成规模、资源检查记录、证据 SHA，以及按七项 CP4 要求列出的状态。清洁部署工具说明见 [CP4_CLEAN_DEPLOY.md](CP4_CLEAN_DEPLOY.md)。

自动清洁部署预演只证明本机新目录可执行后端旅程；另一位成员照 README 部署仍需本人完成。可靠性挑战只证明实际覆盖的边界；注册的完整 CP4 验收 gate 仍应由正式 validator 和联合证据支持。本轮不伪造 `tests/acceptance/cp4/` 的结果，也不把这些诊断回执放到正式 gate 文件中替代完整验收。

A 侧若需修改核心性能、部署环境或正式 gate，应在新提交上重跑对应证据。旧提交的失败与部分完成记录保留，可用于比较，不能复用为新提交的通过结果。

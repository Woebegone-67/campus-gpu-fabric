# B 第一版接线与交付

日期：2026-11-14。责任：B / xuyangma；接收：A / Woebegone-67。
分支：`feat/route-b`，已快进合入 A `work/a-core` 的 `e134f94`。
共同契约、官方 OpenAPI、后端依赖锁未改。评分依据仍为 challenge 的 55 + 45。

## 已交付

- 六工作区：真实工作提交、资源/贡献查询、工作详情/阶段回执/结算、决策解释、事件与既有实验查询、系统能力/验收目标。
- B 查询替换 A 的 GET bridge，保留所有写接口。可信全局管理员可以读取全局；普通 Owner 隔离。版本分页、epoch 冲突、错误/空态分离。
- B 工作流通过 A 的 WorkflowPort 推进稳定 `allocate-v1` / `job-v1` 阶段，等待真实获配后启动；重启扫描持久意图，不建第二账本。传入发现时 epoch；失败避免每轮反复提交，未知回执按同阶段有限退避。
- B0 FIFO first-fit、B1 当前占用公平准入/best-fit、B2 兼容性稀缺放置。都是当前快照纯函数，不读未来到达、隐藏工期或场景 seed。候选范围为有限单节点搜索，不宣称全池最优/不可行，也未测得性能提升。
- 独立离线结果计算器 `python -m experiments.compare_runs baseline.json candidate.json --output comparison.json`。同输入/窗口配对，保留未完成/失败/取消，报告个体延迟伤害和 Owner 有效服务下降；它不产生模拟运行或实际实验凭据。

## 可直接运行

安装依赖：Python 3.12、Node 22.x、uv，然后在仓库根目录 `make setup`。
两个终端分别运行：

```bash
bash web/dev-local.sh api
bash web/dev-local.sh web
```

工作台 `http://127.0.0.1:5174/`，API `http://127.0.0.1:8081/`。
默认实例 `owner-b-v1`，账本 `runtime/owner-b-v1/fabric.sqlite3`。
启动脚本只在首次创建 `auth.json` 和 `client-identities.json`，不会覆盖已有身份。
在右上角导入后一个文件并选 alice；该文件仅本地读取，不上传原件、不进浏览器持久存储、不入 Git。

正常旅程：alice 提交镜像和命令声明 → 查询工作看到运行及绑定 → 切换 admin → 系统验收推进模拟时间10秒 → alice 查工作和租约终态。使用 keep_lease 时任务完成后租约仍保留；release_on_success 则应为 Released。镜像/命令不会被实际执行。

本机启动脚本优先识别忽略目录 `runtime/b-tools/` 的 Node 22/uv；其他机器使用自己的 PATH。它不在运行时下载依赖。

## 与 A 接入

B 入口为 `campus_fabric.workflows.devhost:create_app`，调用 A 的 app factory 后装配 `make_kernel_services`、替换 GET bridge 并启动工作流 worker。A 的 `app.py` 没有被改动；将来可把此装配迁入 A 的正式启动入口。

默认 builtin 为 A 原有基线。使用 B 策略必须新建隔离实例，不能修改已有数据库的调度模式：

```bash
FABRIC_INSTANCE=owner-b-external FABRIC_SCHEDULING_MODE=external FABRIC_B_POLICY=B2 PORT=8082 bash web/dev-local.sh api
PORT=8082 WEB_PORT=5175 bash web/dev-local.sh web
```

external worker 提交实际选中计划，每轮最多32个；无选中候选时不写空决策。模型与时钟仍由 A 控制，后台不会自动推进逻辑时间。

## 本地验证记录

以下是当前实现验证，不是比赛评分或双人联合验收。

| 验证 | 实际结果 | 记录 |
|---|---|---|
| `make cp0` | Python 263项通过，静态检查/契约生成检查/Web构建通过，真实进程冒烟5项通过 | `evidence/local/b-v1/cp0-final.log` |
| `make cp1-a` | A 的真实HTTP检查10项通过；含32并发、幂等、权限、租约、硬退出重启、部分成功和结算 | `evidence/cp1/a_core_receipt.json`、`evidence/local/b-v1/cp1-a.log` |
| `npm --prefix web run test:client` | 9项通过 | `evidence/local/b-v1/client-tests.log` |
| 本地浏览器 1440×900 | alice提交→Running→admin推进10秒→completed/Released；Bob访问Alice工作404；资源恢复3/3，事件与管理员读取可见 | `evidence/local/b-v1/completed-work.jpg`、`http-observation.json` |
| API重启后读取 | 同一intent/allocation/job与终态仍在，未重复创建 | `evidence/local/b-v1/http-observation.json` |
| 双人交叉操作/联合 CP1 | NOT_RUN，不能由单一代理切换两个角色替代 | `make checkpoint CP=CP1` 应继续拒绝缺失的联合证据 |

Python 有一条上游 TestClient/httpx 弃用提示；未为此改变 A 的依赖锁。
首轮完整测试曾在收集阶段暴露计算器导入路径问题，已按脚本路径加载修复并全量复跑。原始失败输出仅留在会话；`evidence/local/b-v1/initial-import-failure.txt` 是据工具输出重建的记录，不冒充原始日志。最终验证以 `cp0-final.log` 为准。

本地 evidence 与凭据均由 .gitignore 排除，测试可在对方机器独立复跑。源代码、脚本和这份交接进入 Git；本次没有线上部署、比赛上传或真实 GPU 验证。

## 仍待共同处理（最多三项）

1. A/B 两位操作者交叉完成 CP1 检查，按现有门槛登记真实联合证据；当前系统验收仍显示 NOT_RUN。
2. A 在 CP2 提供回收、保存、核验、接续和真实 CPU 执行；B 再启用对应操作。当前不伪造工件或恢复成功。
3. 后续正式会话/实验/工件写入契约由 A 冻结。当前本地身份名称仅提示界面，不授予权限；实验页只读已登记 run，正式离线实验与消融尚未跑。

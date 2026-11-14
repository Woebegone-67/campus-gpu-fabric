# CP2 A → B：接线与联合验收

本交接对应 `work/a-cp2`。A 已实现回收、保存/恢复、节点状态、故障控制与真实 CPU reference 接续；**B 六工作台与三角色 CP2 联合验收仍为 NOT_RUN**。保留自己的分支和改动，先合入双方约定的 A 提交，再接线；不覆盖 B 目录。

## 1. 启动独立实例

从仓库根目录操作。依赖为 `uv`、Python 3.12、Node 22.12+（22.x）、npm；首次使用运行 `make setup`。以下端口用于新实例，不使用 CP1 的 18081 / 15174。

终端一，逻辑模拟后端：

```sh
FABRIC_INSTANCE=cp2-a-logical \
FABRIC_DATA_DIR="$PWD/runtime/cp2-a-logical" \
FABRIC_AUTH_FILE="$PWD/runtime/cp2-a-logical/auth.json" \
FABRIC_INVENTORY_FILE="$PWD/configs/cp1_inventory.json" \
FABRIC_MODE=logical_simulation FABRIC_SCHEDULING_MODE=builtin \
PORT=18082 bash web/dev-local.sh api
```

终端二，Web：

```sh
FABRIC_API_TARGET=http://127.0.0.1:18082 \
WEB_PORT=15175 bash web/dev-local.sh web
```

打开 `http://127.0.0.1:15175`。首次启动自动调用 `scripts/init_cp1_demo.py`，在上述目录产生 `auth.json` 和 `client-identities.json`；在 UI 导入后者，再选 alice、bob、supplier 或 admin。文件名沿用 CP1，但身份方式适用于 CP2。凭据只在本机保存，不提交仓库、不放入截图。前端代理不包含 `/_test`。

需要手动预建身份时，使用：

```sh
uv run python scripts/init_cp1_demo.py runtime/cp2-a-logical
```

该脚本只允许新目录中的新身份文件；已有文件时拒绝覆盖。不要在自动启动已生成文件后再重复执行。

启动真实 CPU 模式时，先停止上述两个新实例的服务（不是 CP1），将终端一的实例、数据目录和 auth 路径统一替换为 `cp2-a-cpu`，并设置 `FABRIC_MODE=cpu_reference`，其余命令不变。两种模式必须使用不同数据库。CPU 模式的 WorkIntent 注册必须使用 `execution_mode: "cpu_reference"`；当前旧 Web 可能仍写死逻辑模式，接线前用后端验收脚本验证，不能把界面 409 当作 CPU 能力缺失。

```sh
curl -fsS http://127.0.0.1:18082/healthz
curl -fsS http://127.0.0.1:18082/v1/version
```

版本响应应显示所选模式、`checkpoint: "CP2"`，并保留 `checkpoint_passed: false`。库存仅首次建库时载入，reset 后不自动补回。CPU 模式启动真实固定参考进程；GPU 库存仍是模拟配置，提交的镜像/命令不会运行。

## 2. 准确接口

原有工作登记、分配/任务原子阶段、查询和结算继续使用。身份必须来自服务端 bearer 映射；仅改 Owner 文本不能获得权限。

| 方法与路径 | 请求 | 返回与用途 |
|---|---|---|
| POST `/v1/allocations/{id}/checkpoint` | `{"status":"ready"}` 或 `{"status":"failed"}`；可选字符串 `artifact_uri` | 200，租约响应；只在有效保存窗口接受，迟到/不适用返回 409 |
| POST `/v1/allocations/{id}/resume` | 无 body 或 `{}` | 200 已获配/运行；202 等待资源；拒绝过期、预算耗尽或严格 CPU 模式未核验工件 |
| POST `/x/v1/contributions` | 完整 `NodeInventory` | 201 `CommitReceipt`；contributor 只能登记自己 owner 的节点；`observed_at` 等于当前逻辑时间 |
| POST `/x/v1/contributions/recall` | `{"gpu_ids":["sim-a10-gpu0"],"notice_seconds":5,"duration_seconds":20}` | 201 `CommitReceipt`；contributor/admin，批量所有权与设备检查失败则整项拒绝 |
| POST `/x/v1/work-intents/{id}/actions` | `{"action":"resume","expected_version":{"epoch":0,"revision":12}}` | `CommitReceipt`；作用于同一工作的已绑定租约，不创建第二项工作 |
| GET `/x/v1/artifacts?uri=…` | URL 编码的已登记 `fabric-cpu://…` URI | 当前 owner、当前 epoch 的已核验工件；不支持任意文件路径/URL，也不开放跨 owner 读取 |

WorkIntent actions 实际支持 `allocate / create_job / release / cancel / resume`；前两项需要稳定 `stage_id`。尚无独立 `retry_stage` 或 `settle` action：重试原阶段使用原动作和 stage，结算遵从登记的 settlement 或 release。

CPU 时钟会持续改变 revision，工作动作遇 `stale_version` 应刷新对象并有限重试；未知响应重放原阶段。等待状态下重复 resume 不新增代次、不扣预算。对终态取消/释放仍应显示实际服务器结果。

## 3. 必须区分的状态与事实

新字段已进入 `contracts/generated/contracts.schema.json` 与 `web/src/generated/contracts.ts`：

- `AllocationView.preemption`：通知时刻、deadline、checkpoint_due；`restart_count` 是替代执行预算；`pending_terminal` 表示已请求 Released/Expired/Evicted，但仍待真实停止。
- `AllocationView.checkpoint` 与 `JobView.checkpoint`：分别显示 `report_status`、`verification_status`、`continuation_status`。公开 ready 只完成第一层；真实参考工件核验才 verified；新进程实际接续才 applied。
- `JobView.execution_mode / retry_count / result_uri / failure / finished_at`：执行失败重试与回收后替代计数分别展示。`job_id` 保持不变，`AttemptRef.generation` 增加。
- `PreemptRequested` 时可能仍占用资源；CPU cancel/release 返回已受理时，不能立即显示“已停机/已归还”。等待服务器确认终态或 `Preempted`。
- CPU reset 后公开列表为空，但私有旧进程隔离可能仍占用物理供给；不能通过重新登记别名释放隔离。

本轮在 B 生产目录仅修改 `workflows/devhost.py` 的 lifespan 组合，防止工作流 host 覆盖 CPU driver；其策略与编排规则保持原实现。生成 TS 是共享契约改动。B 应检查已有查询映射是否保留上述字段；A 的原始 HTTP bridge 不是 B 界面展示已完成的证明。

## 4. 测试操作与产品操作分离

`/_test/*` 只给服务端测试操作者。普通用户、贡献者与管理员 bearer 不替代测试密钥；测试密钥不进入 Web。

| 操作 | 请求要点 |
|---|---|
| `POST /_test/contributor-demand` | 与上方 recall 相同字段，供隔离实验控制，不是贡献者产品路径 |
| `POST /_test/node-state` | `{"node_id":"sim-a10","state":"Draining"}`；亦支持 Ready/NotReady |
| `POST /_test/fault` | `{"operation":"POST /v1/allocations","mode":"503","count":1}` |
| latency | `mode:"latency"`，`delay_ms` 为 0–30000；`count` 为 0–20；0 禁用 |

fault 的 operation 必须是官方 public API 的 `METHOD + 空格 + 原模板`；例如 `POST /v1/allocations/{allocation_id}/resume`，不是具体 ID。429/500/503 可接受数字或字符串并规范化。无权限请求不消耗受保护操作的故障预算。latency 自身不推进逻辑时间；CPU 实例独立的真实时钟仍正常前进。

Draining 停止新放置，保留当前任务；NotReady 请求终止受影响租约，CPU 要等停止确认；Ready 不复活旧任务，也不解除仍有效的贡献者收回窗口。

## 5. B 的连续主线和交汇点

1. **接线主线**：六工作台共用一致身份与执行模式，接上贡献者 recall、resume、保存三层事实、等待停止、两预算和真实工件。不要在用户路径调用测试接口。
2. **查询核对**：同一工作、租约和 attempt 的状态相互一致；新字段不被旧映射丢弃；Owner B 查询 A 的工作和工件返回 404；事件、指标不泄露其他 owner。
3. **体验核对**：503 显示可重试故障而不是“能力未实现”；版本冲突与未知提交结果分别处理；当前仍为模型执行时明确呈现。CP1 导航身份与文件导入反馈问题保留 CP4 跟踪，若影响权限判断则立即修复。
4. **交汇验收**：A/B 使用同一提交，在独立实例中由两个实际操作者完成下表；每项保留去敏感的工作/租约/attempt ID、请求 ID、截图或事件导出，失败保留最短复现。

| 场景 | CP2 需要看到的结果 | 本次状态 |
|---|---|---|
| researcher 提交 → contributor 收回 → researcher 接续 | 同一工作和 job，新 attempt；期限不重置；占用只归还一次 | NOT_RUN（双人 UI） |
| ready / failed / 超时 / 连续收回 | 展示各自实际结果；失败不得标 verified；严格模式失败需终止 | NOT_RUN（双人 UI） |
| 无替代资源、预算耗尽、租约已过期 | 可解释等待或拒绝；不会无限新建工作或消费预算 | NOT_RUN（双人 UI） |
| contributor 只收回自己的资源 | 自有成功，外部 owner 拒绝；不携带测试密钥 | NOT_RUN（双人 UI） |
| CPU 保存 → 停止 → 接续 → 结果 | 工件三层事实、来源和完整结果；不能由逻辑 clock 伪造成功 | NOT_RUN（双人 UI） |
| 刷新、重新认证、切换三角色 | 同一记录仍可追溯；查询隔离；决策记录能展开 | NOT_RUN（双人 UI） |

## 6. A 的独立复验入口

```sh
make cp2-a
# 按需定向检查；完整入口已包含下面的测试及脚本
uv run pytest tests/domain/test_http_cp2.py tests/domain/test_cp2_adversarial.py tests/concurrency/test_cp2_history.py tests/executors -q
uv run python tests/acceptance/cp2/cp2_all_operations.py
uv run python tests/acceptance/cp2/cp2_cpu_reference.py
```

前两份进程回执默认分别写入 `evidence/cp2/http_operations_receipt.json` 与 `cpu_tcp_receipt.json`。单项收据必须对应当前构建；源码改变后重跑。`make cp2-a` 生成 `evidence/cp2/a_core_receipt.json` 和部分联合 `receipt.json`；后者保持 `NOT_MET`，`CP2-SIX-WORKSPACES` 为 `NOT_RUN`。A 的成功不自动放行联合门禁。**本交接停在 A 后端可接线、联合 CP2 待验收；不进入 CP3。**

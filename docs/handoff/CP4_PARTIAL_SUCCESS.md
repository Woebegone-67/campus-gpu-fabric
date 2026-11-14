# CP4 部分成功：贡献者收回后的真实任务创建拒绝

本页是可复现的操作步骤，不是通过记录。先按 [CP4 真人验收指南](CP4_HUMAN_ACCEPTANCE.md) 核对最终冻结候选的构建标识。每位操作者都须亲自执行并记录页面；旧“只推进1秒制造 stale_version”步骤会被前端自动重试，不能用作浏览器错误态验收。

此用例在独立逻辑实例中执行：先由 alice 成功取得一个租约，随后 supplier 对其物理 GPU 发送收回通知；alice 尚未刷新的页面点击任务创建。前端可以正常自动刷新版本，但租约已经处于 PreemptRequested，因此最终仍会明确拒绝创建任务。真实错误码是 lease_unavailable。无需修改源码、关闭客户端重试、制造假响应或修改数据库。

## 启动独立实例

在最终候选项目根目录使用新的目录。不要复用普通 15186/15187 候选实例，也不要清空已有运行数据。以下以 B 为例；A 换成自己的目录和空闲端口。

```sh
uv run python scripts/init_cp1_demo.py runtime/cp4-partial-recall-b
```

终端一：

```sh
env -u FABRIC_TEST_KEY \
  -u FABRIC_CP3_ARCHIVE_SUITE -u FABRIC_CP3_ARCHIVE_MANIFEST_SHA256 -u FABRIC_CP3_ARCHIVE_INDEX_SHA256 \
  FABRIC_INSTANCE=cp4-partial-recall-b \
  FABRIC_DATA_DIR="$PWD/runtime/cp4-partial-recall-b" \
  FABRIC_AUTH_FILE="$PWD/runtime/cp4-partial-recall-b/auth.json" \
  FABRIC_INVENTORY_FILE="$PWD/configs/cp1_inventory.json" \
  FABRIC_MODE=logical_simulation FABRIC_SCHEDULING_MODE=builtin \
  uv run uvicorn accept_cp1_joint:manual_app --factory --app-dir scripts \
  --host 127.0.0.1 --port 18096
```

终端二：

```sh
FABRIC_API_TARGET=http://127.0.0.1:18096 \
  npm --prefix web run dev -- --port 15188 --strictPort
```

这使用仓库已有 manual_app，只关闭自动工作流，便于逐阶段操作；没有测试密钥。页面为 http://127.0.0.1:15188，身份文件为新项目的 runtime/cp4-partial-recall-b/client-identities.json。端口若已占用则保留错误，不结束其他人的程序。本例不要求实例此前逻辑时间为0；从资源申请成功到释放结束，**本用例不要推进时钟**。

## 浏览器操作顺序

1. 打开两个 15188 标签页，分别导入此隔离实例身份文件。第一个选 alice，第二个选 supplier，核对右上角身份。不要用 admin 代替 supplier。
2. alice 在运行工作台新建一项逻辑工作：A10 × 1、租约300秒、尽力运行、保留租约手动归还。记录工作ID，然后到任务连续性查询该ID。
3. alice 点击“重试资源申请”，等待成功回执和查询刷新。必须只有一个租约、租约 Allocated、尚无任务。保存此成功请求/回执和详情GET、页面截图，记录原租约ID。
4. 确认原租约实际占用的 GPU。可查看已保存工作GET响应里的 allocations[].placement.slices[].device_ids；全新配置第一项A10工作通常是 sim-a10-gpu0，以实际响应为准。**从此刻起不要刷新、重新查询或重载 alice 标签页**，让“重试任务创建”保持可点击。
5. supplier 标签页进入“资源与贡献”，在“收回自有资源”勾选步骤4的物理GPU，收回通知期30秒、收回占用时长60秒，点击“发送收回通知”。必须收到真实成功回执。不要推进逻辑时钟；资源会立即进入收回通知状态。
6. 返回未刷新的 alice 标签页，点击“重试任务创建”。前端会正常处理首次 stale_version，自动读取新版本并再尝试；最终必须显示 **HTTP409**，错误内容为 **Job requires a live allocated lease with no active job**（原响应 error=lease_unavailable）。它不会继续自动重试这个业务拒绝。保存最终错误态截图及原请求链；不要把首次瞬间出现的 stale_version 当成验收结果。
7. 点击 alice 上方“刷新”，保存新详情GET和截图：仍是同一工作、只有原租约；租约 PreemptRequested、仍有原 placement；该工作的 job_ids 和 jobs 仍为空。这里证明已成功的分配没有被重复申请，也没有在被收回的租约上偷偷创建任务。
8. 本例不尝试恢复尚未创建的任务。点击“释放租约”并确认，再刷新：原租约应为 Released、placement为空，未新增租约或任务。由于贡献者通知窗口仍有效，GPU可能继续显示 unavailable；这不等于该工作仍持有它。
9. 两个终端分别 Ctrl-C 停止本例实例，保留记录和目录。普通候选实例不受影响。

若步骤6直接成功，或没有 lease_unavailable，保留真实结果并停止此用例，不能改写回执。核对是否选错GPU、误刷新alice或实际使用了其他实例。若在步骤5后先刷新alice导致按钮不可用，也记录该状态；使用另一项全新工作重做，不能伪造一次拒绝。

## 回传原件

将本例放在本人 submit-and-follow 的 partial-success 历史中，按实际顺序保留：工作创建、allocate成功、分配后GET、supplier收回POST、create_job的首次拒绝/自动GET/最终拒绝、刷新后的GET、释放和最终GET。请求保存 method/path/status_code/request_body/response；尤其保留 action、stage_id、expected_version，响应 objects/error/receipt。不要保存 Authorization、Bearer token或身份文件。

记录 retained_allocation_id、duplicate_allocation_created=false；所有详情GET的 allocation_ids 都只有原租约。至少保存分配成功、最终业务拒绝、收回通知状态、已释放四个原生截图。W2超时不能替代本例。

证据中的代理 `frontend_client_recall.json` 仅检查真实前端客户端与服务通信，明确 `human_ui_observed=false`，不能代替 A/B 本人的浏览器画面与确认。公开 `/v1/jobs` 外部占用方案因工作绑定租约主动拒绝而不支持，不能照此方案造占用任务。

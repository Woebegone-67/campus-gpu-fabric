# CP4：真人交叉验收与交付证据

本页是待执行清单，不是通过记录。只在最终候选提交冻结后操作；修订源码、测试或文档都会改变构建摘要。原 CP3 人验、自动角色切换、代理的干净克隆均不能替代本轮两位真人。

执行顺序为：换人干净部署 → 两人各自六项任务 → 同构建实录 → 回传原件。若规模或延迟仍未达标，可以先做功能预验并记录问题，不能据此签署 CP4 最终通过。后续结构性修复改变候选后，必须针对新候选重新验收，旧观察和签署不能平移。

## 冻结与身份

记录 `uv run python scripts/check_checkpoint.py --build-id` 的完整输出。A 与 B 分别以自己的姓名或可识别账号签署自己的观察，不让代理填 `attested: true`。一人两账号仍只算一位操作者。

所有材料放在 `evidence/cp4/`。每个引用写仓库相对路径与文件 SHA-256，不能指向旧 CP3 目录、符号链接或文件自身。凭据不加入证据。截图来自真实 1440×900 CSS 浏览器视口，同时记录 `device_pixel_ratio`，保留原 PNG；例如1倍截图为1440×900、Retina 2倍为2880×1800。不能把其他视口截图缩放成符合要求的图片。

## 不同操作者的干净部署

由不是本轮候选准备者的真人，在新的目录克隆候选提交。按当前 README 完成依赖安装、契约检查、前端构建与两个服务启动。使用自己的目录和端口，不重置已有实例。

保留初始空工作区状态、实际提交/源码摘要、当前 README 摘要、每条命令及完整输出、服务健康/版本 HTTP 响应、未登录写入 401、浏览器首屏截图。记录真实机器、实际路径和操作者。登记到 `evidence/cp4/clean_deploy_observer.json`，使用 `cp4-clean-deploy-v1`；字段与检查代码见 `tests/acceptance/cp4/_delivery_review.py:clean_deploy`。

`uv run python tests/acceptance/cp4/cp4_clean_deploy.py --agent-qa` 可预先自动检查真实干净克隆、依赖和启动。它写入独立 `agent-clean-deploy/<时间>/agent_qa.json`，其中 `formal_human_gate_satisfied=false`；这一记录不能改名代替真人观察。

## 两人分别交叉完成六项任务

每位操作者都执行以下六项。每项保留实际按钮/输入顺序、响应、观察结果及原截图；不要仅写 PASS。使用服务数据完成操作，不把组件测试或手写 JSON 当业务执行。

| 任务 ID | 操作与必须看见的结果 |
|---|---|
| `submit-and-follow` | 从运行工作台登记新工作，进入任务连续性查询同一工作。额外完成空列表和[部分成功场景](CP4_PARTIAL_SUCCESS.md)：资源申请成功后，由 supplier 通知收回实际占用 GPU；正常客户端刷新重试后，任务创建仍被 `lease_unavailable` 拒绝。同一租约保留、工作没有任务，最后释放原租约。保留真实成功与最终拒绝响应、原租约 ID。 |
| `contributor-recall` | supplier 本人在“资源与贡献”通过按钮收回自己的 GPU，核对通知和连续性状态。再以未获授权身份 Bob，使用自己的凭据向同一收回端点、同一已知属于 supplier 的 GPU 发出显式 HTTP 请求，必须收到 403；同时观察 Bob 页面没有可选择的他人卡。合法收回须实际 UI 操作，越权负例按下方步骤执行。 |
| `checkpoint-and-recover` | 在 CPU 参考实例运行保存—停止—恢复。查询响应必须分别显示工件 `verification_status=verified`、`continuation_status=applied` 和新 `resumed_attempt_id`；逻辑 ready 不满足此项。 |
| `explain-decision` | 从工作关联导航打开实际决定，核对当时合法候选、选中动作与已提交事件；`commit_status=committed`，不是仅显示计划。 |
| `events-report-and-download` | 读取工作关联事件和冻结的策略报告；保留原执行构建，R=1.00 与覆盖缺口不得改写。点击浏览器下载，保存实际下载文件并逐字节对照服务端工件。 |
| `administrator-acceptance` | 读取验收状态，再连续刷新管理员总览十次。每次真实响应均为 200；出现任何 409 或间歇错误应保留，不删失败后补十次成功。 |

每人都要覆盖 `error`、`empty`、`partial-success` 三种条件，可分布在六项任务中。每个任务的原始历史使用 `cp4-ui-history-v1`：`build`、`task_id`、`human_id`、从1递增的 `actions`（含 `operation`、`observed`）、实际 `requests`（`method`、`path`、`status_code`、`request_body`、`response`）。请求体保留实际 `action`、`stage_id`、`expected_version`，不保存身份文件和 Authorization。下载还需 `downloads`，逐项引用 `file` 与原 `server_artifact`；部分成功还需 `retained_allocation_id` 与 `duplicate_allocation_created=false`，以及按实际先后顺序记录的同工作 allocate 成功、分配后详情、create_job 拒绝和拒绝后同租约详情。无关接口的失败不能代替该任务阶段拒绝。若服务实际产生异步阶段失败，可通过 HTTP 200 详情中的已提交资源阶段和带 rejected 回执的任务失败阶段证明；不能为凑记录伪造此类阶段。

空态须保留对应任务的实际 GET 列表响应（如工作 `items=[]`、事件 `events=[]`），详情中暂时没有任务不能冒充列表空态。错误态须来自该任务的实际接口和真实错误体。登记与跟踪绑定同一工作；恢复保留 resume 操作前后同一 CPU 工作详情和递增的执行代次；关联决定保留 URL 中同一决定的 GET 和其已提交事件；管理员记录保留 GET 响应中的真实计数。

收回历史的每个请求另记安全身份描述 `actor`（`actor_id`、`owner_id`、`role`）和来源 `origin`：浏览器实际请求为 `browser`，本人显式 HTTP 为 `explicit_http`，代理检查为 `agent_http`。身份值来自本地初始化身份与页面选择，不包含 token；它们帮助核对一致性，不能替代本人观察。例如 supplier 的描述为 `{"actor_id":"supplier","owner_id":"supplier","role":"contributor"}`，Bob 为 `{"actor_id":"bob","owner_id":"bob","role":"researcher"}`。还须保留 supplier 的 GET `/x/v1/contributions`，其中节点所有者与物理 GPU ID 确认归属，以及 Bob 的 GET `/x/v1/work-intents`，确认其有效身份返回 `scope=owner, owner_id=bob`。

下载历史需同时保留该次报告 GET（含登记的 `artifact_ids`）、下载 GET 和原响应文字 `raw_response`。`downloads[].request_index` 是对应下载请求在 `requests` 数组中的 **0 起算索引**；例如下载是第3条请求则填 `2`。`file` 指实际浏览器下载原件，`server_artifact` 指另一个路径保存的服务端原件，两者都写 `path` 与 `sha256`。保留 HTTP 原响应字节，不从 JSON 预览复制、格式化或重新序列化；空白变化和大整数舍入都会破坏逐字节核对。以下只是字段示例，必须替换为实际路径和摘要，不能直接当证据：

```json
{"request_index":2,"file":{"path":"evidence/cp4/B/download-original.json","sha256":"实际摘要"},"server_artifact":{"path":"evidence/cp4/B/http-artifact-original.json","sha256":"实际摘要"}}
```

部分成功必须执行正式步骤中的隔离实例和供应者路线。仅推进时钟制造 `stale_version` 会被前端正常自动重试，不保证显示错误，不能作为该浏览器场景的通过证据。也不能通过公开 `/v1/jobs` 向工作绑定租约塞入占用任务：服务会正确拒绝这一绕过。代理的纯 TCP 或前端客户端复验均不代替本人实际看见页面最终错误、刷新后状态和释放结果。

联合记录 `evidence/cp4/user_tasks_observer.json` 使用 `cp4-user-tasks-v1`。根含 `build`、带时区 `observed_at`、空的 `unresolved_issues`、两位 `operators`。每人有不同 `human_id`、`observer_kind=human`、角色 `A` 或 `B`、本人确认的 `attested` 和六个 `tasks`。每项写 `id`、`status`、具体 `actual`、`viewport`、`workspaces`、`conditions`、`screenshot` 与 `history` 引用。缺项应留待完成，不能补造观察。

### Bob 对同一已知资源的越权负例

原验收条款要求“借用者尝试收回他人资源”和“UI/HTTP 关联”（[交汇契约](../blueprint/03_交汇契约与验收规格.md)）；合法贡献者收回仍须走页面（[终态产品](../blueprint/01_终态产品与独立复审.md)）。不要求新增第二贡献者，也不要求产品提供越权按钮。

复用[部分成功指南](CP4_PARTIAL_SUCCESS.md)新建的隔离实例。在该指南步骤8释放完成后、步骤9停止前执行：保留 supplier 实际 UI 收回成功及 GET 自有资源记录；新标签页选择 Bob，拍摄“资源与贡献”没有他人卡可选的原图。随后在项目根目录执行下列命令。交互输入刚才 supplier 成功收回的实际物理 GPU ID，不能猜一个不存在的卡。脚本仅使用 Bob 的本地凭据，不输出凭据；保留其原输出加入本人的收回历史。若端口调整过，同步修改 `base`。

```sh
uv run python - <<'PY'
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = "http://127.0.0.1:18096"
headers = json.loads(Path("runtime/cp4-partial-recall-b/client-identities.json").read_text())["bob"]
print("刚才 supplier UI 成功收回的物理 GPU ID: ", end="", flush=True)
with open("/dev/tty") as terminal:
    gpu = terminal.readline().strip()
assert gpu, "必须填写已核对的实际 GPU ID"
actor = {"actor_id": "bob", "owner_id": "bob", "role": "researcher"}
requests = []
for method, path, body in [
    ("GET", "/x/v1/work-intents", None),
    ("POST", "/x/v1/contributions/recall",
     {"gpu_ids": [gpu], "notice_seconds": 30, "duration_seconds": 60}),
]:
    request = Request(base + path, method=method, headers={**headers, "Content-Type": "application/json"},
                      data=None if body is None else json.dumps(body).encode())
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read().decode("utf-8")
        requests.append({"method": method, "path": path, "status_code": response.code,
                         "request_body": body, "response": json.loads(raw), "raw_response": raw,
                         "actor": actor, "origin": "explicit_http"})
print(json.dumps({"requests": requests}, ensure_ascii=False, indent=2))
PY
```

期望先收到 Bob 的正常列表 200，再收到同卡收回 403、`detail=Contributor authority is required`。保留实际结果；其他接口的 403、无效身份 401、未知 GPU 或代理代操作都不能替代此负例。浏览器本身没有越权按钮，因此此项应如实写“UI 不可选择 + 本人显式 HTTP 被拒绝”，不能写“点击他人卡被拒绝”。

## 同构建视频候选

保留一次至少30秒的连续屏幕排练实录，覆盖实际场景的三个以上步骤。这里检查 CP4 候选一致性；不冒称完成 CP5 的3–5分钟最终演示或外部提交。

记录 `evidence/cp4/video_rehearsal_observer.json`，使用 `cp4-video-rehearsal-v1`：当前 `build`、真人 `operator`、`video`、`scene`、`deployment_observation`、`model`、`scene_id`、`credentials_visible=false`、时间及未解决项。场景文件使用 `cp4-rehearsal-scene-v1`，含相同构建、场景与执行模式，`recording_method=continuous_screen_recording`、`edited_for_outcome=false`、带实际时间码的 `steps`。视频、场景和部署观察的模型必须一致。验证器用 ffprobe 检查真实视频时长和流，不能用封面图、计划或录制脚本替代媒体。

`deployment_observation` 必须引用上述同构建完整 `cp4-clean-deploy-v1` 真人观察和实际日志、HTTP、截图，并填写本次实际 `model`；仅写构建与模式的简短占位记录不能通过。

## 图表与原始数据

`cp4_evidence_index.py --prepare-index` 只复制与复算封存数据，不重跑125次实验。它把原字节无损压缩，并同时保存压缩文件哈希与原文件哈希。原实验构建、模型、输入、策略、run 和原始快照保持原值；CP4 当前构建只代表审核和展示的版本。

索引覆盖策略报告总览、场景族表、运行表，以及当前研究报告的动作计数、F 分解、微型对照和 Owner 伤害结果。MCM 资料列为历史引用，不宣称本轮重做旧实验。当前报告新增数值图表后，必须先更新完整图表目录和计算记录，再验收。

显式 `--prepare-index` 或 `--deep-audit` 从原快照和决策重算，写 `figure_deep_audit.json`。页面和普通 `review()` 复核当前构建、该深审记录、索引、计算程序和每个保留源文件的哈希，不在每次页面请求重新解压上千兆原始JSON。源数据、图表或计算程序改变后旧深审立即失效；这是完整性审核，不宣称每次页面读取都重做演算。

## 实际审核命令

```sh
uv run python tests/acceptance/cp4/cp4_clean_deploy.py
uv run python tests/acceptance/cp4/cp4_user_tasks.py
uv run python tests/acceptance/cp4/cp4_evidence_index.py
uv run python tests/acceptance/cp4/cp4_video_rehearsal.py
```

四个入口只读源观察，写单项审核结果；缺记录为 NOT_RUN，过期、篡改、缺原始材料、代理冒充真人或记录不一致为 NOT_MET。它们能校验证据一致性，不能独立认证镜头外是谁操作；仍需要两位真人对自己的观察负责。正式 CP4 还必须满足可靠性、规模、延迟等其余门槛，不由这四项单独放行。

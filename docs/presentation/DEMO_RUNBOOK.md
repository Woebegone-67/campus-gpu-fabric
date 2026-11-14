# 织算现场演示执行手册

2026-11-15。给A/B共同执行。接口与启动方式核对到架构提交 `c3fad1bac8a208b74de99b1c769dee28e771ebc5`；这是编写基线，不是未来最终合并构建的固定编号。B新UI尚未合入，本手册的排练、真人操作和录像均未执行。正式4 CPU/8 GiB测试正在独立进行，初步延迟仍未达标；本手册不签署CP4通过。CP3的R=1.00保持冻结。

## 1. 目的、分工与交付物

中心命题：**共享是否成功，要看中断后保留并真正接续的工作，而不只看曾经分配了多少卡。** 本次只检验声明范围内的一项工作。checkpoint、工件哈希和抢占不是我们首创；现场成功也不证明调度优于基线。

| 人员 | 负责的动作 | 不能代替的事 |
|---|---|---|
| A：实例与证据负责人 | 核对构建、启动独立实例、只读采集执行事实、独立核对下载结果、整理原件 | 不替B签真人观察，不在后台改状态让演示成功 |
| B：产品操作者 | 用自己的浏览器操作借用/贡献页面，核对新UI与HTTP一致，记录问题 | 不用静态图、代理脚本或设计稿代替实际页面 |
| 评委/挑战者 | 在合法窗口决定收回时机，并选择继续或取消 | 不需提供凭据，不通过测试后门触发成功 |

借用、贡献是同一普通账户的两类能力，不是互斥职业。现场用两名member账户只是为了形成双方冲突；管理员不是主角。A/B可以交换操作职责，正式人验各自署名。

每次排练交付：构建与启动状态、该次工作完整请求/响应时间线、原生截图、浏览器原下载、脱敏执行核对表、独立结果核对输出、连续原始录像、操作者实际观察和未完成项。成功与取消分别使用新工作ID；禁止将两项工作剪接成同一项。

## 2. 冻结与启动：先落实本次实际候选

1. 合并所需后端与B UI后，由A确定唯一候选提交。保留 `git status --short`，源码/配置/文档不得在录制中变化。新文档也在源码指纹范围内，不能只记短SHA。
2. 依赖与相关工程检查按候选交接完成。不要为每次排练重跑CP0、125轮策略比较或正式规模负载；失败修复后只做必要受影响检查，正式门禁仍单独执行。
3. 在本机项目根目录使用下列**全新**目录。端口若占用，统一换成四个空闲端口并记录；不要杀别人的进程或沿用旧候选目录。`127.0.0.1`只代表当前机器。

```sh
umask 077
mkdir -p evidence/roadshow
mkdir evidence/roadshow/run-01
git rev-parse HEAD > evidence/roadshow/run-01/git-commit.txt
git status --short > evidence/roadshow/run-01/git-status.txt
uv run python - <<'PY' > evidence/roadshow/run-01/build.json
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
from check_checkpoint import current_build
print(json.dumps(current_build(Path.cwd()), indent=2))
PY
uv run python scripts/cp2_joint_session.py start \
  --runtime runtime/roadshow-a-01 \
  --logical-api-port 18220 --logical-web-port 15220 \
  --cpu-api-port 18221 --cpu-web-port 15221 \
  > evidence/roadshow/run-01/session-start.json
uv run python scripts/cp2_joint_session.py status \
  --runtime runtime/roadshow-a-01 \
  > evidence/roadshow/run-01/session-status.json
```

这是仓库现有会话管理器，同时启动逻辑和CPU两个隔离实例；**主线只使用CPU页面 `http://127.0.0.1:15221`，API `http://127.0.0.1:18221`**。逻辑页面15220不参与此次恢复，也不推进其时钟。B自己的机器/排练用另一个目录，例如 `runtime/roadshow-b-01`，所有命令中的目录一起替换。

查看status：四服务应owned_running，构建与启动匹配；`source_matches_start`须为true。健康200只证明可用，不等于CP4通过。管理脚本会清理继承的 `FABRIC_`/`VITE_` 环境并设置自己的模式；不要以为启动前临时设置CPU参数能覆盖它。

本主线不要求历史实验报告在线可用，启动命令未配置历史归档。若要打开旧CP3报告，按 [CP4候选说明](../handoff/CP4_CANDIDATE.md) 额外提供原归档目录和两个已封存摘要，保留原实验构建；不要从待核对文件重算摘要来“修好”错误。主线讲R=1.00可使用明确标明历史构建的原报告片段，不冒充本实例新实验。

### 两个普通账户与真实能力

- 两个CPU标签页分别导入 `runtime/roadshow-a-01/cpu/client-identities.json`，选择alice与supplier。新建文件中的Alice、Bob、Supplier都是member，Admin单独是administrator。老目录不会自动升级，故本手册要求新实例。
- 各自 `GET /x/v1/session` 应200，owner分别为alice/supplier，capabilities包含 `use_compute` 与 `contribute_resources`，`is_administrator=false`。凭据和X-Owner由现有客户端发送，不能改显示名冒充。
- `GET /v1/version` 应有 `execution_mode=cpu_reference`、`real_cpu_execution=true`、`real_gpu_execution=false`、`checkpoint_recovery=true`。账户capabilities与服务capabilities是两个对象。
- 身份文件、Bearer、执行token、HMAC密钥只留本机。录屏前折叠凭据区；刷新整页后须重新导入，不能假装已有持久登录。
- 当前旧UI尚未接入新的统一账户体验；不能据后端session成功签新UI通过。B需按第9节接线后实际验证。

### 确认库存，不隐藏备用卡

管理器把 `configs/cp1_inventory.json` 复制进新实例：supplier拥有 `sim-a10-gpu0`、`sim-a10-gpu1` 两张24 GiB A10，另有A100/切片与CPU节点。使用 `GET /x/v1/contributions?limit=50` 核对归属；公共 `GET /v1/capacity?limit=500` 的节点刻意不暴露贡献者owner，不能用它推断所有权。

本工作显式要求A10×1。为了观察无兼容资源的等待，supplier应收回**两张A10**；只收回实际占用的一张，另一张可能允许立即接续，这不是系统错误，也不能记录成“已验证无资源等待”。不在线登记新供给，不改库存文件，不清空历史。新实例应无其他活动工作或遗留收回窗口；如不满足，先按实际合法动作处理或另建新实例。

## 3. 参数卡：提交前读给操作者

| 页面字段 | 本次值 | 依据/说明 |
|---|---|---|
| 执行方式 | CPU参考实例 | 由服务决定，不是任意训练镜像执行 |
| 镜像/命令 | 保留 `python:3.12-slim` / `["python", "-V"]` 元数据 | 当前两模式均不执行这些字段；提交前明确说明 |
| CPU、内存 | 1000 millicores、4096 MiB | 当前表单与示例库存均支持 |
| GPU | 数量1、型号A10、最低显存0、profile留空 | 避免any匹配到未收回的A100 |
| 租约 | 300秒，优先级0 | TTL合法范围30–3600；恢复保留原截止 |
| 中断处理 | 保存点恢复（checkpoint） | 不用best_effort代替可信接续 |
| 结算 | **结束后申请自动归还**（release_on_terminal） | 成功与取消两分支都要求核对归还；不是只成功归还 |
| 保存报告窗口 | 15秒 | 实际保存截止还受收回通知截止限制 |
| 恢复/任务重试 | max_restarts=2，max_retries=0 | 等待不等于已经使用一次恢复预算 |
| 任务超时 | 留空 | 不沿用W2的2秒超时，不把timeout当工期 |
| supplier通知 | notice_seconds=3 | 合法范围1–300；是通知期，不是保证进程3秒必停 |
| supplier占用 | duration_seconds=30 | 合法范围1–3600；从通知截止起算。比旧3/20指南多留10秒给评委选择，录制前固定并公开 |

标准会话通过 `scripts/cp2_cpu_preview.py` 运行：seed17、total_steps1,000,000、batch_steps1,000、batch_pause_seconds0.05。约50秒来自1000批的公开演示节拍，加实际运算与管理开销；它不是GPU速度或准确完成时限。普通devhost的默认CPU节拍不同，不能用其他启动方式后仍照搬“约50秒”。

看到工作真正运行后，由评委在约5–15秒的已声明窗口内选择收回时刻。操作前先确认仍在运行，不能按讲稿秒数强行判定。若已完成，本次只是普通完成，另建工作重做，原记录保留。

## 4. 接口与数据定位表

下面列的是现有实际接口。主线正常动作由UI发起，HTTP辅助必须标明；没有“上传任意checkpoint”“实时可信进度百分比”之类新增接口。

| 动作/观察 | 路径与输入 | 保存什么 |
|---|---|---|
| 账户/执行模式 | GET `/x/v1/session`；GET `/v1/version` | 授权能力、模式、响应时间与请求号，不存请求凭据 |
| 提交工作 | POST `/x/v1/work-intents`；请求见下方 | 正常202回执；`outcome/objects/version/request_id/event_ids`，不把受理当运行 |
| 查询同一工作 | GET `/x/v1/work-intents/{urlencoded_id}` | `item`与`intent`为同一工作；顶层`allocations/jobs/events/legal_actions/version/now` |
| 收回自有卡 | POST `/x/v1/contributions/recall`；`{"gpu_ids":["sim-a10-gpu0","sim-a10-gpu1"],"notice_seconds":3,"duration_seconds":30}` | 201及实际回执，supplier归属、通知/截止/不可用窗口 |
| 恢复、取消、释放 | POST `/x/v1/work-intents/{id}/actions`；`{"action":"resume或cancel或release","expected_version":{"epoch":实际值,"revision":实际值}}` | 工作GET最新version、原POST、实际结果、随后GET；这三种动作不需stage_id |
| 普通恢复HTTP辅助 | POST `/v1/allocations/{allocation_id}/resume` | 202只表示受理；再次GET确认等待、预算与新执行。只用于合法的同owner HTTP挑战 |
| 租约/任务详情 | GET `/v1/allocations/{id}`；GET `/v1/jobs/{id}` | ID、状态、placement、restart_count、pending_terminal；原租约截止另保存 |
| CPU保存点/结果 | GET `/x/v1/artifacts?uri={URL编码的fabric-cpu URI}` | 原响应字节；由工作attempt.checkpoint.artifact_uri或result.artifact_uris取得URI，不拼猜 |
| 关联决定 | GET `/x/v1/decisions/{URL编码decision_id}` | `item.decision_id`、`actual_events`、`commit_receipts/commit_status`；ID从该工作真实关联记录取 |
| 事件 | GET `/v1/events?limit=500`，有后页用响应`next_after`作`after` | `items/events`、`has_more`、cursor；按关联entity筛选，不假设服务器有work_id查询参数 |

工作动作的客户端会对确定的 `stale_version` 最多重新读取版本并重试；网络结果未知不会自动重发。保留整条请求链，不能把一次被正常重试的409当成最终失败，也不能关闭重试制造戏剧效果。Pending时UI可能合法隐藏恢复按钮；不要强行点不可用按钮。重复恢复只作为明确标出的普通HTTP辅助，可省略，不影响主要继续/取消两分支。

UI登记的请求结构如下。`intent_id`由UI生成并保持，重试不换ID；API示例中的名称是说明用，不是固定预置答案。

```json
{
  "intent_id": "实际新工作ID",
  "owner_id": "alice",
  "execution_mode": "cpu_reference",
  "settlement_action": "release_on_terminal",
  "specification": {
    "allocation": {
      "kind": "allocation.create", "owner_id": "alice",
      "ttl_seconds": 300, "priority": 0, "preemption_mode": "checkpoint",
      "checkpoint_timeout_seconds": 15, "max_restarts": 2,
      "request": {
        "cpu_millicores": 1000, "memory_mib": 4096,
        "gpu": {"count": 1, "model": "A10", "memory_mib": 0, "profile": null}
      }
    },
    "job": {"image": "python:3.12-slim", "command": ["python", "-V"], "max_retries": 0}
  }
}
```

## 5. 逐步现场执行：先公开失败条件

开场说清：这是固定真实CPU与模拟GPU库存。我们允许这项工作最后完成，也允许评委取消；不能继续时必须说明原因。以下任何项出现，保留本次失败，不换工作接续成功画面：新旧执行不安全重叠；把ready当已接续；换工作/工件；悄悄延长截止；无资源虚报运行；取消后复活；输出不等价。停止不及时即使保留隔离也不能称“按时归还成功”。

### S0：登记、运行与基准记录

1. B在alice标签页提交参数卡中的工作，进入对应详情。记录 `W=intent_id`，分配后 `L=allocation_id`，创建后 `J=job_id`，第一执行 `A1=attempts[0].ref`。默认组装host自动分配与创建，不用manual_app逐阶段按钮。
2. 刷新到第一执行 `started_at`非空且运行；保存GET和截图。记录唯一的L/J、原租约`expires_at_logical`（组装详情）及公共`expires_at`、restart_count、实际placement。完整身份保存，画面可用短ID。
3. supplier标签页已选两张属于自己的A10，填3/30，等待评委口令。此时不要再解释长表单；CPU正在真实运行。

### S1：评委选择收回时刻

4. 评委在声明窗口说“现在收回”，supplier本人点击发送。保存实际POST/201/回执及时间；不是A在后台代发测试接口。将实际recall响应时刻记为T0，通知截止与供给不可用截止以回执/查询为准；不能从T0重新给服务加30秒。
5. alice刷新：租约应进入 `PreemptRequested`，显示通知与截止。CPU执行器自动保存，不点击逻辑ready，不调用`/_test/*`、管理员时钟或私有执行器接口。
6. 等待实际停止。保留保存点的 `report_status`、`verification_status`、`artifact_uri`、原attempt；保存源执行状态与停止确认。仅收到ready或核验通过都不表示原进程已停止。源lease进入Preempted且已无placement后才进入恢复步骤；若卡已回来或对象终态，按真实状态处理。
7. 记下已核验保存点URI与原attempt，**停止确认后立即进入S2，不等下载或机制旁白讲完**。A可并行以alice身份下载，或在分支选择后补下载原JSON，记录SHA256与HTTP路径。其 `state.step=k` 应处于0与N之间才能证明本次有可保留中间进度；0不构成正进度接续证据，N说明时机过晚，不挑改数值。

### S2：先让观众看到“受理恢复但还没有运行”

8. 停止确认后、两张A10尚在不可用窗口内时，alice立即点击恢复。保留动作回执和刷新后的详情：同W/L/J、租约Pending、没有已启动的新执行、restart_count仍为原值。恢复按钮隐藏是合法提示；网络失败则先查询，不盲目重复。
9. **一看到真实Pending，评委立即选择C1继续或C2取消**；取消则随即操作，讲解穿插进行。确认当前仍Pending，且实际通知的不可用窗口尚未结束。30秒会自然过去，若窗口已过或新执行已开始，就不能记“等待中取消”；可以记录真实运行中取消，但应另开新工作补所需等待分支。不得暗加占用时长或临时增加资源来配合讲稿。

### C1：继续分支——官方视频必须完整包含

10. 等待窗口自然结束，刷新同一工作。新代次A2应沿用W/L/J、generation递增，checkpoint状态为 `verified/applied`，`resumed_attempt_id`绑定A2，URI绑定步骤7的保存点。原租约截止不变；restart_count应仅增加1。
11. 核对新进程的初始步数确实等于k（第7节的本地白名单证据）；不能只靠“已接续”标签或最终结果相同。
12. 等待实际完成；若仍运行就如实等，不以讲稿时长催出成功。终态应成功，结算最终L为Released、placement空、无pending_terminal；“成功回执”与“资源已归还”分开核对。
13. 浏览器查看并下载真实结果原JSON。标准配置应 `state.step=1000000`；值用第7节独立计算确认，不只对照文案常量。保存当前工作GET、关联决定/事件、原下载、原生截图与执行核对表。

### C2：取消分支——允许以正确取消结束现场

10. 仍Pending且供给窗口未结束时，alice点击取消并确认；保留POST及GET。结算预设release_on_terminal，核对工作cancelled、J为Cancelled、L最终Released且无placement/pending_terminal。
11. 服务继续运行，等贡献窗口结束后再刷新，并做至少两次后续观察（记录实际间隔，不称为永久证明）：不得出现该工作新的已启动执行、复活或重新持有资源。供给重新available与旧工作Released分别记录。
12. 本次正确结局是取消，不下载别的工作结果来补“完成”。既有保存点可保留为历史；不把取消当恢复成功。另一场同构建成功恢复的录像只能明确标为另一场。

## 6. 如何留下可追问的证据

建议每场新目录包含以下内容，字段按实际发生填写，未执行为NOT_RUN；目录不是正式门禁的新格式，也不自动等于人验通过。

```text
run-01/
  build.json、git-commit.txt、git-status.txt、session-start.json、session-status.json
  operator-notes.md             # 操作者、日期、工作ID、分支、实际异常与未完成项
  http-timeline.json            # 不含请求凭据；实际顺序，不重排因果
  screenshots/                  # 原生截图；保留CSS视口与DPR，禁止重采样冒充
  downloads/                    # 浏览器下载原字节；保存点、结果，不重新序列化
  execution-observation.json    # 第7节白名单派生记录，非原始签名工件
  output-check.txt              # 独立计算的实际输出
  session-export/               # 现有CLI公开快照导出
  video-original.*              # 连续原录制
  video-official.*              # 如剪辑，另附时间轴与剪辑说明
```

HTTP每项至少记录：真实操作者/owner、界面来源、时间、method、完整path/query、status、请求体、响应体、请求号；下载响应另外保留原文字节和对应文件SHA。不要保存Authorization、Cookie、token、身份文件或浏览器包含凭据的完整“Copy as curl”。DevTools导出的HAR可能含秘密，必须本机逐项检查脱敏，不能直接交付。

原下载必须来自浏览器正常按钮，不能用本地脚本生成相同JSON顶替下载。查看器格式化后的文本不能代替原件：64位整数大于JavaScript安全范围，重新JSON.stringify可能改变值。公开CPU工件接口已去掉执行token；它与磁盘上的签名封装不是同一种字节，不能要求二者SHA相同。

场景结束、服务尚在运行时用现有只读导出：

```sh
uv run python scripts/cp2_joint_session.py export \
  --runtime runtime/roadshow-a-01 \
  --output evidence/roadshow/run-01/session-export
uv run python scripts/cp2_joint_session.py status \
  --runtime runtime/roadshow-a-01 \
  > evidence/roadshow/run-01/session-status-end.json
```

导出包含公开工作/事件/租约/任务/工件快照，可能因分页状态变化标为不完整；不得手改complete。它不包含浏览器点击历史、原响应字节、进程初始步数或人的观察，不替代上述材料。源码指纹须与开场匹配，若不匹配则本次不记同构建正式录像。

## 7. CPU证据：来源、只读采集与独立核对

### 7.1 六个事实使用不同来源

| 要证明的事实 | 实际来源 | 限制 |
|---|---|---|
| 本次保存到k | alice下载工件 `state.step/value`、`attempt`、`workload_id` | ready文本不等于工件已核验；步数不是训练完成百分比 |
| 工件属于原工作 | 工件attempt内W/L/J/attempt_id/generation，工作GET中的checkpoint URI | 同值文件不能替代来源绑定 |
| 原执行停止 | 本地executor journal的stop_confirmed/exit_code、stopped原件及进程观察；公开attempt停止状态 | 没有物理确认时不宣称已腾空；PID后来可能被复用 |
| 新执行实际起点 | A2的 `started.json.payload.initial_step/resumed_from` 与manifest.initial | 这是本地执行事实，当前不是独立公开进度API |
| 实际接续与代次 | 公开checkpoint的applied/resumed_attempt_id与A2；本地新起点/事件顺序 | 新代次登记本身不够；不能仅凭最终值一致 |
| 输出正确 | 浏览器结果JSON，独立矩阵计算，不导入生产advance | 只验证这个固定整数程序，不证明任意AI/GPU训练 |

标准会话的私有执行根为 `runtime/roadshow-a-01/cpu/data/cpu-executor/`。索引文件是 `journal.json`；原文件在 `attempts/<record.key>/`，包含manifest、started、progress、stopped、result及checkpoint文件。它们有payload/signature封装，payload含执行token；journal也含token与控制消息，`secret.key`是本地HMAC密钥。**禁止整目录打包、打开全文录屏或上传。** 不要为只读检查新建 `CpuReferenceExecutor`：构造器可能创建文件。不得调用poll/dispatch/ack来“采集”证据。

### 7.2 A的最小只读白名单采集

下面代码是操作者可执行的辅助观察步骤，本文件编写时未对最终实例运行。仅在自己的正常场景终态或已确认停止时执行，`实际工作ID`替换为本场W。它读取已存在文件，HMAC仅在本机核对，不输出密钥/token，不改业务状态；输出是**脱敏派生观察**，不是原始签名工件或CP4通过回执。原始秘密文件留在本机供受控检查。

```sh
uv run python - runtime/roadshow-a-01/cpu/data/cpu-executor \
  '实际工作ID' evidence/roadshow/run-01/execution-observation.json <<'PY'
import fcntl, hashlib, hmac, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1]).resolve()
work_id, output = sys.argv[2], Path(sys.argv[3])
if output.exists():
    raise SystemExit('Output already exists; preserve it and choose a new observation path')
key = (root / 'secret.key').read_bytes()
if len(key) != 32:
    raise SystemExit('Invalid local executor key')

def signed_payload(path, record):
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Unsafe artifact path')
    raw = path.read_bytes()
    document = json.loads(raw)
    payload = document['payload']
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, document['signature']):
        raise ValueError('Local signature mismatch')
    for field in ('owner_id', 'epoch', 'attempt', 'token'):
        if payload.get(field) != record[field]:
            raise ValueError('Execution identity mismatch')
    allowed = ('owner_id', 'epoch', 'attempt', 'pid', 'algorithm', 'workload_id',
               'kind', 'initial_step', 'resumed_from', 'initial', 'config', 'state',
               'total_steps', 'started_monotonic_ns', 'finished_monotonic_ns')
    return {'file': path.name, 'sha256_of_private_signed_bytes': hashlib.sha256(raw).hexdigest(),
            'local_hmac_checked': True, 'fields': {k: payload[k] for k in allowed if k in payload}}

with (root / 'executor.lock').open('rb') as lock:
    fcntl.flock(lock, fcntl.LOCK_SH)
    journal_raw = (root / 'journal.json').read_bytes()
    journal = json.loads(journal_raw)
    records = sorted((r for r in journal['attempts'].values()
                      if r['attempt']['intent_id'] == work_id),
                     key=lambda r: r['attempt']['generation'])
    if not records:
        raise ValueError('No execution for this work')
    observed = []
    for record in records:
        fields = ('owner_id', 'epoch', 'attempt', 'pid', 'status', 'stop_confirmed',
                  'exit_code', 'initial', 'resumed_from', 'workload_id', 'config')
        item = {k: record[k] for k in fields if k in record}
        directory = root / 'attempts' / record['key']
        names = ['manifest.json', 'started.json', 'stopped.json', 'result.json']
        names += sorted(p.name for p in directory.glob('checkpoint-*.json'))
        item['files'] = [signed_payload(directory / name, record)
                         for name in names if (directory / name).is_file()]
        item['registered_artifacts'] = []
        for uri, entry in journal['artifacts'].items():
            if entry['record_key'] != record['key']:
                continue
            registered = signed_payload(root / entry['path'], record)
            if registered['sha256_of_private_signed_bytes'] != entry['sha256']:
                raise ValueError('Registered artifact bytes changed')
            item['registered_artifacts'].append({
                'uri': uri, 'kind': entry['kind'], 'file': registered['file'],
                'registered_sha256': entry['sha256'], 'registry_bytes_equal': True})
        pid = record.get('pid')
        if type(pid) is int and pid > 1:
            probe = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                   capture_output=True, text=True, check=False)
            stat = probe.stdout.strip()
            item['pid_observation'] = {
                'stat': stat, 'returncode': probe.returncode,
                'missing_or_zombie': (probe.returncode == 1 and not stat) or stat.startswith('Z')}
        observed.append(item)
    events = [{'sequence': x['sequence'],
               'event': {k: x['event'][k] for k in ('kind', 'attempt', 'observed_at')
                         if k in x['event']}}
              for x in journal['events'] if x['event']['attempt']['intent_id'] == work_id]
report = {'scope': 'LOCAL_READ_ONLY_DERIVED_OBSERVATION_NOT_HUMAN_ACCEPTANCE',
          'observed_at': datetime.now(timezone.utc).isoformat(), 'work_id': work_id,
          'source_journal_sha256': hashlib.sha256(journal_raw).hexdigest(),
          'attempts': observed, 'events': events}
with output.open('x', encoding='utf-8') as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2)
    stream.write('\n')
print('Saved a derived observation; no credential material was exported')
PY
```

核对A1/A2：A1停止确认、正常停止原件/事件在A2启动之前；A2 `initial.step` 与started.initial_step均等于下载保存点k，resumed_from等于公开URI，W/L/J不变、generation增1。`registered_artifacts`将公开URI对应到登记的具体私有文件与摘要；按该映射找保存点，不猜“最新checkpoint文件”。stopped文件是在worker返回前写入，仅文件存在不等于进程已退出，须另核对adapter停止确认。若只取得adapter确认而没有相应stopped原件，则如实注明证据范围；不得凭缺失文件填一个停止时刻。`ps`结果是采集当时观察，不证明历史；PID存在可能是复用，不能杀它来“修复”核验。

公共下载去除了token与签名封装，所以与本地原件只做**去除token后的payload字段一致性**比较；原文件SHA用于记录来源，不能直接与下载SHA比较。上面片段只导出字段子集，没有自动完成完整公共payload比对；只核对展示字段时应记录“展示字段一致”，不能升级为完整字节或完整payload一致。若另做完整payload比对，应在本机读取原件、去除token后比较，仍不得导出秘密。文件哈希/HMAC不是对恶意服务端的远程证明。本地key、执行器和主机属于信任边界。

### 7.3 独立输出核对：不调用生产advance

标准演示程序定义为 `x(i+1) = (1664525*x(i) + 1013904223 + i) mod 2^64`，x(0)=17。下面用三维矩阵快速幂计算保存点及最终值，与生产逐步循环不同；只用Python标准库，64位整数保持精度。文件应为浏览器下载的原payload，不是私有签名封装。将两个文件名换成本场原件。

```sh
set -C
uv run python - evidence/roadshow/run-01/downloads/checkpoint.json \
  evidence/roadshow/run-01/downloads/result.json \
  > evidence/roadshow/run-01/output-check.txt <<'PY'
import hashlib, json, sys
from pathlib import Path

MOD = 1 << 64
SEED, TOTAL = 17, 1_000_000
def multiply(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) % MOD
             for j in range(3)] for i in range(3)]
def expected_value(steps):
    power = [[1664525, 1, 1013904223], [0, 1, 1], [0, 0, 1]]
    product = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    while steps:
        if steps & 1:
            product = multiply(product, power)
        power = multiply(power, power)
        steps >>= 1
    return (product[0][0] * SEED + product[0][2]) % MOD
config = {'algorithm': 'lcg64-indexed-v1', 'total_steps': TOTAL, 'seed': SEED}
workload = hashlib.sha256(json.dumps(config, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode()).hexdigest()
for filename, kind in zip(sys.argv[1:], ('checkpoint', 'result'), strict=True):
    raw = Path(filename).read_bytes()
    data = json.loads(raw)
    state = data['state']
    step, value = state['step'], state['value']
    if (data['kind'] != kind or data['algorithm'] != config['algorithm']
            or data['workload_id'] != workload or data['total_steps'] != TOTAL
            or type(step) is not int or type(value) is not int
            or not 0 <= step <= TOTAL or not 0 <= value < MOD or 'token' in data):
        raise ValueError('Unexpected artifact/model')
    if value != expected_value(step) or (kind == 'result' and step != TOTAL):
        raise ValueError('Independent value mismatch')
    print(json.dumps({'file': filename, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'kind': kind, 'step': step, 'value': value,
                      'independent_value_equal': True}))
print('Only fixed-reference output equivalence checked; execution linkage is separate')
PY
```

`set -C`让同名输出已存在时停止，重做核对须换新输出名并保留原记录。此核对不导入CPU生产模块，不把下载自报的seed当可信输入。若最终冻结配置变了，应事先修改并记录独立核对的模型参数，不在失败后修改期望值。最终值一致不证明实际复用了进度，必须结合7.1/7.2；保存后到停止前可能丢弃尾部计算，不声称零损失或训练加速。

## 8. 官方4分30秒实录与现场分支

官方要求3–5分钟实际运行、异常和恢复。正式视频必须拍到C1的完整实际恢复；现场评委可以选择C2，并以正确取消结束。两者不是同一个任务的剪辑变体。

| 讲述时间预算 | 实际画面与动作 | 操作者提醒 |
|---|---|---|
| 0:00–0:20 | 实例/构建/模式、两名member、失败判据 | 凭据区折叠，不以测试数量开场 |
| 0:20–1:20 | alice提交并真实运行→评委选时机→supplier收回→实际停止确认→立即恢复→真实Pending后立即选择分支 | 这一串由状态驱动，绝不拆成按墙钟等候的台词段。正式视频选择C1；机制旁白穿插，下载/核对可稍后补 |
| 1:20–2:20 | 呈现已选择C1后的真实等待/接续，核对同工作A2；若已接续或完成，就展示已发生的结果 | 不是等到1:20才按恢复。现场若选择C2，则展示实际取消与窗口结束后的观察 |
| 2:20–3:10 | 成功、租约释放、浏览器下载，展示独立核对 | 未完成就继续等，不能切别的成功工作 |
| 3:10–3:55 | 关键事件/决定与执行起点核对表 | 公共与本地辅助证据明确区分 |
| 3:55–4:30 | 结果边界、R=1.00、CP4实际未达项 | 新架构是否达标只引用最终实际结果 |

时间是讲述预算，不是完成保证或操作触发器。现场以T0及服务实际截止观察：停止确认后立刻恢复，Pending出现后立刻选择；不能等固定旁白结束。若供给窗口先结束，本场不声称验证了Pending取消，另场补测；参数仍固定3/30，不暗加时、不加资源。原始连续录像完整保留；如剪去重复等待，标明剪辑点与实际经过时长，交付原片。不得剪掉异常、偷偷换ID或把旧构建素材混成同构建链。未按时完成就保留真实记录后重新安排录制，失败场不删除。

现场5分钟只跑一条由评委选择的结局；8–12分钟可加一个已排练的对抗条件和机制解释。正式规模负载、容器限额测试、125轮实验都不在现场主线运行。讨论架构性能时明确普通请求延迟与调度F/作业时间不同。

## 9. B需要落实的界面与人验清单

以 [UI设计说明](../design/UI_DESIGN_SPEC.md)、[数据映射](../design/UI_DATA_AND_COMPONENT_MAP.md) 和 [账户能力](../handoff/ACCOUNT_CAPABILITIES.md) 为准，本手册不另造一套页面。

- [ ] 同一member显示“我的工作”“我的设备”，不靠Alice/Supplier姓名推断能力；管理员入口单独授权。
- [ ] 登记前清楚说明固定CPU/GPU模拟；镜像字段不得暗示会执行训练。
- [ ] 工作详情维持同W/L/J与草稿/导航，空间转场不重发任何写操作；取消、恢复、收回均来自真实legal_actions与服务校验。
- [ ] ready、verified、applied及停止/归还不同事实准确显示；没有可信总量不造进度条。initial_step是本地证据，不冒充新增公开接口。
- [ ] 等待显示原因和可行下一步；Pending隐藏恢复按钮可以接受；取消之后不残留“将恢复”提示。
- [ ] 网络结果不明不自动创建新工作；确定stale_version按原客户端重试，epoch改变停止重试；错误和等待不被绿色成功遮住。
- [ ] 下载使用原响应文本，保留大整数；本人浏览器实下保存点与结果，跨owner拒绝单独按正式人验记录。
- [ ] 表单与正文不透装饰背景；总览/工作台可点击与键盘操作，手势不是唯一入口；录屏标明真实CSS视口/DPR。
- [ ] A/B各自执行成功、等待取消两分支；本次实际工作ID、分支与失败记录对应原件。未执行只勾未测，不由代理代签。
- [ ] 结束后刷新重新导入，仍能查本场工作与原件；有旧迟到响应时不跨身份显示。

本清单只是路演排练范围，不能替代 [CP4真人验收](../handoff/CP4_HUMAN_ACCEPTANCE.md) 的干净部署、完整六任务、图表来源、视频及其他正式条款。正式门禁要求的观察者材料需按其格式另行提交，不因这里演示成功自动通过。

## 10. 失败退路与收尾

| 实际情况 | 立即采取的行动 | 本场应怎样记录 |
|---|---|---|
| 401/非JSON/服务断开 | 停止写操作；核对本实例URL、session、身份；先查询原工作 | 连接/授权未完成，不记空列表或业务失败已恢复 |
| 收回前任务已完成 | 保留普通完成记录；准备新工作重新开始 | 本场未验证中断接续 |
| 保存失败/没有核验工件 | 展示真实失败与合法动作；不手填ready，不篡改工件 | 接续未达；必要时正常取消/释放并核对停止 |
| 无资源但意外立即恢复 | 核对是否只收回一张、型号any或存在其他兼容供给 | 只证明即时恢复，未证明容量不足等待 |
| 取消太晚、新代次已开始 | 保留运行中取消的真实过程；另场补Pending取消 | 不改写时间，不冒充等待分支 |
| 停止未确认或隔离 | 保留持有/隔离状态，由A按实际原因处置 | 不能为腾空演示把账本直接改Released |
| 原下载/独立核对不一致 | 停止“恢复成功”结论，保留文件、HTTP与配置 | 数据/执行验证失败，不改期望值配合文件 |
| 候选源码变化/混入旧录像 | 停止同构建通过主张，保留开发记录 | 冻结后重做必要相关场景，不改签旧记录 |
| 现场时间不够 | 明确本次未完成；可播放标识了构建/场次的备份录像 | 备份不是现场成功，不隐去等待和失败 |

关闭前先在UI确认本场工作已结束，租约最终无占用，旧CPU停止已确认；如果仍有不明确状态，不把停服务当取消。然后只停止本会话：

```sh
uv run python scripts/cp2_joint_session.py stop --runtime runtime/roadshow-a-01
```

脚本保留目录；不要reset、删除数据库或清理别人的端口。服务停机不等于CPU任务被取消。核对交付包不含auth/client-identities/secret.key/私有journal/未脱敏HAR，保留原件SHA、构建、操作者与实际日期。

## 11. 尚未执行的事项与来源

本文件只完成代码/指南一致性核对。上述新候选排练、B新UI接线、评委现场选择、浏览器原下载、本地派生观察、独立结果核对及4分30实录均为**待执行**。文中的命令片段不是已安装的新CLI或已运行回执；A/B应在首次排练记录其实际输出。技术结果与人工记录分别认定，不给CP4 PASS。

来源：`scripts/cp2_joint_session.py`（隔离启动、status、export与stop）；`scripts/cp2_cpu_preview.py`（实际演示节拍）；`scripts/init_cp1_demo.py`与账户交接（新member）；`configs/cp1_inventory.json`（两张A10）；`web/src/pages/Run.tsx`、`Resources.tsx`、`Continuity.tsx`和`web/src/api.ts`（实际表单/动作/重试）；`api/mutations/http.py`、`api/queries/routes.py`、`app.py`（路径与响应）；`executors/cpu_reference.py`、`cpu_worker.py`、`cpu_files.py`（私有证据/公开脱敏/计算模型）；`tests/acceptance/cp3/cp3_cpu_equivalence.py`（独立矩阵核验方法）；[原CPU人工路径](../handoff/CP3_JOINT_ACCEPTANCE.md)（历史指南，不能沿用旧端口/旧构建认定本轮通过）。

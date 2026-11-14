# B 双空间界面与实体存储接线交付

日期：2026-11-15。工作目录：`/Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b`。分支：`feat/b-cp4-starwave`。

本次交付完成总览 / 工作台双空间界面，以及可信账户和新实体存储后端的接线。真实 Chrome 的合成接口回归为 **17/17 PASS**；独立新后端的浏览器只读接线为 **13 次 GET 全部 HTTP 200**。这两项分别验证界面行为和实际读取链路，不代表完整 CP4 已通过，也不代替最终构建的 A/B 真人业务验收。

## 1. 构建来源与当前结论

| 项目 | 交付范围 / 状态 |
| --- | --- |
| A 可信账户来源 | `7c71b3e`，包含 `GET /x/v1/session` 与普通账户能力投影 |
| A 实体存储来源 | `c3fad1bac8a208b74de99b1c769dee28e771ebc5`，继承上述账户变更，SQLite schema 3 |
| B 界面 | 总览 / 工作台、身份与查询隔离、工作 / 设备页面呈现、限定区域手势与相应测试 |
| 合并与保存 | 账户合并 `9543d8c`；架构合并 `d5aca86`；B 存储观测适配 `455f872`；其后的独立 B UI 提交包含本文件，可用 `git log -1 -- docs/handoff/B_UI_V2_DELIVERY.md` 定位。上述提交仅本地保存，尚未推送 |
| 全工程回归 | 最终一轮 `make cp0` 退出 0：Python **1165 passed**、UI **102/102**、契约 / Ruff / 构建 / 静态检查通过、5 项独立 HTTP 启动检查通过；另跑客户端 **15/15** |
| 完整 CP4 | **未通过 / 尚未收口**；本次不重写既有正式负载、部署、可靠性及真人验收结论 |

架构与兼容性依据：[实体存储交接](STORAGE_ARCHITECTURE.md)、[账户能力说明](ACCOUNT_CAPABILITIES.md)、[UI 接管说明](UI_B_HANDOFF.md)。本轮不更改冻结公共 API，不把内部 Merkle 摘要作为用户任务进度。

## 2. 已完成的产品接线

### 总览与工作台

- 默认进入总览，以当前账户已加载的工作、授权租约与贡献呈现当前工作、待处理提醒、设备摘要及最近结果。
- 工作台保留工作、资源、连续性、决策、事件、验收的业务组件和接口。原 `#run`、`#resources`、`#continuity`、`#decisions`、`#events`、`#acceptance` 哈希及工作 / 决策深链接继续可达。
- 总览中的工作入口打开同一工作详情；浏览器前进 / 后退、顶部及侧栏切换可用。两个空间使用独立内部滚动位置；同身份下往返保留当前工作台子页、当前页筛选和草稿。
- 隐藏空间暂停页面查询。保持草稿所需的组件继续保留，避免切空间丢失输入；隐藏页面不持续后台轮询。
- 完成工作与释放租约分别显示；提醒依据实际授权租约及工作状态，不能从工作完成推断资源已归还。普通排队不会被一概归为异常。

### 可信账户与失败处理

- 账户能力仅依据已验证的 `/x/v1/session`，不根据导入文件的身份名称或 `/v1/version` 服务能力猜测用户权限。普通账户可同时使用算力和贡献资源；纯贡献者的工作台目标指向资源页面。
- 未取得可信 session 时不读取受保护业务数据；无相应能力时不发起贡献者 / 管理员查询。
- 切换或清除身份时清除旧对象、草稿、筛选和手势状态，旧身份迟到响应不得写回。观察到新 epoch 时重建工作区、清除旧草稿，并拒绝更旧快照回写。
- 同身份 session 暂时失联时保留已显示数据与草稿，同时禁用工作区操作并由 API 写入守卫拒绝变更；身份失效的 4xx 响应清除旧授权视图。普通列表刷新失败保留上一份结果并显示失败状态。
- 身份文件仅在浏览器内存解析；令牌不写入 URL、localStorage 或 sessionStorage，也不放入交付截图与仓库。

### 已确认的视觉与交互

- 总览复用已冻结 Starwave 引擎及预设：纯黑背景、连续星芒曲面、点击弹性波纹、中性玻璃；没有恢复紫色渐变，也没有导入独立 demo 的 `main.js`。
- 侧栏保持纯黑并位于星芒画布之外。工作台使用可读业务表面；离开总览后释放艺术 Canvas，保留业务 DOM。返回总览重新建立单一画布。
- 支持减少动态效果和暂停背景。浏览器媒体模拟确认 reduced motion 下不持续绘制，工作台中 Canvas 数为 0。
- 页头横向手势 **默认关闭**。用户在显示设置中开启后，仅限定页头区域接收合格横向输入；有方向锁、取消阈值、冷却及惯性保护，排除交互控件、可滚动区域、弹窗和选中文本。身份变化后恢复关闭。
- 按钮与键盘是可用的基础切换方式。模拟 `mouse.wheel` 的通过结果不等同于真实触控板或系统返回手势验证。

## 3. 总览数据范围与快照限制

总览不是全局仪表盘，也不是全部待办清单。当前页面只读取下表范围，页面文案明确标注加载范围；后续页未加载的对象不会被计入摘要或提醒。

| 来源 | 本次请求上限 | 使用方式 |
| --- | --- | --- |
| `/x/v1/work-intents?limit=25` | 25 项工作 | 当前账户工作、结果、工作自身的人工处理状态；仅显示已加载集合的部分摘要 |
| `/v1/allocations?limit=50` | 50 项授权租约 | 通过工作 `allocation_ids` 联接，补充收回、停止、恢复与终态仍占用等事实；不假设列表含嵌套租约 |
| `/x/v1/contributions?limit=50` | 50 项贡献记录 | 当前账户贡献节点及物理 GPU 摘要；切片不重复计为物理卡，不称为实际 GPU 利用率 |

工作与租约是独立 HTTP 读取，不保证同一 revision。不同 revision 会显示快照差异提示；不同 epoch 不联接。租约读取失败、尚未完成或未覆盖相关租约时，页面提示提醒不完整，不把“没有读到”解释为“已释放”或“没有问题”。

待处理区域仅展示当前候选中的前三项：停止待确认、收回通知优先，再考虑人工处理、可恢复及终态仍占用；同类按截止 / 发生时间排序。忽略提醒仅影响本次本地会话，不修改后端、不代表问题已解决。最近结果依据已记录结果时间排序，缺少时间的记录置后。当前工作、贡献和最近结果各自的摘要数量均不代表全量。

## 4. 本地预览、启动与身份导入

本轮隔离环境：

| 项目 | 地址 / 路径 |
| --- | --- |
| 前端 | `http://127.0.0.1:15191` |
| API | `http://127.0.0.1:18096`，经 Vite 代理访问 |
| Runtime | `/Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b/runtime/b-ui-v2-entity` |
| 浏览器身份文件 | `/Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b/runtime/b-ui-v2-entity/client-identities.json` |
| 服务端身份文件 | 同目录 `auth.json`；只在本地使用，不复制到报告或 Git |

集成人已用只读 SQLite 查询确认该实例的 `runtime/b-ui-v2-entity/fabric.sqlite3` 为 `user_version = 3`，没有旧 `fabric_state` 表。这是新建隔离实例，不是旧实例迁移演练。

页面打开账户面板，选择上述 `client-identities.json`，在身份下拉框选择 `alice`，确认可信账户读取成功后关闭面板。本轮实际浏览器验证完成了该导入及后续只读访问。刷新浏览器后需重新导入。

环境已运行时直接使用上述地址。需要重启时，先确认端口对应本轮实例，仅停止自己明确识别的进程；不要覆盖旧 `runtime/b-ui-v2` 或其他 CP 验收实例。以下为在两个终端中的复用命令，不会替换已存在的身份文件：

```bash
cd /Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b
FABRIC_INSTANCE=b-ui-v2-entity FABRIC_MODE=logical_simulation \
FABRIC_DATA_DIR="$PWD/runtime/b-ui-v2-entity" \
FABRIC_AUTH_FILE="$PWD/runtime/b-ui-v2-entity/auth.json" \
FABRIC_INVENTORY_FILE="$PWD/configs/cp1_inventory.json" \
FABRIC_SCHEDULING_MODE=builtin \
PORT=18096 bash web/dev-local.sh api
```

```bash
cd /Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b
FABRIC_API_TARGET=http://127.0.0.1:18096 WEB_PORT=15191 bash web/dev-local.sh web
```

这是本地逻辑模拟实例，GPU 库存为模拟数据。不要将这个预览指向生产实例；本轮实际浏览器验收未触发资源申请、收回、恢复、支付或其他真实业务写入。

## 5. 验证结果与原始证据

| 验证 | 结果 | 能证明 / 不能证明 |
| --- | --- | --- |
| Chrome 界面回归 | **17/17 PASS** | 真实 Chrome `157.0.8103.51` 渲染与交互，所有业务 API 由合成 fixture 替代；证明状态、路由、身份隔离、响应式等 UI 行为，不证明真实业务事务成功 |
| 新后端只读浏览器接线 | **13 次 GET 全 200**；无页面异常、无被拦截请求 | 实际本地后端、真实 Alice 身份导入，经 Vite 代理读 session / 工作 / 租约 / 任务 / 容量 / 贡献；当前为空工作实例，不证明创建到回收的业务闭环 |
| B 负载工具 schema 3 兼容 | **34 passed**，定向 pytest；Ruff 通过 | 独立 SQL 观测、迁移后陈旧旧表隔离、并发一致快照及小规模隔离 smoke；没有重新运行正式 CP4 负载 |
| 全工程测试与构建 | **PASS**：1165 Python、102 UI、15 client；构建及 CP0 检查通过 | 本轮合并工作树实跑；CP0 是工程门禁，不代表 CP4 产品与性能验收 |
| A/B 真人验收与完整 CP4 | **未完成** | 自动检查不能代签真人，旧 CP 流程也不能替代本构建验收 |

最终回执：

- [Chrome 17 项原始结果](../../evidence/cp4/dual-space/20261115/browser-release/browser-results.json)。覆盖默认总览、租约提醒、部分读取失败、同工作导航、双空间草稿 / 筛选 / 滚动恢复、旧深链接、身份与 epoch 隔离、可信 session、能力限制、空态、刷新失败、艺术层生命周期、键盘焦点、手势与窄屏。
- [实际后端 13 次 GET 原始结果](../../evidence/cp4/dual-space/20261115/actual-backend-readonly-final/readonly-browser-results.json)。13 为请求次数，不是 13 个不同端点。
- [完整工程输出](../../evidence/cp4/dual-space/20261115/engineering/make-cp0-final.txt) 与 [客户端输出](../../evidence/cp4/dual-space/20261115/engineering/client-final.txt)。执行环境为 Python 3.12.16、Node 22；最终完整 Python 用时 345.23 秒。日志中保留一个上游 Starlette/httpx 弃用提示，无失败。
- 可复现 fixture 脚本：`web/tests/dual-space.browser.mjs`、`web/tests/dual-space.fixtures.mjs`。该套件拦截业务接口，不将合成场景请求透传给真实 Fabric。
- 定向命令：`.venv/bin/python -m pytest -q tests/blackbox/test_cp4_load_storage.py tests/blackbox/test_cp4_load.py`；`.venv/bin/ruff check experiments/cp4_load.py tests/blackbox/test_cp4_load_storage.py --no-cache`。

浏览器脚本使用本机 Node 22、独立工具目录 `runtime/starwave-browser/node_modules/playwright-core` 的 `1.65.1` 版本，以及 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`。Playwright 没有加入前端产品依赖。先确认这些本地工具存在，再对已准备的开发服务执行下列命令；输出目录需换为未使用的新目录，保留原始验收证据：

```bash
cd /Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b
DUAL_SPACE_URL=http://127.0.0.1:15191 \
DUAL_SPACE_EVIDENCE=evidence/cp4/dual-space/local-rerun-new-directory \
node web/tests/dual-space.browser.mjs
```

先前工程失败输出保留在 `engineering/make-cp0-before-storage-adapter.txt.gz` 与 `engineering/make-cp0-before-assertion-text-fix.txt.gz`：分别记录旧表观测不兼容的 2 项失败、测试预期断言提示过时的 4 项失败；修复后重新完整运行得到上表结果。较早浏览器失败及诊断目录留在本机、未纳入本次 Git；`browser-release` 与 `actual-backend-readonly-final` 是本稿引用的最终记录，不将历史失败改写成通过。

### 截图

下列均为实际浏览器截图；前两行使用合成业务数据，最后一行使用真实独立后端的空实例。

| 数据来源 | 1440 × 900 | 390 px 窄屏 |
| --- | --- | --- |
| Fixture 总览 | [总览](../../evidence/cp4/dual-space/20261115/browser-release/overview-1440x900.png) | [总览 390 × 844](../../evidence/cp4/dual-space/20261115/browser-release/overview-390x844.png) |
| Fixture 工作台 | [工作列表](../../evidence/cp4/dual-space/20261115/browser-release/work-list-1440x900.png)、[工作表单](../../evidence/cp4/dual-space/20261115/browser-release/work-form-1440x900.png) | [工作列表](../../evidence/cp4/dual-space/20261115/browser-release/work-list-390x844.png)、[工作表单](../../evidence/cp4/dual-space/20261115/browser-release/work-form-390x844.png) |
| 实际新后端空实例 | [总览](../../evidence/cp4/dual-space/20261115/actual-backend-readonly-final/actual-backend-empty-overview-1440x900.png)、[工作列表](../../evidence/cp4/dual-space/20261115/actual-backend-readonly-final/actual-backend-empty-work-list-1440x900.png)、[设备](../../evidence/cp4/dual-space/20261115/actual-backend-readonly-final/actual-backend-empty-devices-1440x900.png) | 本轮未对实际后端另拍窄屏；响应式行为由上述 fixture 浏览器回归覆盖 |

## 6. 修改文件分组

本表列本轮 B 的实现及接线范围；A 后端架构文件以 `c3fad1b` 的变更和其交接文档为准，避免把 A 的实现归为 B UI 改动。

| 分组 | 文件 |
| --- | --- |
| 布局、路由、滚动与视觉挂载 | `web/src/App.tsx`、`web/src/main.tsx`、`web/src/dual-space.css`、`web/src/visual/starwave/StarwaveSurface.tsx` |
| 账户信任与查询生命周期 | `web/src/session.ts`、`web/src/api.ts`、`web/src/hooks.ts` |
| 总览与业务页面呈现 | `web/src/pages/Overview.tsx`、`Run.tsx`、`Resources.tsx`、`Continuity.tsx`、`Events.tsx`、`work-experience.css` |
| 业务公共呈现 | `web/src/components/CP2.tsx`、`Settlement.tsx`、`common.tsx` |
| 可选手势 | `web/src/components/SpaceGesture.tsx`、`spaceGestureMachine.ts` |
| UI 定向测试 | `web/tests/account-workspace.test.tsx`、`overview.test.tsx`、`resource-access.test.tsx`、`space-gesture.test.tsx`、`work-experience.test.tsx`、`ui.test.tsx`、`starwave.test.tsx` |
| 浏览器与截图证据 | `web/tests/dual-space.browser.mjs`、`dual-space.fixtures.mjs`，`evidence/cp4/dual-space/20261115/` |
| B 独立负载观测适配 | `experiments/cp4_load.py`、`tests/blackbox/test_cp4_load_storage.py`；另有 `test_cp4_clean_deploy.py`、`test_cp4_reliability.py` 对既有统一断言保护提示的预期文案修正，并非存储架构行为变更 |
| 交付说明 | `README.md`、本文件 |

Starwave 冻结引擎与预设未改。负载工具改用独立标准库 SQL 重建读取路径，不调用生产存储的状态加载 / 摘要实现自证；事务内读取单一快照。新 schema 3 下不再依赖 `fabric_state`，迁移后该表仅是陈旧迁移原始副本。检查 B 的 experiments / scripts / web / projections / queries / workflows 范围，没有发现其他直接依赖旧 `fabric_state` 的 B 读取点。

## 7. 未验证项

- 真实硬件触控板、Safari、系统返回手势、VoiceOver；模拟 wheel、Chromium reduced-motion 模拟及 DOM 可访问性检查均不能替代这些验证。
- 最终合并构建的真人完整业务流程，包括同账户借用与贡献、另一账户越权拒绝、收回 / 中断 / 接续 / 归还、真实结果下载和对应录屏。当前真实后端检查只读，fixture 场景不能代替这些操作。
- 新架构在既有实例上的迁移 / 回退演练，以及最终冻结构建的正式规模、延迟、可靠性、部署验收；本次未重跑正式 CP4。A 的架构开发证据保留其原始范围。
- 长历史全量列表、管理员聚合、完整导出的实际性能；本次分页视图没有证明这些操作为常数成本。

## 8. 回滚与保护现有实例

1. **关闭艺术层**：打开 `http://127.0.0.1:15191/?visual=classic#overview` 并重新导入身份。它关闭 Starwave 层，保留本次双空间业务界面；不是恢复旧界面的开关，也不是数据库回滚。
2. **仅回退界面代码**：本次 UI 单独提交，可对它执行正常 `git revert <B-UI提交号>`，保留 `d5aca86` 架构和 `455f872` 存储观测适配。另一种方式是在 `455f872` 建立独立 worktree 检查改版前界面。原已确认视觉 `96f0e60` 另有备份分支 `backup/b-cp4-glass-before-dual-space-20261115`；该旧构建须遵守下一条数据库兼容要求。不要覆盖工作目录、`reset --hard` 或清理未提交改动。
3. **回退到旧后端程序**：旧程序必须配套使用原来的、未迁移的旧 runtime / 备份及兼容的身份配置。**不得让旧程序直接打开 schema 3 的 `runtime/b-ui-v2-entity`；没有数据库静默降级路径。** 旧 `fabric_state` 不代表新写入后的当前状态，不能用它做无损恢复；新版身份中的 `member` 角色也需要与旧程序兼容性分别核对。
4. 如需迁移已有实例，按照 [实体存储回退顺序](STORAGE_ARCHITECTURE.md) 在独立备份副本上演练：停止自己明确识别的服务及 CPU worker，确认无活跃或未确认停止的执行；用 SQLite 在线备份 API，或停机后完整备份数据库、WAL 相关状态、工件、身份与配置。凭据只留本地。迁移后新增写入不会包含在旧备份里，回退时必须明确处置这些写入。

本次预览使用独立 `b-ui-v2-entity`，不需要为了检查新 UI 迁移或覆盖旧 CP 验收实例。

## 9. A 待处理与联合收口

当前只读接线及审查中没有新增的 A 接口阻塞项；不要求 A 为本次 UI 改动更换公共 API。下列是待验收事项，不是已经定位的后端缺陷：

1. A 继续按新存储验证协议完成最终候选的迁移 / 回退和正式 CP4 证据，注明冻结构建、运行环境及原始记录；既有开发测量不能直接升级为正式通过。
2. A/B 在本次最终合并构建上完成第 7 节真人业务流程并留下对应记录，最后统一更新 CP4 状态。B 本轮的 17 项 fixture 和 13 次实际 GET 仅作为 UI 与只读接线证据。

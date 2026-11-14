# CP1 审计补修交接

责任：B / xuyangma；基于双方验收版本 `355ad556d7d56460ad7002cb6dff9280ddbacaf8`。
用户已授权修复审计发现的全部问题，包括 A/B 查询授权交界。本次不实现 CP2，不修改官方 OpenAPI、共享契约或后端依赖锁。

## 最终行为

- 本地身份选择后再次点击“应用身份”，凭据匹配时保留角色提示；手工改变凭据不沿用旧角色。角色提示不授予后端权限。
- 再次查询同一工作、决定或实验 ID 会重新读取，显示加载及最近读取反馈。
- 工作详情直接链接到关联决定和该工作的事件，事件中的决定可直接打开，事件导出位于列表上方。工作范围的导出只含服务端返回的关联记录；全局时间线导出明确为当前页。
- 工作状态与租约状态并列显示，明确保留租约仍占资源；逻辑模拟说明指出需要管理员推进时间。
- 普通用户能查看多人候选计划中属于自己的决策说明；管理员保留完整原计划。
- 验收页读取实际回执，使用既有门槛核对构建、验证器与证据摘要。缺失保持 NOT_RUN，无效或过期显示 NOT_MET，只有有效的当前构建回执支持 MET。

## A/B 交界改动

内核的 `read_snapshot` 与 `export_state` 使用同一授权决策投影。候选归属按权威 allocation 及关联 demand/intent 核验，不能单凭候选上的 owner 标签获得读取权。

普通 Owner 的投影保留自己的候选身份、放置、可行性与约束种类，以及属于自己的选中候选。它隐藏其他 Owner 的候选、选中身份、全局评分、预条件引用、自由文本及私有 URI。即使预算截断后计划中仅剩一个 Owner，也需要脱敏，因为评分和说明可能来自全局输入。投影明确为部分可见，不声称覆盖完整候选域或证明全局不可行。

原持久 DecisionPlan、提交逻辑、权限规则和管理员完整视图不变。普通用户仍只能读取授权事件，无关用户仍为 404；没有为普通用户申请全局读取权限。本次投影是读取说明，不能作为新的可提交计划。

## 验证入口

```bash
make setup
make cp0
npm --prefix web run test:client
uv run python scripts/accept_cp1_a.py
uv run python scripts/accept_cp1_joint.py
uv run python tests/acceptance/cp1/cp1_capacity.py
make checkpoint CP=CP0
```

`make cp0` 已包含 `npm --prefix web run test:ui`。新增回归覆盖真实 React 组件交互、多 Owner 的实际内核/HTTP 查询和证据读取器；它们不是两位真人的操作证明。最终运行记录存放于 `evidence/local/cp1-fixes/`，以实际日志和构建标识为准。

本地补修预览使用独立实例 `cp1-fixes-b-20261114`、API 18082、Web 15175；原 18081/15174 人工验收环境不重置。

```bash
FABRIC_INSTANCE=cp1-fixes-b-20261114 FABRIC_SCHEDULING_MODE=external FABRIC_B_POLICY=B0 PORT=18082 bash web/dev-local.sh api
PORT=18082 WEB_PORT=15175 bash web/dev-local.sh web
```

## 联合验收收口

A、B 均已由 B 在对话中报告完成原版本操作；B 的原始导出、截图和本人确认已单独保留，A 的原始记录仍在 A 处。不能把原版本材料直接更改构建标识后当作补修版真人记录，也不能将自动化切换身份称为双人操作。

将双方材料汇总到 `evidence/cp1/joint/human_handoff.json` 和真实 `ui_receipt.json`，按补修影响复核身份应用、决定追溯及读取状态，再按既有验证器形成顶层 `evidence/cp1/receipt.json`。缺少材料时最终 CP1 保持未通过。回执读取器只校验和展示已登记证据，不生成通过结论、不执行场景、不推断真人参与。

注册有效 CP1 回执后，该阶段条目可显示 MET；尚未执行的 CP2–CP5 仍是 NOT_RUN，不能据此称整个项目已全部验收。

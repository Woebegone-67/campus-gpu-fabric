# B CP3 前缀与决策证据入口

在源码稳定后，从仓库根目录运行：

```sh
.venv/bin/python tests/acceptance/cp3/cp3_prefix_replay.py
.venv/bin/python tests/acceptance/cp3/cp3_decision_coverage.py
```

两者支持 `--output PATH`。默认输出为 `evidence/cp3/prefix_replay_receipt.json` 与 `evidence/cp3/decision_coverage_receipt.json`，相邻 `_histories/` 保存带 SHA-256 的原始证据。回执包含 `schema_version=1.0.0`、`check`、`evidence_kind`、`status`、`build`、`build_at_finish`、`checks[{id,status,observed}]`、`unresolved_issues` 与 `evidence`。源码身份变化会让原本 PASS 的结果降为 NOT_MET。

`cp3_prefix_replay.py` 对 B0/B1/B2/P 分别执行改变/删除未来到达、改变/删除未来收回通知、改变不可见固定工期五组攻击。比较 t=5 以前的真实计划、提交事件以及截止状态；修改已经可见的需求作为负控。另用不导入任何生产 reducer 的独立 Python fold，从冻结库存和已提交 `resource_deltas` 重建资源持有，核对非空占用前缀及最终持有。重复运行生产内核仅用于前缀比较，绝不称为独立 replay。

**CP3-PREFIX-REPLAY 仍为 NOT_MET，预期退出码 1。** 现有事件不足以重建完整工作、请求幂等记录、租约规格/TTL、任务规格、收回通知、回调和 outbox。`state_changes` 也不是完整对象 patch；部分耦合状态变化未独立记录。资源投影通过不等于完整数据库回放通过，不替代 A 的 CP3-INDEPENDENT-MODEL 验证器。完整缺口保留在回执中。

`cp3_decision_coverage.py` 为四个策略分别执行接受、候选预算 1、过期计划、非法 placement 四类实际回环 TCP 场景。测试装配不新增 HTTP 路由，HTTP 工作登记触发真实 WorkflowWorker，再由未修改的 A 内核提交/拒绝。测试策略适配器只在拒绝场景故意损坏提案。捕获真实命令/回执/前后快照，并通过产品 HTTP 查询解释、工作、容量。核对选中 placement 与提交增量、管理员原始计划、普通用户 partial 脱敏视图、另一用户 404，以及拒绝不产生成功解释/事件/占用。

该结果覆盖有限场景下的 API 与实际命令一致性，不认证浏览器渲染、全部候选空间、所有并发时序或真实 GPU 执行。正常通过时退出码 0；断言失败或源码变化则非零并保留证据。

开发验证不写正式回执：

```sh
.venv/bin/python -m pytest tests/policy/test_cp3_evidence_validators.py
```

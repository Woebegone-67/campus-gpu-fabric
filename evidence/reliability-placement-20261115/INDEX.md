# 收回权与神经分配研究证据索引

基线 `0217e45de5bac7c8217c0144d51ff6414331d2f5`；2026-11-15。
研究报告：`docs/research/RECLAIM_PLACEMENT_BREAKTHROUGH.md`。
接入说明：`docs/research/NEURAL_ALLOCATION_HANDOFF.md`。

## 数据与实验

| 证据 | 内容 | 支持与边界 |
| --- | --- | --- |
| `notification-headroom.json` | 原 140 行复算、五策略事后上限、保存耗时误差 | 解释旧 0.48% 与负结果；不换分母声称收益 |
| `../reliability-placement-20261115-run01/` | 180 案例 × 6 策略，主实验及严格控制共 2,160 行 | 单次中断损失下降 25.19%，但总体有用计算只节省 0.5996% |
| `headroom.json` | 原 2,160 行重放、8,640 项未来已知分配结果 | 单次收回模型总体改善空间有限；仅事后诊断 |
| `../reliability-renewal-20261115-run01/` | 180 案例 × 8 策略，1,440 行，反复窗口、两次恢复、900 秒 | 最佳简单对照到新匹配，限制均值下降 9.92%，完成数 625→637 /720 |
| `../neural-availability-20261115-run01/` | 一次固定 MLP 训练、64 案例 × 5 策略，320 行 | 神经分配比时段历史统计差 0.84%；未证明神经收益，不调参补救 |
| `neural-headroom.json` | 320 行重放、1,536 个事后分配；真实生成概率下的成本遗憾 | 总体似然更准不保证关键任务排名更好；未重训、未改策略 |
| `../neural-kernel-replay-20261115-run02/` | 固定案例的真实训练权重→隔离 SQLite，四次提交 | 改变实际选卡；1 秒收回、原租约到期、旧版本拒绝都生效；不是性能实验 |
| `../neural-kernel-replay-20261115-run01/` | 同案例初次回放的 tuple/list 参数适配失败 | 保留失败；只改新增回放脚本，不改封存研究 |
| `../neural-worker-demo-20261115-run01/` | 真实训练模型、资料适配器、实际 WorkflowWorker 的独立运行 | 四次真实提交，两项改变选卡；1 秒收回及原租约到期生效，非自动线上学习 |

前三轮研究目录中的 `freeze.json` 固定运行前的协议、输入和源文件哈希；`result-manifest.json` 固定结果哈希。神经模型 SHA-256 为 `c7830f8062fb5f700aa73fa2d333bdbdc6ec70413bbe653ed92c366ac12afbeb`。所有正式对照均为合成数据，所有反向案例、个体损害和未完成任务保留。

冻结验证与当前专项测试的最后结果写入本目录 `final-verification.json`；它是专项记录，不是 CP0/联合验收通过凭据。

## 已发现的全仓库测试问题

`make cp0` 实际执行结果：**104 failed、1,186 passed、78 errors**，不能标绿。其后清洁进程隔离排查（原始本地日志被仓库 `*.log` 规则忽略）：

1. `test_historical_report_configuration_survives_environment_cleanup` 未恢复 `FABRIC_ACCOUNTS_FILE`，让后续测试引用已删除的临时文件。单独领域测试 24 项通过，按原顺序两项联跑在本轮和干净 `git archive 0217e45` 中同样失败。见 `triage-baseline-results.json`。
2. 两项 CP2 CPU/session 测试期待在 2 秒通知内形成保存点并 Preempted；基线的实际规划上界已有 2 秒停止成本、0.5 秒保存及安全余量，判定来不及保存并 Evicted。相同 Python、Node、隔离端口的当前与干净基线均复现。见 `triage-owned-results.json`、`triage-recall-window.json`。本轮不擅改 A 的执行/收回约束来满足旧预期。
3. AccountSession 序列化现在包含 `username: null`，旧测试期望没有这个字段。干净基线单独复现，契约与该测试本轮均未修改。见 `account-session-baseline.json`。

排除两个已定位的 CP2 黑盒文件后，另一个历史回归进程报告 **1,405 passed、3 failed**；其中一项是上述 AccountSession，另两项是新研究在并行开发阶段缺失 `renewal_protocol.json` 的检查。这两项随后补齐协议并通过专项重跑；不将那次旧回归记录改写为全通过。另三项 session 测试及 CPU preview 测试分别在清洁进程中通过，见 `remaining-regression-results.json`。

前端 156 项与构建在本轮早期通过；后续用户追加了任意非空密码登录的修改，相关最终检查另记于 `final-verification.json`，不把这些检查称为真实 GPU 性能证据。

## 采用与回滚

研究策略保持显式选项，默认 P 路径在未配置模型时保持原样。没有改动运行中的服务、原预览目录或产品数据库。神经权重未推荐自动上线；尚缺真实连续历史及一般库存/老化窗口验证。

移除 `FABRIC_AVAILABILITY_MODEL_PATH`、`FABRIC_AVAILABILITY_MODEL_SHA256` 与可选输入 registry 的三项环境配置，重启自己的测试实例即可恢复原策略。代码回滚使用本轮独立提交的 `git revert`，不 reset 其他工作分支；保留实验和失败证据。

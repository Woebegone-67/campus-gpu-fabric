# CP2 隔离实验样例

`python -m experiments.run_scenarios --scenario cp2-recall-v1 --policy P --seed 0`

可选场景：`cp2-contention-v1`、`cp2-recall-v1`、`cp2-negative-control-v1`、`cp2-node-loss-v1`。策略为 B0/B1/B2/P；seed 为 0–9999。每个样例最多 8 项工作、40 逻辑秒，同样的 512 候选预算、10 秒固定工作量模型和恢复规则。命令默认写入 `evidence/local/cp2-experiments/experiment-<uuid>/`，`--output` 仅由本地操作者指定。

产品端管理员只能提交登记的场景、策略和 seed，不能传入自定义命令、路径、数据库、节点或故障请求。后台最多 4 项排队/执行、单线程执行，每个实例最多保留 128 个 run。每项 run 使用独立临时 SQLite Kernel；没有运营实例对象、远程 API 或测试密钥。结束后移除临时数据库，保存实际输入、事件、决策、回执、最终授权快照、模型、构建及环境元信息。服务目录单进程加锁；重启发现未完成记录时记 failed，不恢复成成功。

这些是实际内核执行的逻辑模型研发样例，**不是实际 GPU/CPU 运行证据、CP2 联合验收通过或 CP3 五族五 seed 效果结论**。`EvidenceRun.checks` 不填 MET。运行器执行完成可以包含失败工作和未完成工作；runner 自身异常/构建变化会令 run failed，并保留已有输出。记录中的 `memory_mib=0` 表示未测量，元信息明确保留这个限制。

每项 run 的可下载文件：

- `input`：完整公共外生输入、固定模型、恢复规则、观察窗口。
- `summary`：完整 WorkIntent 队列、到达/完成/失败/未完成、输入摘要，兼容 `experiments.compare_runs`。
- `events`、`decisions`、`receipts`、`final-snapshot`：实际执行记录；决策附有其当时 snapshot 与测得的计算时间。
- `run-metadata`：构建、预算、运行器异常及范围说明。

`evidence.json` 为类型化 EvidenceRun 清单；输入/输出有 SHA-256，下载时再次校验。不改变历史 run 的构建，也不把旧 build 的完成记录当作新 build 的验收。先前失败样例不得删除后当作从未发生。

## 策略 P 的当前定义

P 与 B1/B2 使用相同 Owner 当前占用份额、优先级、到达和 ID 准入顺序，使用 B2 枚举出的相同有界候选。对同一被准入工作，优先减少其他已知 Owner 的已记录合法放置备选被当前动作消耗的比例，再使用 B2 的兼容稀缺性和 best-fit 同分规则。按 Owner 和需求形状平衡，避免同类请求复制直接放大权重。

这是有界已知候选机会代理，未看未来到达、未来回收、隐藏工期，也不声称完整不可行证明。`CandidatePolicy(opportunity_enabled=False)` 关闭机会组件后回到 B2 的动作。恢复成本组件目前 **inactive**：Snapshot 没有核验过的恢复成本模型，不能捏造该组件或消融收益。四策略共享保存/接续规则，A 内核仍校验期限、停止状态、工件和两种预算；B 不绕过恢复资格。

## 配对比较

将两个相同场景/seed 的 `summary.json` 交给：

`python -m experiments.compare_runs <B2-summary.json> <P-summary.json> --output <comparison.json>`

完整队列保留失败和未完成，服务单位为 `completed_work_equivalents`：只对成功完成的一项逻辑工作计 1，部分处理不冒充有效服务。计算器仅核对提供记录的配对字段，不替代运行真实性验证；先检查两个 EvidenceRun 状态、build、实际观察窗口及摘要。负结果、零分母和个体损害必须保留。

# 神经可用性预测与任务条件分配：B 线接线说明

开发基线：`0217e45de5bac7c8217c0144d51ff6414331d2f5`。调度部分不修改 A 权威账本、贡献者收回权或现有执行器。旧研究与新神经研究分别封存；另按用户要求简化账号登录。

本版交付目标已经收敛为核心算法和可运行接线，不继续调参或扩展性能实验。现有训练集只有 896 条合成样本，数据规模和代表性不足是重要限制、也是负面结果的可能原因；未经数据规模消融，不能认定它是主要原因。神经分配的额外性能收益仍未证实，不妨碍先交付版本化训练、排序与实际分配通路。

## 已实现的链路

```text
过去已经可见的供给记录 + 公开时间上下文
    → 因果特征提取 / 截止时间分割
    → 离线训练 8→16→8 MLP，输出带 SHA-256 的 JSON 权重
    → 每张 GPU 的可用窗口概率分布
    → 结合任务工期、保存成本、恢复预算计算任务×GPU 成本矩阵
    → 同一 B2 准入队列内的有界联合分配
    → 一个已有合法 CandidateAction / DecisionPlan
    → A 内核重新校验版本、权限、资源并提交
    → 实际收到收回通知时，仍由现有自适应保存/停止/恢复机制执行
```

这是一张**随任务变化的排序表**。例如一个短任务在两张卡上的预计完成成本都为 10 秒，而一个无法及时保存的长任务在稳定卡上为 30 秒、碎片卡上可能耗尽恢复预算。联合分配把稳定卡留给后者；不能先按“显卡风险分”排一个全局榜，再把最稳定的卡永远发给先到的人。

`DecisionPlan.candidates[].scores` 中的 `learned_budgeted_expected_restricted_seconds` 保留每个合法任务/设备候选的预测成本，可用于以后展示排序。数值小表示模型预计更有利，不是实测 GPU 速度，也不是贡献者的可用性承诺。联合分配选中的卡可能不是该任务单独排名第一的卡，因为还要考虑同批其他工作。

## 训练与可复现性

先运行最小完整产品链路（约数秒，不重复训练或调参）：

```bash
.venv/bin/python -m experiments.research.neural_worker_demo \
  --study-dir evidence/neural-availability-20261115-run01 \
  --output runtime/my-neural-worker-demo
```

输出目录必须不存在。程序使用真正训练权重、审核输入适配器和实际 `WorkflowWorker`，在独立 SQLite 中提交四项工作，输出逐任务 GPU 排名、相对默认 P 的分配变化及真实收回回执。已有运行回执位于 `evidence/neural-worker-demo-20261115-run01/RECEIPT.md`：四次提交通过，两项任务改变选卡；真实 1 秒收回及原 900 秒租约期限均生效。这是可运行算法原型，普通界面新增任意任务尚不会自动生成历史资料。

`policies/availability_model.py` 实现真实反向传播、固定超参数的小型神经网络，没有新增 NumPy/PyTorch 运行依赖。输入包含日内/周内周期、最近供给窗口的长度分布和短窗口比例；不以原始贡献者 ID 记忆设备，也不读隐藏的下一次收回时间。

模型输出 8 个**离散代表时长**：8、16、32、64、128、256、512、900 秒。当前训练接口的精确标签应属于这些代表值；它不是已经完成连续时长校准的生存模型。右删失标签使用尾部概率损失，不能把“观察结束了”当成“贡献者收回了”。任意连续实测时长的区间离散/删失方案需要在真实轨迹协议中另行固定。

离线训练入口：

```bash
.venv/bin/python -m experiments.research.train_availability_model \
  --observations /absolute/path/observations.json \
  --cutoff 1000000 \
  --output evidence/models/unique-run
```

输入 JSON 为 `{ "source_kind": "observed_export" 或 "synthetic", "namespace": "时钟与数据命名空间", "rows": [...] }`。每行包含 `features`、`observed_at`、`label_observed_at`，以及二选一的 `label_bin` 或 `censored_after_seconds`。特征字段/边界以 `FEATURE_NAMES` 为准。只纳入标签已在 cutoff 前可见的记录；输出目录必须不存在，历史模型不覆盖。新记录到来后重新训练一个新版本，经验证再显式切换权重，当前版本不会偷偷在线自改。

`historical_features` 接收完整供给区间的 `started_at / ended_at / available_at / duration_seconds`，剔除当前不可见的记录，至少 4 条、最多最近 32 条。供给区间是贡献者提供资源的窗口，**不是使用者任务的运行时长，也不是租约 TTL**。真实采集还必须统一时间域、时区、曝光口径与删失机制。

## 接入当前策略工厂

默认策略保持原样。只有选用 P 且同时提供下面两个配置，工厂才构造 `LearnedAvailabilityPolicy`：

```text
FABRIC_AVAILABILITY_MODEL_PATH=/absolute/path/model.json
FABRIC_AVAILABILITY_MODEL_SHA256=<该模型的完整 SHA-256>
```

现有 B `devhost` 的调度 worker 还要求 `FABRIC_SCHEDULING_MODE=external`、`FABRIC_B_POLICY=P`。仅给内建调度器配置权重不会切到 B 策略。四项配置也不能代替资料输入；本轮未修改已有运行实例的配置。

只设置其中一个会明确报配置错误；模型缺失、哈希不符、畸形、未来训练截止、输入缺失/过期/越界会回退到历史匹配或 B2，并在决策理由中写出原因。“越界检查”仅是特征契约检查，不能声称已经可靠检测所有行为漂移。默认服务本轮没有开启此配置。

## A 需要接出的真实事实

本轮已测试神经预测确实改变合法分配并能提交到真实隔离 SQLite 内核。**当前 A 的 `Kernel._decision_snapshot` 尚未生产以下资料，因此仅设置模型路径不会让现有服务凭空开始学习，通常会安全回退。** 这是进入产品联调的明确接线点。

| 快照位置 | 字段和来源 |
|---|---|
| `history`，source_id=physical_gpu_id | 至少 4 条截至当前可见的完整 `availability_window_seconds`，source 为 history/observed_event；不能提前暴露真实未来 |
| `metadata`，source_id=physical_gpu_id | 8 个模型特征，source 为 history/observed_event/public_metadata；必须由可信生产链形成，不能直接相信客户端自报“稳定” |
| 同上 | `notice_seconds`、`owner_use_seconds`：可审计历史或明确声明，不能是评估器隐藏未来 |
| 同上 | `availability_window_age_seconds=0`：当前模型只支持刚开放的窗口，必须在本快照 now 同刻观测；已经老化的窗口回退，不能把完整窗口误当剩余时间 |
| `demands[].known_data`，source_id=demand_id | `estimated_compute_seconds`、`checkpoint_seconds` 来自 declared_profile/history；可选 restart_seconds/stop_seconds；工期不得取 TTL/timeout 代替 |
| 同上 | `remaining_recoveries` 来自 public_metadata/observed_event，对应 A 的真实剩余预算，当前支持 0–2 |
| 同上 | `execution_window_seconds` 来自 public_metadata/observed_event，对应剩余租约与 timeout 的较小值，当前策略要求至少 900 秒；这是有界研究适配范围，不是假定默认租约就是 900 |

所有事实满足 observed_at ≤ available_at ≤ now；模型 trained_through 与 now 必须属于同一时钟/数据命名空间。日历特征需要明确现实时间与时区映射，不能把从零开始的逻辑秒直接当成真实星期几。reset/切数据域后应重新绑定对应工件。

当前分配范围是最多 6 项工作、独立节点上的单张完整 GPU；共享节点资源、多卡/MIG、候选截断、资料不足等情况保持 B2 回退。这些限制不会被预测分数越过。

## 已接入的 B 侧资料适配器

`workflows/availability_inputs.py` 的 `AvailabilityInputProducer` 已接到 `WorkflowWorker.tick` 的快照读取与 `policy.plan` 之间。它读取服务端审核并固定哈希的历史导出，用 `historical_features` 生成特征，从权威全局读取计算恢复预算及有效期；不创建第二份业务账本。

服务端额外配置三项：

```text
FABRIC_AVAILABILITY_INPUTS_PATH=/absolute/path/reviewed-inputs.json
FABRIC_AVAILABILITY_INPUTS_SHA256=<该输入文件的完整 SHA-256>
FABRIC_AVAILABILITY_NAMESPACE=<审核输入对应的实例命名空间>
```

输入结构以该模块的严格 schema 为准，包含模型哈希、epoch、执行模式、日历基准、每张卡的贡献者与完整窗口、当前窗口起点，以及绑定账号、工作 ID 和完整 specification 哈希的声明 profile。最近 24 小时内至少四个完整窗口才增强；任何设备资料缺失、时间/所有权不符或两个读取版本不一致，均返回原快照并记录诊断。哈希证明文件未变，不证明其历史事实真实。

目前只增强**初次分配且刚开放的窗口**。恢复任务继续走既有机制；没有与执行代次绑定的剩余工期资料时，不会把初始总工期冒充剩余工期。该适配器明确使用 `logical_day0_monday` 日历，不能直接接未经映射的现实秒或不同时间域模型。

## 自动采集的接线缺口及责任边界

B 已提供上述只读特征增强和固定历史输入路径，但不能捏造 A 没有提供的事实。完整自动学习仍需要：

1. **A 提供权威原始事实**：任务明确声明的工期/保存 profile、当前剩余恢复预算和有效期，以及有可见时间与覆盖信息的贡献者供给/收回历史。已有 DecisionSnapshot.notices 可观测当前通知，不能据此断言断线期间没有漏掉收回。
2. **B 从事实生成学习资料**：相邻且完整的收回通知可以形成从上次 unavailable_until 到下次 issued_at 的开放窗口；重叠收回、离线和缺记录区间必须合并或标为未知/删失。使用者释放租约或 GPU 空闲不是贡献者供给行为的替代标签。当前离线训练入口接收审核过的导出，尚未部署自动采集与周期重训。
3. **B 扩展模型支持后再联调**：现有默认库存含多卡、MIG 和 CPU 节点；冻结适配器会回退。已有窗口的剩余寿命也不是刚开放窗口的总寿命，不能把 age 改成 0 强行启用。应在独立协议下扩展这些范围，同时统一产品 clock_origin、模型日历和数据命名空间。

现有 ReadPort 不能重建完整供给历史：决策快照只保留活跃收回通知，`recall.ended` 没有 GPU 引用，空闲卡收回也可能没有租约事件。A 应提供设备、贡献者、epoch、起止时间与事实可见时间齐全的供给生命周期流。这些是明确未完成的自动采集项。不能把“实际 worker 已能读审核输入并分配”写成“当前界面已经在线自学习真实用户习惯”。

## 本次模型的采用结论

正式研究的模型位于 `evidence/neural-availability-20261115-run01/model.json`，SHA-256 为 `c7830f8062fb5f700aa73fa2d333bdbdc6ec70413bbe653ed92c366ac12afbeb`。它是一次冻结训练生成的**合成研究权重**，不是产品推荐默认。神经联合分配相对同一时段经验分布的完成指标恶化约 0.84%，未证明额外收益；完整结果和负面组见研究报告。版本化训练和任务条件排名通路保留，生产自动启用状态为 `NOT_ENABLED`。

## 验收和回滚

- 独立预测验收：训练/验证/测试按时间分离，检查 NLL、Brier 与删失数；当前只有合成数据证据。
- 分配验收：神经与简单历史预测共用同一成本矩阵变换、同一准入队列和执行器；另比较同一神经成本的贪心与联合匹配，保留预测失准组与受损任务。
- 权威验收：模型改变选卡，A 正常提交、拒绝旧版本；模型预测稳定也不阻止贡献者按实际更短期限收回；缺模型/缺数据时回退。
- 回滚：移除两个模型配置及三个输入配置并重启自己的测试服务，即恢复原 P 工厂路径；没有数据库迁移。已生成的模型与实验目录保留，不覆盖证据。回滚代码可撤销本轮独立提交，不重置其他人的分支。

尚未验证：真实贡献者历史训练、真实 GPU 性能、真实流量下的收益、连续时长校准、老化窗口条件预测、完整生产快照接线、漂移后的自动再训练和上线策略。研究报告必须保持这些边界。

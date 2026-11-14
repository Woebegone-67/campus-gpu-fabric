# 语义与实现边界

当前状态：**CP0 冻结的团队约定已扩展至 CP2 A 实现；回收、恢复、两预算和真实 CPU reference 已接入，联合 CP2 仍待验收。** 机器可读唯一表为 [semantics.json](../configs/semantics.json)，按 `id` 引用，不在各模块复制默认值。

`source=official` 仅表示原规范已写明；`team_provisional` 是本团队为可执行开发作出的暂定选择。官方文件保持原样。每条含 `value / reason / operations / validation / replace_at`；收到正式答疑时由集成负责人改配置、适配器与对应回归，记录语义版本并重新跑受影响验收。

优先读这些条目：

| 规则 | 开发决定 |
|---|---|
| lease_time | 首次获配起 TTL；初次等待不耗租约；被抢占仍计时；恢复不重置截止；恰好截止先到期 |
| job_binding | 每租约一个活跃 job；完成/取消 job 不退租；通知期拒绝新 job |
| recall_windows / preemption_policy | 主人收回优先；重复通知取阻断区间并集；failed/timeout 不假装保存成功 |
| budgets | 新执行代次真正提交时计数；等待、HTTP 重试、旧计划均不扣预算；两种预算分账 |
| same_time_order | 到期→回收/保存截止→job超时→执行回调→收回结束→重试→调度；序号同分 |
| clock_modes | logical_simulation 与 cpu_reference 使用独立实例和同一逻辑内核；CPU 停止未确认先隔离 |
| work_intent_atomicity | allocation/job 创建、绑定、阶段回执在同一事务；已保存阶段回执先于版本冲突检查 |
| information_boundary | 策略只读当时快照；未来到达/通知、隐藏工期、评估器状态不能进入策略 |

原规范待澄清处：初始化/reset场景；TTL、并用与预算细则；fault 的 string 类型与数字 enum；X-Owner 写在扩展说明但未逐路由挂接。兼容适配不能反写原件。`timeout_seconds` 是终止上限，绝不当作工期；`GPURequest.profile` 是硬资源约束，区别于可选性能 profile。

验收至少覆盖每条 `validation`，保存输入与实际回执。配置能解析、文档存在、schema能生成，只说明 CP0 材料可用，不说明 HTTP、并发、执行恢复或产品通过。

CP1 的显式逻辑执行模型见 `cp1_execution_model`。首次初始化的库存及调度模式都是实例配置；reset 不恢复演示库存。CP2 已实际区分回收后的 restart 与执行失败的 retry；计数由内核新代次提交产生，不能从一次正常成功运行推断预算边界通过。

CP2 准确实现与验证入口见 [交付记录](CP2_A_DELIVERY.md) 和 [接线说明](handoff/CP2_A_TO_B.md)。逻辑模式的保存只保留模型状态；CPU reference 使用真实进程与本地核验工件。公开 ready、verified 工件、applied 接续分别记录。CPU 单调时间锚点与逻辑推进同事务保存，同一 boot 内计入服务停机时间并先处理到期；boot identity 改变时拒绝自动继续，跨主机重启恢复留待 CP3。

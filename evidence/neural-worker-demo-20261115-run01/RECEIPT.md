# 神经模型接入实际 WorkflowWorker：本地运行回执

结果：**PASS**。固定案例 predictable / index 0。

真实已训练模型 → 固定 SHA 外部历史 registry → AvailabilityInputProducer → P 合法计划 → WorkflowWorker → A SQLite commit。

| 工作 | 默认 P GPU | 神经 P GPU | 分配改变 |
| --- | --- | --- | --- |
| task-0 | provider-00 | provider-01 | True |
| task-1 | provider-01 | provider-00 | True |
| task-2 | provider-02 | provider-02 | False |
| task-3 | provider-03 | provider-03 | False |

模型 SHA：`c7830f8062fb5f700aa73fa2d333bdbdc6ec70413bbe653ed92c366ac12afbeb`。
输入 registry SHA：`3833fb1e529fec65886d41569def44cecb32686d33e8c5c04a8f534016dd9e12`。

四项工作由实际 worker 建立租约和运行任务，GPU 分配互斥。贡献者随后发出 1 秒收回通知，A 在该期限中断对应租约并移除占用，预测中的 12 秒通知期没有延长真实权利。所有租约仍在原始 900 秒期限到期。

实际输入从固定案例原历史中筛出24小时内的完整窗口，保留原始时间戳。排名使用 producer 实际输出，未读取隐藏未来日历。完整排名见 task-gpu-rankings.json。

本次仅证明真实本地 worker 接线可运行及模型改变了分配，不证明性能提升。历史来自经审核的合成案例，不宣称自动采集线上数据；未启动真实 GPU 任务，未修改生产服务。

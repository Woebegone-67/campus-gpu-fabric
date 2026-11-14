# 权威状态、资源占用与执行代次

本页描述完整状态设计。CP1 已实现正常分配、执行、租约和事务；PreemptRequested/Preempted/Evicted、真实执行隔离与恢复仍属 CP2 待实现范围。A 写唯一账本；B 只消费带 `VersionStamp(epoch,revision)` 的不可变快照。状态、占用差量、事件与幂等/阶段回执同一 SQLite 事务提交；外部执行通过持久 outbox 驱动，回调核对 epoch、attempt_id、generation。

| allocation 状态 | 占用 | 合法后续/触发 |
|---|---|---|
| Pending | 无 | 合法计划获配→Allocated；释放→Released；恢复等待的旧租约到期→Expired；明确拒绝→Rejected |
| Allocated | 全部请求资源 | 接受 job→Running；通知→PreemptRequested；释放/到期/节点失联→对应终态 |
| Running | 全部请求资源 | job终止→Allocated；通知→PreemptRequested；释放/到期/失联→对应终态 |
| PreemptRequested | 确认停止前仍持有 | 保存报告或截止触发停止；停止确认后→Preempted；严格checkpoint失败→Evicted；租约到期→Expired |
| Preempted | 无可再承诺的旧占用 | resume有资源→Allocated/Running；无资源→Pending；到期→Expired；释放→Released |
| Released / Expired / Evicted / Rejected | 无客户持有 | 终态，不由迟到回调复活；实际进程未确认停止的资源转入隔离账，不归入可分配库存 |

通知可针对 Allocated 或 Running；deadline 前也可提前归还。公开枚举不新增 `Releasing`；停止未确认是内部执行/资源状态，通过回执显示。模拟停止可以在同事务确认；真实CPU必须收到实际停止证据。非法转移拒绝并记录原因，不改占用。

job保持同一ID，契约状态为 Pending→Running→Succeeded/Failed/Cancelled；保存进入 Checkpointing，中断进入 Preempted，再以新 attempt/generation 接续。一个allocation最多一个非终态job；绑定WorkIntent的allocation只允许其原子job阶段创建工作，防止旁路逃逸结算；job终态后租约仍存在。WorkIntent串联首次到达、allocation、job、attempt和结算，job不能替换整体等待的计时起点。

必须维护的不变量：

- I1 守恒：每节点每资源 `held + unavailable + free = configured_capacity`，三者互斥且非负；unavailable包括离线、收回阻断和隔离，不能与held重复计算。恢复/释放按唯一claim归还一次。
- I2 形状：CPU/内存/存储与GPU一起原子扣还；GPU型号、单卡显存、profile和标签全部满足；MIG父整卡与子切片互斥，切片总量不超物理容量。
- I3 权限：actor来自服务端鉴权；owner不能从请求体越权；列表、事件、metrics、工件同样隔离。
- I4 事务：成功状态必有同版本事件和回执；失效计划不扣账；同幂等键与同工作阶段不产生第二效果。
- I5 代次：终态、reset和新attempt不能被旧消息修改；真实旧进程未确认停止时不能启动冲突的新执行。
- I6 时间/预算：按所有到期边界推进；通知、expiry及两种预算均遵守语义表；ready报告、工件核验、实际恢复是三个事实。
- I7 信息：策略输入只到当前逻辑时刻；修改未出现输入不能改变共同前缀；读投影不能反向改权威状态。

A用独立小参考模型和并发历史验证 I1–I6；B用跨Owner查询、前缀对照和实际decision解释验证 I3/I7。`make checkpoint CP=CP1` 起各项必须附实际历史；CP1 A 已有独立内核和 HTTP 验证，见 docs/handoff/CP1_A_TO_B.md；联合 UI、故障与真实执行不可沿用此结论。

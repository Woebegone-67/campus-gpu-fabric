# 资源局部校验独立复核

基线：`b19acae7f0ea185e6289388fb629d2a15dd84fad`，分支工作树 `resource-locality`。本文件仅记录本轮局部资源校验的独立定向核验；SQLite FIFO 准入由另一个测试文件单独验证。本报告不替代 CP4 正式规模负载、完整回归或真人验收。

## 验证前冻结的风险

1. 命令名称不能证明资源未变。`allocation.renew` 本身只改截止时间，但命令末尾的内置调度仍可能授予其他 Pending 请求；取消 job 也可能因 `release_on_terminal` 释放租约并授予下一项。
2. 必须比较实体修改前后的有效 placement。只看“被读取过的对象”或顶层赋值会漏掉嵌套 `device_ids/resources/node_id` 改动、删除、从持有资源状态进入终态，以及旧、新节点的并集。
3. GPU 资源独立性由物理父卡决定，不能只按 device ID 排重。整个 GPU 与任意 MIG-like 子片互斥；同父卡不同子片允许并行，但显存不能重复累计。
4. CPU stop 请求不等于真实进程退出。未确认期间仍占资源；reset 后的 retired 屏障需要覆盖另一 node ID 下重新登记的同一物理父卡，不能仅匹配旧节点 ID。
5. 节点、设备可用状态与 retired 屏障变化可能影响任意资源视图；此时局部推断证据不足，应保留全量校验。
6. 校验失败必须回滚修改、事件、版本、日志和资源变化；拒绝回执不能携带半提交的续租或新 grant。
7. 惰性 first-fit 必须保持旧节点排序及逐候选合法性检查，且新授予立即影响同次调度的后续选择；FIFO 写准入不改变 SQLite 跨进程事务权威。

## 方法与边界

- 固定夹具 [旧版本 oracle](../../tests/concurrency/_resource_locality_oracle.py) 保存基线 `placement.py` 原文及 retired 屏障的全量投影，不依赖当前 `iter_inventory` 或 `_inventory_nodes`。对独立构造的普通字典应用相同状态变化；新 `_validate_resources(lazy_state)` 使用未被 oracle 预先遍历的事务懒对象，比较接受/拒绝结果及提交后的完整 JSON。夹具随源码提交，浅克隆和源码归档运行时不需要 `git show`，也不会因缺旧 Git 对象而跳过关键覆盖。
- 另按真实持有 placement 独立求和 CPU/内存/临时存储，检查 device 唯一性、GPU 父子冲突及物理显存上界，不调用生产 placement 校验器来定义这些守恒条件。
- 命令路径使用少量可判别交错：续租末尾 grant、取消自动结算、节点状态变更和越权拒绝。人为暂停一次调度只用于建立“资源已空闲但请求仍 Pending”的合法状态，验证后续命令的既有调度语义，不声称该暂停是产品入口。
- CPU 项使用实际参考子进程和真实 driver stop 观察，GPU 库存仍为模型。它验证真实 CPU 生命周期对模型资源屏障的影响，不声称执行了真实 GPU 工作。
- 本复核只新增 [独立场景](../../tests/concurrency/test_resource_locality_adversarial.py)、固定 oracle 夹具和本报告；不复制既有全部组合、不运行 125 轮实验或完整测试套件。契约 DTO 形状仍复用项目契约；资源折叠和 first-fit 参考逻辑为冻结旧原文。

冻结来源摘要：

| 基线文件 | 原文 SHA256 |
| --- | --- |
| `src/campus_fabric/domain/placement.py` | `022f683fe83a66fcbd5aa104ccbd1cb88920613deff048538fb9998b06b1f4df` |
| `src/campus_fabric/domain/lifecycle.py` | `d7fe01054acaeba8276814064338000d9160dbd2ac63a9e142a11e61245b6f32` |

## 实际执行

执行日期：2026-11-15（Asia/Shanghai）。固定夹具版本最后一次执行：

```text
uv run ruff check tests/concurrency/test_resource_locality_adversarial.py tests/concurrency/_resource_locality_oracle.py
All checks passed!

uv run pytest tests/concurrency/test_resource_locality_adversarial.py -q
13 passed in 0.66s
```

| 范围 | 判别点 | 实测结果 |
| --- | --- | --- |
| 续租后的末尾调度，2 项 | 有效新 grant 正常提交；故意使新 grant CPU 超配时，续租截止时间、事件、资源与 journal 全部回滚 | 通过 |
| 取消自动结算，1 项 | `release_on_terminal` 取消释放原租约，同提交将卡授予 Bob 的等待请求，无重复占用 | 通过 |
| lazy / 旧 full 差分，7 项 | placement 跨节点原地移动、删除 allocation、释放状态；嵌套 CPU 超配、重复 slice、whole/slice 同父卡重叠、全局节点容量缩小 | 接受/拒绝结果一致；拒绝不留提交 |
| 节点全局变化与越权，1 项 | NotReady 逐出、Ready 重新授予等待请求；Alice 释放 Bob 租约被拒，完整状态与 journal 不变 | 通过 |
| 混合 GPU 首适应轨迹，1 项 | 未排序输入节点、whole 与 half 交错、资源不足等待、释放后重新授予，与固定旧 first-fit 逐步一致 | 通过 |
| 真实 CPU 屏障，1 项 | 实际子进程 running 后申请 release，未 driver 确认仍占用；reset 保留 retired；相同物理父卡改 node ID 重登记仍被阻断；真实 stop 确认后才授予新请求 | 通过 |

未发现本轮实现缺陷。初版 oracle 曾直接使用新的 full 包装器；复核发现它与局部路径共享新 iterator 后，已改用固定旧源码夹具，再执行上述全部 13 项。报告以固定夹具的最后一次结果为准。

不据此宣称吞吐或延迟门槛通过，不覆盖真实 GPU 执行，也不替代未在本文件执行的 FIFO 公平性/跨进程竞争、长时间可靠性、正式规模负载和双人 UI 验收。底层状态变异差分属于资源校验层测试，其中直接移位/删除的 fixture 不属于对外业务写入口。

执行结束后记录的文件摘要（2026-11-14 18:36:09 UTC）；后续实现如有变化，应由集成者核对并重跑受影响项：

| 文件 | SHA256 |
| --- | --- |
| `src/campus_fabric/domain/kernel.py` | `1f61d234ad6c413daf57060b7a47298669d7f770c799681f751b996acdcc4645` |
| `src/campus_fabric/domain/lifecycle.py` | `96875ff360ee4a57010c80d107e3a4fcec4aa11866cb2a0eb1ef35aada38a7bb` |
| `src/campus_fabric/domain/placement.py` | `0b5bd966462162c474b17d132c132a09def26638049860911086d6e6cee892ed` |
| `src/campus_fabric/domain/state_views.py` | `13dce9d9e33653894a0483f6868d8f1602f30dbbc12f0a97bfa1c8a26d8122eb` |
| `src/campus_fabric/storage/state.py` | `f11e6614cdaea2e113f484f32b9583728da834898a65d1040cfad9a63ab472f7` |
| `src/campus_fabric/storage/sqlite.py` | `2cc2232c86dab5a8c58caf44e379cc85ed462a16905a200b1626dd770aeb44b3` |
| `tests/concurrency/test_resource_locality_adversarial.py` | `28773417389ee85521bc805b03a2954106bf15da946cf58d82daf482f069ee7c` |
| `tests/concurrency/_resource_locality_oracle.py` | `4270becf473859624ac544ec5e99d187205651f21493bd54ed931a202fa1f26d` |

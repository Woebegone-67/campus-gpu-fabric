# CP4 规模与 HTTP 延迟诊断

运行器在独立临时目录启动真实 loopback HTTP 进程，使用 A 权威内核和 B 查询投影。子进程的 `PYTHONPATH` 固定为当前树的 `src`、仓库根目录和 `scripts`，覆盖旧 shell 中其他 checkout 的路径。它不接触现有人工验收实例，不更改 A 的账本、契约或部署设置。停止时仅结束自己创建的进程。

## 执行

源码稳定后运行，输出目录必须不存在：

```sh
uv run python -m experiments.cp4_load --profile smoke --output evidence/cp4/load-smoke
uv run python -m experiments.cp4_load --profile pilot --max-wall-seconds 120 --output evidence/cp4/load-pilot
uv run python -m experiments.cp4_load --profile full --max-wall-seconds 600 --output evidence/cp4/load-full
```

| profile | 节点 | 物理 GPU | Owner | 累积 jobs | 成功命令目标 | 并发生命周期上限 |
|---|---:|---:|---:|---:|---:|---:|
| smoke | 2 | 8 | 4 | 8 | 40 | 4 |
| pilot | 128 | 1024 | 64 | 64 | 320 | 32 |
| full | 128 | 1024 | 64 | 10000 | 50000 | 32 |

每个物理 GPU 同时列出整卡和两个 MIG-like 切片表示；总量只能计父卡一次。本负载只申请整卡，不能证明切片分配正确性。每个生命周期固定执行 5 个变更：申请租约、创建任务、取消任务、续租、释放租约；另查询任务和释放后的租约。Owner 按 job 序号取模分配，确定性 workload seed=0。10000 是累计创建任务数，不是同时运行任务数。模拟时钟不推进，不执行真实 CPU/GPU 命令；不把模拟任务数称作真实算力吞吐。

普通查询延迟只覆盖 B devhost 上的两个详情接口：`GET /v1/jobs/{id}` 和 `GET /v1/allocations/{id}`。容量接口只在初始化/收尾核验，不算普通查询；本负载不覆盖 B 的工作意图、事件、决策、管理员总览和实验查询性能。

`--max-wall-seconds` 限制负载阶段的后续请求与生命周期准入，超时后保留部分结果；正在进行的 HTTP 请求可在 `--request-timeout` 内完成。清理释放请求单独计为 cleanup，不补入50000命令数，也不能把失败运行补成通过。初始化、最终核验和清理耗时另包括在总耗时中。默认请求超时15秒，最大120秒。出现意外HTTP状态、异常或不变量破坏即停止新增负载。

## 记录与核验

- `requests.jsonl` 逐条记录方法、路径、Owner别名、请求内容、状态、耗时、响应身份字段与响应摘要，以及 HTTP 调用开始/结束时间和开始时的在途请求/命令数，不保存 Authorization 或本地密钥。
- `initial-capacity.json` / `final-capacity.json` 是实际 HTTP 返回；`inventory.json` 封存输入。
- 每两秒以及末尾只读查询自己临时SQLite库的一份已提交快照。独立检查器不调用内核的合法性方法，检查整卡/切片占用冲突、重复设备声明、CPU/内存/存储超卖、非持有态遗留placement、重复释放效果。每个 placement slice 的 CPU/内存/存储必须分别为非负整数，拒绝 bool 和其他类型，然后才汇总，防止负值抵消其他 slice 的超卖。末尾核对任务和租约数量、Cancelled/Released状态、资源全归还。
- `invariant-snapshots.json` 保存观察结果、版本和完整快照摘要。这是抽样已提交状态核验，不是所有并发历史的逐前缀线性化证明，也不替代 CP3 完整前缀重放。
- `ledger-initial.json` / `ledger-final.json` 保留首份和末份无凭据原始账本；异常/未完成时尽量另存 `ledger-failure.json`，不变量失败会保存当时被检查的原始状态。`summary.json` 的 `ledger_snapshots` 记录文件SHA256与规范化状态摘要，可在临时数据库删除后复核。只保留这三类少量原始快照，普通采样仍只保存计数/摘要。若启动失败或数据库不可读，则记录快照缺失原因；发现运行器凭据时拒绝导出快照。
- `summary.json` 包含开始/结束构建、profile、目标与实际完成数、失败、请求分组p50/p95/p99、错误数、吞吐、证据文件SHA256。构建在运行中改变则失败，原始测量保留为开发诊断。

延迟定义为客户端发起到收到并解析完整HTTP响应，含本机调度、网络、服务端等待和处理。最近秩法计算分位数，失败/超时样本保留。普通命令、普通查询、最终核验和清理分开统计；没有注入延迟、批量推进时钟或实验运行。吞吐分母是负载总时间，包含并发请求与独立只读观察的真实成本。

`actual.peak_client_lifecycles` 是生命周期线程峰值。`actual.peak_http_requests_in_flight` 与 `actual.peak_http_commands_in_flight` 分别记录实际重叠的客户端 HTTP 调用峰值、其中普通变更命令的峰值；后者排除查询、核验和清理请求。计数窗口覆盖客户端调用到完整响应返回或抛出异常，含连接池等待，排除后续 JSON 解析和日志写入。它不证明服务端同时执行多少个 handler。

队列另记录客户端生命周期并发边界；服务端没有可用队列长度观测，输出 `NOT_OBSERVABLE`，不把线程数或在途 HTTP 数冒充服务端队列长度。

## 判定范围

`status=PASS` 仅指指定profile的所有生命周期和声明检查完成，仍不等于完整CP4通过。`cp4_concurrency` 明确比较普通命令 HTTP 实测在途峰值与32并发门槛。只有full完整执行、构建一致且未出错，并且实测普通命令 HTTP 峰值达到32，`cp4_scale` 才标为 `MET_FOR_DECLARED_WORKLOAD`；32个生命周期线程不自动满足该条件，未达到时标为 `NOT_MET`。pilot/smoke不能代替full。

当前运行器不会虚构4 vCPU/8 GiB限制。客户端和服务端共用当前主机，并发后台进程未隔离，环境信息如实记录；`cp4_reference_latency` 固定为 `NOT_RUN`。即便本机p95低于200ms也不认证参考环境指标。`joint_cp4` 固定为 `NOT_RUN`，UI、另一位真人操作、其他命令组合和真实GPU执行均不由本运行器签署。

当前存储层每次事务序列化整个权威状态，历史增长可能显著影响速度。应先运行pilot测量，再给full明确墙钟预算；超时应提交实际完成规模和原始负结果，由A负责其存储/事务/部署路径的优化。

# Prepared phase diagnostic — NOT_RUN

用户明确要求收尾时，这项 64-job 机制诊断尚未启动。没有启动诊断服务、创建运行数据库或发送任何业务请求，也没有产生分阶段计时结果。只保留准备文件和一次无负载预检的原始输出；不得将这些文件用于性能验收或瓶颈结论。

预检在 `inspect.isgeneratorfunction(Kernel._inventory_nodes)` 断言停止。实际方法返回 `iter_inventory(...)`；函数本身没有 `yield`，因此这条静态断言不成立。这是诊断工具的准备检查错误，不是产品失败。预检没有执行 `service.install()` 后续逻辑，更没有创建服务或数据库。收到收尾指令后没有修补、重试或开启负载。

已在容器内只读核对：`run-5f25604-64/`、原生 runtime `/tmp/fabric-locality-phases-5f25604-64-20261115`、`service-events.json` 都不存在。完整状态和文件哈希见 `status.json`，原始失败输出见 `preflight.log`。

保留文件：

- `phase_service.py`：尚未验证的计时 wrapper 草稿。
- `run_diagnostic.py`：尚未运行的单次 64-job driver 草稿。
- `preflight.py`：存在上述断言问题的无负载预检。
- `preflight.log`：原始预检失败输出。
- `status.json`：`NOT_RUN` 状态、边界、命令和哈希。

最终已实际运行的性能证据仍是冻结 5f25604 的 256-job probe；本目录既不补充，也不替代该结果。没有运行完整 10,000-job 负载。

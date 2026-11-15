# 定向检查执行记录

2026-11-15。本文件整理本轮工具输出；下列后端批次没有另存完整终端日志，明确保留这一来源区别。不同批次有重叠，不相加为独立测试数量。

| 检查命令 | 实际结果 | 执行阶段 |
|---|---|---|
| `uv run pytest -q tests/executors/test_cpu_reference.py tests/executors/test_cp2_kernel_driver.py tests/domain/test_http_cp2.py tests/blackbox/test_cp2_queries.py --tb=short` | 73 passed，9.75s | 加入未签名进度拒绝测试之前 |
| `uv run pytest -q tests/policy/test_reclaim.py tests/executors/test_reclaim_driver.py tests/blackbox/test_recall_status.py --tb=short` | 17 passed，7.34s | 自适应收回与真实执行接线 |
| `uv run pytest -q tests/executors/test_reclaim_driver.py tests/executors/test_cpu_reference.py::test_unsigned_progress_is_not_promoted_to_trusted_observation --tb=short` | 4 passed，6.63s | 进度签名检查补充 |
| `uv run pytest -q tests/policy/test_reclaim.py tests/blackbox/test_recall_status.py --tb=short` | 14 passed，0.65s | 默认切换尽快归还之后 |
| `uv run pytest -q tests/blackbox/test_reclaim_validation.py tests/blackbox/test_account_session.py tests/blackbox/test_browser_sessions.py` | 21 passed，2.37s | 报告、账户与会话定向回归 |
| `uv run pytest -q tests/blackbox/test_reclaim_validation.py` | 2 passed，0.22s | 报告错误分流最后修改之后 |
| `uv run pytest -q tests/research/test_reclaim_comparison.py` | 10 passed | v3 模型时钟与保存边界修正之后 |

界面原始日志 `fabric-adaptive-ui.log`（72 passed）、最后文案检查 `fabric-recall-copy-final.log`（11 passed）、生产构建 `fabric-recall-copy-build.log` 均保留在本目录。生成契约一致性、改动 Python 文件 Ruff、Git 空白检查、两个浏览器脚本语法检查和文档链接检查通过；静态规范检查见 `static-receipt.json`。未重新执行完整 CP3/CP4 性能套件。

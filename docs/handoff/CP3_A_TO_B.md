# CP3：A → B 接线与联合验收

CP2 已由 A/B 双方确认人工验收通过。A 本轮从 `aa63fb6` 开发 `work/a-cp3`；原 CP2 实例、凭据和历史证据保持原位。

## 接线

在 B 自己的干净工作树执行：

```sh
git fetch origin
# 保持自己的 CP3 分支，不切到 A 分支开发
git merge origin/work/a-cp3
uv sync --locked
npm --prefix web ci
make cp3-a
```

有未提交修改时先保存自己的工作，不使用强制重置。A 修改包括 CPU 执行器启动恢复、B 反馈的管理员总览单快照读取、首次登记事务及验收工具；公开 DTO 与 API 字段未变，不需要改已有前端调用。protocol 2 对新 attempt 生效，无法确认的旧执行继续隔离。总览修复涉及 B 的查询装配文件，合并时保留单次已授权读取，见 [409 修复交接](CP2_OVERVIEW_409_FIX.md)。

## A 已提供的门槛

| 注册门槛 | 验证器 | 结果入口 |
|---|---|---|
| CP3-CRASH-RECOVERY | `tests/acceptance/cp3/cp3_crash_recovery.py` | `evidence/cp3/crash_recovery_receipt.json` |
| CP3-CPU-EQUIVALENCE | `tests/acceptance/cp3/cp3_cpu_equivalence.py` | `evidence/cp3/cpu_equivalence_receipt.json` |
| CP3-INDEPENDENT-MODEL | `tests/acceptance/cp3/cp3_independent_model.py` | `evidence/cp3/independent_model_receipt.json` |

实际是否通过须读取对应构建的回执。`make cp3-a` 还运行已有集成回归，但不会签署下列五项。

## B 主线与联合门槛

1. **CP3-PREFIX-REPLAY**：改写/删除未来输入，保持同一可见前缀的决定相同；重放已提交事件重建最终状态。目前事件并不含全部工作参数或回执。先明确日志需要补什么、重建哪些权威字段，再由 A 对写入边界配合；不要把账本计数重建冒充完整重放。
2. **CP3-DECISION-COVERAGE**：解释来自实际被接受的计划，候选覆盖、淘汰理由、版本与最终绑定可追溯，拒绝计划不能显示成已执行。
3. **CP3-POLICY-EVIDENCE**：五类场景 × 至少五个封存 seed；统一 B0/B1/B2/P 与适用消融。保留全部失败、未完成和个体损益，不只输出平均成功等待。
4. **CP3-POLICY-TARGET**：按既定统一公式要求 `R <= 0.85`，配对完成数不下降，定义的个体/Owner 严重损害不增加。没有达到就记录未达到，不能改阈值、删 seed 或改分母。最终验证集不用于调参。
5. **CP3-SETTLEMENT**：结果、失败、中断、恢复和租约结算在界面可使用；保存报告、工件核验、实际接续分别展示，不能看到 ready 就计为成功恢复。

注册路径与准确标准以 `configs/checkpoints.json`、原 checkpoint 蓝图为准。B 增加对应注册验证器并提供可执行入口、原始实验和具体 UI 证据；A 随后拉取双方代码做同构建联合验收。需要两位实际操作者的部分由最终集成版本发简明步骤，此时不要求提前重复 CP2 操作。

## 放行规则

`make cp3-a` 的成功仅表示 A 侧三门槛及既有回归成功。`make checkpoint CP=CP3` 必须检查八门槛及其构建、验证器和证据身份，缺失或过期应失败。回执不得借用 CP2 人工记录代替 CP3 新旅程。未完成联合判定时，保持 CP3，不启动 CP4。

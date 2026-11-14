# CP3 B 线实现与验收边界

本分支从本地已获取的 A 线 `work/a-cp3`（`6e3afe5`）建立。用户明确授权进入 CP3 开发；旧 CP2 验收实例与证据保留。CP2 管理员总览 409 已交给 A，本次不修改该接口，也不把旧的待验项目写成通过。

## B 交付范围

- 独立实验：冻结五族、五验证 seed、B0/B1/B2/P 和关闭机会分量的真实消融；每次运行拥有独立账本。保留原始输入、事件、计划、回执、终态及元数据。
- 配对核验：从完整工作队列计算需求面积、完成数、每工作与 Owner 损害；中性、反向、失败与未完成样本均保留。阈值仍为族等权 `R <= 0.85`，不随验证结果调整。
- 前缀和解释：在隔离实例检查未来输入不可提前影响在线决策，核对实际提交与候选范围声明；完整事件重放的缺口独立记录。
- 页面：CP3 报告与 CP2 研发样例分开；首屏呈现结果、失败原因、资源结算与下载入口；历史记录折叠。保存报告、工件核验、实际接续继续分别显示。
- 验收：补齐 B 的五个注册入口与 `make cp3-b`。原始证据绑定 Git 提交及源内容；缺证据、旧构建、失败和未达标均不会显示通过。

## 执行方式

使用 Python 3.12、Node 22，安装锁定依赖后执行：

```sh
make setup
make cp0
# 在源文件稳定、完成提交后冻结；不要覆盖已有验证目录。
uv run python -m experiments.cp3_suite freeze --output evidence/cp3/policy-suite
uv run python -m experiments.cp3_suite run --suite evidence/cp3/policy-suite --workers 3
uv run python -m experiments.cp3_suite analyze --suite evidence/cp3/policy-suite
make cp3-b
make checkpoint CP=CP3
```

开发 seed 与正式验证 seed 分开。不得删掉不利结果重新冻结，或修改阈值后把同一批验证输入称作新的盲测。某项数值未达标时，`make cp3-b` / 联合检查应返回失败；工程实现完成不改变这个判据。

A 的三个注册可靠性验证器须在同一构建运行。`make cp3-b` 只消费其真实、当前构建回执，不代跑签名或自动将 A 项目设为通过。A 原有 `make cp3-a` 会重新写联合回执，若先跑 A，应在最后运行 B 汇总。

## 只读实验页面

使用 `campus_fabric.workflows.devhost:create_app` 启动产品服务。新增扩展：

- `GET /x/v1/policy-validation`：管理员读取当前 checkout 的冻结实验摘要。
- `GET /x/v1/policy-validation/artifacts/{artifact_id}`：仅下载登记的工件 ID；不接受客户端文件路径。

默认目录为 `evidence/cp3/policy-suite`。接口不启动实验，不修改运营实例。研究者伪造角色头无效；身份由服务端认证。原始 JSON 下载保留字节与整数精度。不存在实验时显示 `NOT_RUN`；坏哈希、身份不匹配或旧构建返回 409，并清除旧页面数据。

## 目前模型与联合依赖

当前 A 内核按实例使用 10 秒固定逻辑工期。每次 32 项工作、400 秒观察窗低于蓝图建议的 256–512 项；本轮是有界集成实验，不是大规模性能结论。资源大小多样不等于运行时间多样，超时参数不充当工期。因此“稀缺大任务与大量短任务”等运行时异构覆盖仍待 A 的明确执行模型接口。

完整权威状态无法仅从现有公共事件恢复：事件不含所有注册参数、幂等回执和持久状态。B 对可覆盖字段的独立事件 fold 必须写明范围；该结果不能代替完整 `CP3-PREFIX-REPLAY`。具体缺字段和复现由注册验证器输出。

当前 recovery-cost 分量未启用，机会分量消融会真实运行；不创建虚假的恢复成本消融。逻辑 ready 报告仍不等于 CPU 已核验保存点，也不等于已接续。

本文件记录实现和复现方法，不是通过证明。最终结果以 `evidence/cp3/b_delivery_receipt.json`、`joint_gate_review.json` 及冻结实验目录为准。自动 DOM/HTTP 检查不代替 A、B 本人人工验收。

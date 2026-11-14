# CP4 图表与结果来源核验（非 UI）

```sh
uv run python -m experiments.cp4_provenance \
  --suite evidence/cp3/policy-suite \
  --figure-index evidence/cp4/figure-index.json \
  --output evidence/cp4/provenance.json
```

命令只读取来源，生成新的核验 JSON，不运行实验、不更改封存输入、不覆盖已有输出。只有本次来源范围全部满足时退出码为 0；缺少包、未执行的注册运行、没有图表索引或空索引为 `NOT_RUN`，损坏/错版本/伪造来源为 `FAIL`。源码在审计中变化为 `STALE`。调用接口为 `audit_provenance(root, suite, figure_index=None)`。

核验覆盖当前源码 SHA 和提交、封存 manifest、全部冻结输入和原始工件 SHA、运行前后构建、运行 ID、输入 SHA、模型与策略身份；复用 CP3 原始事件/命令/回执/最终状态重算，另外核对 run-metadata 中的 run_id、input_sha256、model。已登记 analysis 必须与重新计算结果一致。运行缺口与已经核验的子集分别列出，不用 development 的 50 项登记或 5 项执行替代 validation 的 125 项。

图表/结果必须显式登记，文件位于仓库 `evidence/cp4/` 下，不接受绝对路径、路径穿越或符号链接。索引格式如下；SHA、构建与数值必须填写实际值，示例占位符不能作为证据：

```json
{
  "schema": "cp4-figure-index-v1",
  "build": {"git_commit": "实际提交", "source_sha256": "实际源码SHA", "source_file_count": 123},
  "figures": [{
    "id": "completed-work-table",
    "kind": "result",
    "path": "evidence/cp4/results/completed.json",
    "sha256": "实际输出文件SHA",
    "sources": [{
      "run_id": "compatibility-fragmentation--0--B0",
      "artifact": "summary",
      "sha256": "该run登记的summary SHA",
      "json_pointer": "/works/0/status",
      "value": "实际来源字段的值"
    }]
  }]
}
```

`kind` 可为 `figure` 或 `result`；每条至少一个来源。来源可指向已经核验运行的 `summary`、`input`、`run-metadata`，`json_pointer` 遵循 JSON Pointer，`value` 按 JSON 原值逐项核对。每条输出同时记录 input/model/run/build 链。汇总图应登记其使用的每个运行及相应值；本工具不自动推断配对聚合公式或搜索未登记图表。

来源完整与算法收益是两个结论。`algorithm_target` 单独列示：development 为 `NOT_APPLICABLE`，缺少完整正式运行证据为 `NOT_RUN`；完整 validation 来源才依据 CP3 原始重算给出 `MET` / `NOT_MET`，不降低原目标。即使来源完整，也不代表算法目标、五族语义覆盖、完整权威状态重放、真人验收或 CP4 整体通过。

边界：核验输出文件字节与索引声明的来源值，不解析图中文字、视觉编码或报告论证；不宣称所有图表已登记。没有实际图表与来源包时，应保留 `NOT_RUN`，不生成虚构图表或运行。

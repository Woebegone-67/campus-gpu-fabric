# CP0 验收入口与后续证据门禁

本仓库当前交付 CP0：可审阅的设计、契约、配置、六区前端骨架和本地 HTTP 开发入口。`/healthz` 与 `/v1/version` 可运行，其余 19 个官方操作显式返回 501。静态检查、类型生成、测试工具自测、前端构建和服务启动，分别证明各自执行过的事情；均不代表 21 个操作合规、账本正确或成品可用。

正式目标来自 [终态产品](blueprint/01_终态产品与独立复审.md)、[双人作战书](blueprint/02_双人24小时正向开发作战书.md) 与 [交汇契约](blueprint/03_交汇契约与验收规格.md)。交接节点见 [CHECKPOINTS](handoff/CHECKPOINTS.md)。

## 运行入口

从仓库根目录执行：

```sh
make cp0
make checkpoint CP=CP0
make checkpoint CP=CP1
```

`make cp0` 执行仓库规定的契约导出/类型一致性、pytest、前端构建、静态检查和真实本地服务冒烟。单独的 `uv run python scripts/check_cp0.py` 只执行静态部分，输出 `evidence/cp0/static_receipt.json`。`scripts/smoke_cp0.py` 的实际 HTTP 子进程检查输出独立的 `runtime_receipt.json`。不能从一份静态回执推断其余构建步骤已经执行。

`make checkpoint CP=CP0` 核对这两份回执的具体检查 ID、结果和当前构建身份，输出 `cp0_static_and_host_only`。其中的 `MET` 只表示这些 CP0 检查有有效记录，`product_acceptance` 仍为 `NOT_RUN`。它不重新运行 npm 或 pytest，也不把契约样例当作真实任务记录。

`make checkpoint CP=CP1` 至 `CP5` 读取对应 `evidence/cpN/receipt.json`。当前没有这些运行证据，命令必须以非零退出码报告 `NOT_RUN` 或 `NOT_MET`。测试文件存在、前端能构建、HTTP 返回 501，均不能满足后续检查点。

## CP0 静态检查实际检查什么

- `spec/official/manifest.json` 包含赛题、OpenAPI、补充 PDF 三个文件的 SHA-256；逐字节核对本地冻结副本。这个清单证明本地副本未变，不是主办方数字签名或外部认证。
- `contracts/api_ownership.json` 与冻结 YAML 的 method/path 集合严格相等，共 21 项，无重复、遗漏或新增；每项有 A/B 负责人、领域对象、计划事件、工作区和验证 ID。查询明确不产生领域事件；测试接口只映射到受控实验/验收视角。
- `contracts/generated/contracts.schema.json` 包含八个交汇对象；按 `contracts/examples/manifest.json` 校验正例和声明应被 JSON Schema 拒绝的反例。仅靠 Pydantic 跨字段约束拒绝的反例由契约测试执行，不假称 JSON Schema 已验证。
- `configs/semantics.json` 含具体值、来源、解释、影响操作、验证例和替换位置；团队暂定语义不能全部冒充官方规定。
- 蓝图、A/B 交接、状态、产品、语义、验收文档确实存在且有内容。
- `configs/checkpoints.json` 含 CP0—CP5 的明确判据、所需证据类型和初始 `NOT_RUN` 状态；若人为改成 `MET`，必须同时存在可通过当前构建门禁的运行证据。

`tests/cp0/` 测试的是这些检查工具能否拒绝缺失、篡改、过期、静态冒充运行等情况。测试使用临时目录里的合成回执，不是产品验收结果，也不会写出 CP1 成功回执。

## CP1—CP5 回执格式

配置中的 `status: NOT_RUN` 是初始计划快照。实际状态由当前回执计算；不要只改配置状态。每项期望、目标 ID 和允许证据类型在配置中登记，不能通过缩小回执判据来通过检查。后续真实验证器登记在 `validator_path`，位于 `tests/acceptance/cpN/`；这些验证器在 CP0 尚未实现。实施时可审阅后更正登记路径，再生成运行证据。

完整回执结构如下。示意值必须换成实际执行结果；省略的检查不会通过门禁。

```json
{
  "schema_version": "1.0.0",
  "checkpoint": "CP1",
  "scope": "runtime_acceptance",
  "status": "MET",
  "build": {
    "git_commit": "实际提交 SHA",
    "source_sha256": "实际源码摘要",
    "source_file_count": 100
  },
  "checks": [
    {
      "id": "CP1-NORMAL-JOURNEY",
      "target_id": "T-PRODUCT",
      "status": "MET",
      "build": {"git_commit": "同上", "source_sha256": "同上", "source_file_count": 100},
      "input_version": "已冻结场景与输入的版本或摘要",
      "executor": "实际操作者或运行器身份",
      "command": "可复现的实际命令或操作顺序",
      "expected": "逐字使用 configs/checkpoints.json 中本检查的 expected",
      "actual": "实际观察，包含结果、计数和差异；不能只填 PASS",
      "observed_at": "2026-11-14T08:00:00+00:00",
      "evidence_kind": "ui_runtime",
      "validator": {
        "path": "tests/acceptance/cp1/cp1_normal_journey.py",
        "sha256": "实际验证器文件摘要"
      },
      "evidence": [
        {"path": "evidence/cp1/normal-journey.log", "sha256": "实际日志文件摘要"}
      ],
      "unresolved_issues": []
    }
  ]
}
```

查看当前构建标识：

```sh
uv run python scripts/check_checkpoint.py --build-id
```

构建身份同时包含 Git 提交与实际文件内容摘要，因此同一个提交上修改了代码、测试、契约、配置、依赖锁或文档，旧回执也会失效。`evidence/`、依赖缓存、`.git` 和构建输出不参与摘要，写入回执本身不会使它立刻过期。CP1 以后必须具有 Git 提交身份。清洁克隆中只有源文件、路径和内容一致时才可匹配；不同机器本地依赖缓存不影响身份。

证据必须是对应 `evidence/cpN/` 下实际存在、非空且哈希匹配的文件；禁止路径逃逸、引用自己、引用 CP0 静态回执当作运行证据。每项还需登记验证器的真实文件哈希。缺检查、`NOT_RUN`、非允许的证据类型、未解决问题、旧构建或错误摘要，全部返回非零。

这个门禁验证证据的完整性、归属和当前性，不凭文件格式证明日志中的陈述真实。正式验收仍需执行登记验证器、审阅原始 HTTP/进程/UI 记录，必要时由另一位操作者重放。真实 CPU 恢复、性能收益和实际上传分别提供各自证据；本地压缩包或模拟事件不能替代。

## 结果解释

`MET / NOT_MET / NOT_RUN` 对应团队明确目标。效果、规模、延迟目标未达到时保留 `NOT_MET` 和原始结果，不删困难输入、不改分母，也不因此否认已真实成立的其他能力。继续修复与交接不受这些命令的时间限制；门禁只阻止把缺证据或未达目标写成已经达成。

CP0 之后的完整工作仍由 A（Woebegone-67）和 B（xuyangma）按交接文档分工。运行正式验证前，先冻结输入、模型、策略、构建和判据；验收记录必须分清本地重放、独立复核、真实执行和外部提交。

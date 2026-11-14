# 2026-11-15 UI 联合检查证据

这是独立 clone 上的开发联合检查，**不是完整 CP4、真人验收或终稿交互通过证书**。最新 UI 终稿要求在 `docs/handoff/B_UI_FINAL_REVISION.md`，当前代码尚未实施该清单。

## 来源与文件

- A `5428c48` + B `913545f` 合并为 `3704599`。
- `session-builds.json`：从本地各独立 session 仅提取启动时间、端口与原构建身份；不包含凭据、进程认证信息。用户试用 `joint-ui-01` 与自动测试实例不同。
- `cp0.log`：一次 `make cp0`，退出 0；1231 Python 用例通过（482.00 秒，1 条弃用警告），102 UI 用例通过，Ruff、契约、构建、静态 CP0 与真实进程冒烟 5 项通过。不是 125 组策略实验重跑。
- `ui-baseline.log`、`client-baseline.log`：前期 UI 102 / client 15 通过；与工程门槛重叠，不累加数量。
- `load-adapter.log`：续租工具 36 项定向回归通过，包含两个不能算成功的反例。
- `fixtures-02/browser-results.json`：17/17 Chrome 视觉与交互检查通过；业务 API 为合成数据，不证明真实业务执行。
- `live-02/receipt.json`：9 组真实 Chrome + 真实后端检查通过。每阶段快照、截图及真实下载文件在同目录。结果原始字节比较与 Python 整数核对通过；无页内脚本错误。
- `report/receipt.json`：真实浏览器读取被固定摘要封存的历史 125 次实验报告，并下载 1,415,227 字节分析原件，SHA256=`fc7152ec290508cae801603bee2329e52625f7e95c34d374f5c6099dae08b5c1`。R=1，历史证据不可用于当前构建验收。未执行新策略实验。
- `overview-live.png`、`form-live.png`：早期真实空实例视觉检查；不单独充当业务通过证据。

## 失败尝试完整保留

- `live-01/receipt.json` 为 FAIL：自动脚本用 GPU ID 作完整可访问名称，但页面标签还带节点和型号，定位不到复选框。没有完成收回流程，只有登录检查通过；后续用完整标签前缀修正脚本，在新空实例完整执行 `live-02`。
- `fixtures/browser-results.json` 保留首次尝试：测试运行期间为下一轮业务检查切换了自有测试实例，造成页面服务短暂关闭，出现连接失败。`fixtures-02` 在稳定实例重新跑完整 17 项。
- 早期临时导入探索脚本使用精确标签匹配包含选项文字的下拉框，超时后改用表单内实际 select；它没有业务写入，不纳入通过计数。

## 构建范围

`make cp0` 从 `72adcbe` 启动；期间新增/调整的是独立浏览器脚本和交付文档，未改动被检查的应用、契约、锁定依赖、pytest 用例和组件用例。真实闭环脚本记录 `f4cd22b`，报告脚本记录 `83e1f13`；各服务实际启动摘要以 `session-builds.json` 为准，启动时也可能包含尚未提交的工具修改，不用 Git 名称代替完整摘要。

已通过逐路径 diff 确认：`src`、`web/src`、`contracts`、`configs`、`deploy`、Python/Node 依赖及 Makefile 与合并提交 `3704599` 相同。后续文档、独立检查脚本与续租辅助工具变更单独记录。最终交付文档会改变完整 source SHA；不声称最终文档提交整份构建再次完整运行测试。

下载与事实记录不包含本地身份 token；提交前以实际开发 token 对所有文本证据比对，零命中。截图未显示明文凭据。

## 复现入口

安装锁定项目依赖后，独立准备 `runtime/starwave-browser` 的 `playwright-core@1.65.1`，使用实际 Chrome。每次真实业务检查使用新生成的空 session 和不存在的证据目录，避免误操作真人试用实例。

```sh
JOINT_URL=http://127.0.0.1:15233 \
JOINT_IDENTITIES=runtime/your-new-session/cpu/client-identities.json \
JOINT_EVIDENCE=evidence/your-new-live-check \
node web/tests/joint-live.browser.mjs

DUAL_SPACE_URL=http://127.0.0.1:15233 \
DUAL_SPACE_EVIDENCE=evidence/your-new-fixture-check \
node web/tests/dual-space.browser.mjs
```

报告检查脚本 `web/tests/joint-report.browser.mjs` 使用相同三个 `JOINT_*` 参数，但服务需显式配置只读历史归档及 manifest/index 固定摘要。没有归档时应显示未运行，不运行脚本来制造报告。

未测：物理触控板/Safari/VoiceOver、独立真人干净部署、A/B 交叉实操、终稿命名与后续 UI 修改、正式 CP4 性能和最终视频。并行工程检查期间的 CPU 耗时只服务功能验证，不用于吞吐性能宣传。

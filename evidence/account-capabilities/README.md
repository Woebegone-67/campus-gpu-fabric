# 同账户能力改造验证

范围：`work/account-capabilities` 基于 UI 交接 `42aea00` 的第一批后端改造。此目录不构成 CP4 通过记录。

- [定向检查](targeted-tests.txt) / [JUnit明细](targeted-tests.xml)：252 passed，13.02秒。覆盖账户契约、初始化、核心/完整宿主的session、两个member同时使用和贡献、旧身份、所有权、管理员边界、查询提示和工作流及相关HTTP回归。HTTP场景经TestClient/ASGI与临时持久内核运行，不冒充真人浏览器验收。
- [代码检查](lint.txt)：Ruff通过。
- [契约一致性](contracts-check.txt)：原内部契约与独立账户契约的JSON Schema/TS生成结果一致。
- [前端编译](web-build.txt)：TypeScript与Vite构建通过，未实施或验收新UI。
- [冻结规范与语义静态检查](static-check.txt)、[独立HTTP进程冒烟](host-smoke.txt)：检查输出见文件；不替代完整CP0/CP4门禁。

本轮未运行完整pytest/CP0门禁或125组策略实验；未迁移/重启既有真人验收实例；未执行新UI手势、真实GPU、双人干净部署和最终实录。性能门槛未放宽。

测试开发中修正了一处无效测试输入（收回通知0秒改为规范内1秒）及一处测试路径拼写，最终记录为修正后的执行结果。没有修改业务规则来迁就测试。

代码另经独立只读权限审查，未发现阻碍合入的问题；审查不是额外执行证据。更多说明见 [后端交接](../../docs/handoff/ACCOUNT_CAPABILITIES.md)。

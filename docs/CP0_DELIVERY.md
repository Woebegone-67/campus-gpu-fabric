# CP0 交付记录

日期：2026-11-14。当前范围是项目开工、契约冻结和协作入口。

## 已建立

- 独立项目、Python 3.12/uv后端、React/TypeScript/Vite前端与依赖锁。
- 官方challenge/OpenAPI/补充说明原件；21项操作的负责人、对象、事件、工作区和验证映射。
- 八个核心交汇DTO、内部端口、13份正反样例、Python→JSON Schema→TypeScript生成链。
- 工作意图登记与原子租约/job绑定接口，显式actor和request_id；未把纯公共jobs接口说成幂等。
- 21条带来源的语义规则、状态占用和不变量；A/B任务书及六次checkpoint。
- 私有仓库、CODEOWNERS、PR/缺陷/交接模板、CI配置；A为Woebegone-67，B为xuyangma。

## 实际本地验证

| 检查 | 本次观察 |
|---|---|
| Python测试 | 123 passed：80契约边界、21验收工具、22 HTTP骨架测试 |
| Ruff | 通过 |
| JSON Schema / TypeScript生成一致性 | 通过；过时生成物会拒绝 |
| TypeScript类型检查和Vite构建 | 通过 |
| CP0静态检查 | 6项MET：官方副本、21操作、语义、契约样例、文档、证据策略 |
| 真实HTTP子进程冒烟 | health/version与两个明确501占位检查通过；4项 |
| CP0回执门禁 | 当前构建的静态与启动记录被接受；scope为cp0_static_and_host_only |
| 非默认端口前端代理 | 两个make开发入口实际启动，HTML和代理health返回正常 |
| 未来门禁 | CP1没有业务证据，应返回NOT_RUN及非零退出；工具测试验证了这一行为 |

本地测试环境为macOS/Apple Silicon、Python 3.12.13、Node 22.23.2。测试出现一条Starlette对httpx TestClient的弃用提示，没有测试失败；实际子进程HTTP冒烟使用独立httpx客户端。后续升级适配由A维护，不以隐藏告警替代处理。

运行回执由`make cp0`生成到`evidence/cp0/`；不把某次机器上的绝对路径提交为通用运行配置。GitHub Actions配置运行同一命令，具体远端运行状态与日志在仓库Actions查看。已从本地Git进行独立干净克隆，创建新的虚拟环境和node_modules，依次执行make setup、make cp0、make checkpoint CP=CP0通过；CP1按预期返回NOT_RUN及退出1。

## 当前边界和下一步

现在是**CP0完成后的开发起点**，不是成品验收：仅health/version有实际实现，19个业务/测试操作明确501。没有资源账本、调度收益、GPU执行、真实CPU接续或浏览器完整用户旅程的通过声明。六工作区是开发骨架，未填入虚构任务。

A沿`work/a-core`推进唯一事务内核与正常资源/任务链；B沿`work/b-product`推进查询、策略接口及正常用户旅程。CP1目标见各自任务书。对现有501占位测试要随对应实现更新，而业务验收要求不能随之减弱。

## 来源与AI使用

CP0由Codex协助建立工程、契约、文档和验证工具，包含三个并行分工及主任务集成复核。团队仍需理解并验证后续实现。FastAPI、Pydantic、React、Vite、TypeScript、uv、pytest、Ruff及其依赖按锁文件使用，保留各自许可证；未复制成熟平台整套实现。

工程入口参考：[FastAPI测试文档](https://fastapi.tiangolo.com/tutorial/testing/)、[Vite入门与运行环境](https://vite.dev/guide/)。业务依据来自仓库原题和团队已审蓝图；既有MCM源码、数据与论文没有导入或改动。

## GitHub协作状态

私有仓库已创建并推送，xuyangma的write权限已通过GitHub API确认。初次推送的GitHub Actions在Ubuntu环境通过同一CP0检查（运行43551800310）；后续运行以Actions页面记录为准。

已尝试启用main强制PR/CI分支保护，GitHub因当前账号套餐不支持私有仓库该功能返回403。仓库保持私有，不升级付费或改公开；CODEOWNERS、PR模板与自动CI已配置，但不能声称平台已强制阻止直接推送。双方约定经PR审阅后合入main，不force-push。

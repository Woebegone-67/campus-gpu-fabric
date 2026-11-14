# A 开工单：可信资源与执行

负责人 **Xiuzhu Du / Woebegone-67**；分支 `work/a-core`；目标协作仓库 `Woebegone-67/campus-gpu-fabric`。远端建立和分支状态以实际Git记录为准，不以本文为创建证明。T0由两人开始实施时登记。

先读 `docs/blueprint/00_START_HERE.md`，再读 `docs/SEMANTICS.md`、`docs/STATE_MODEL.md` 与 `src/campus_fabric/contracts/`。共享契约和 `contracts/generated/` 由集成负责人维护；修改需给精确diff和兼容说明，不直接并行改生成物。

产品扩展路径按 `docs/EXTENSIONS.md` 接线；内部类型与准确签名以 `contracts/ports.py` 为准。

你唯一写：`src/campus_fabric/{domain,storage,clock,auth,executors,api/mutations,api/test_control,api/extensions/mutations}/`、`tests/{domain,concurrency}/`、`deploy/`、后端依赖/锁、启动和路由装配。B唯一写读投影、查询、策略、workflow、web和experiments；A提供所有持久写命令，B不能另建业务账本。

执行顺序：

1. `make setup && make cp0` 确认基线；使用自己的数据库/run目录与端口，禁止reset集成或B实例。
2. H1–H5：单事务提交、版本快照、owner/幂等、资源形状、库存到租约/job正常链、时间/续租/释放、重启重载。尽早交付真实开发host；以 `make api` 启动入口为准。
3. H5–H10：全部写/测试控制、贡献者权限、回收/保存失败/无替代等待、两预算与代次隔离、CPU第一条保存—停止—恢复。
4. H10–H16：杀进程窗口、旧CPU子进程确认/隔离、独立参考模型、生成式交错；真实恢复结果对照不中断运行。
5. H16–H24：规模/延迟、换人干净部署、依赖封存、迁移/限制/来源。对自己模块自测并提供证据，不把测试和说明转给B。

只向B交三类东西：已授权不可变快照/事件；CommandCommit回执；执行与工件证据。B交DecisionPlan后你复查epoch/revision、全部硬约束、权限和预算再原子提交；拒绝计划不能显示已执行。

必须先做 WorkIntent 事务口：`WorkflowPort.create_job_and_bind`（完整参数含actor、request_id，见ports.py） 将创建job、绑定、阶段回执同事务落地再响应；相同阶段重试先返回原回执，参数冲突拒绝。allocation阶段同样处理。不能靠“先创建再补关联”，必须测试提交后响应丢失窗口；冻结 `POST /v1/jobs` 不因此获得假定幂等。

交接前勾选：

- [ ] 每写路由有command、事件、错误和验收映射；无未授权第二写路径。
- [ ] 32并发最后资源、重复key/异payload、提交后断线均有实际历史。
- [ ] 大步clock与分步一致；回收/到期无双还，旧回调无复活。
- [ ] 真实CPU未确认停止保持隔离；不以模拟事件声称实测完成。
- [ ] 按 HANDOFF_TEMPLATE 交构建、命令、预期/实际、证据和最多三项跨边界问题。

`make checkpoint CP=CP1` 及后续门槛需实际结果；尚未实现/未运行时应失败并记录NOT_RUN。上述业务清单当前未勾选；可直接按本任务书开工，完成后附实测证据。

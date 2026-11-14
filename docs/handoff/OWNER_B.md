# B 开工单：服务决策与操作体验

负责人 **xuyangma**；分支 `work/b-product`；目标协作仓库 `Woebegone-67/campus-gpu-fabric`。先读README、本任务书与`docs/PRODUCT.md`、`docs/SEMANTICS.md` 与共享contracts；契约变更交集成负责人，不直接编辑 `contracts/generated/`。

产品扩展路径按 `docs/EXTENSIONS.md` 接线；蓝图01–03按需查阅，内部类型以contracts为准。

你唯一写：`src/campus_fabric/{projections,api/queries,api/extensions/queries,policies,workflows}/`、`web/`、`experiments/`、`tests/{policy,ui,blackbox}/`，前端依赖锁、产品/实验/视频材料。A写领域、账本、时钟、权限、执行与所有持久命令。workflow由你编排，持久状态通过A事务口保存；页面不维护第二份业务真相。

执行顺序：

1. `make setup && make cp0`；使用自己的端口、库和run目录。`make web` 开发入口；Node22/React/TypeScript/Vite。合同样例只用于组件/纯函数，不能出现在运行验收里冒充服务。
2. H1–H5：六区正式布局、三角色/统一状态组件；读API与timeline；正常提交到结算；allocation成功job失败时继续用现有租约；B0/B1/B2插件。H5接真实host。
3. H5–H10：贡献者扩展真实操作、保存/恢复/等待、decision候选与排除解释、候选策略、通用场景运行器；H10六区全部读真实数据。
4. H10–H16：冻结验证集与模型、五族配对/消融及个体损害、负结果；全域与截断解释区分；未来前缀攻击；隔离回放与结果/失败结算。
5. H16–H24：陌生输入、1440×900与空错态、换人HTTP/UI挑战、证据索引、同构建3–5分钟实录及来源说明。

只依赖A的不可变DecisionSnapshot/ReadSnapshot、CommitReceipt/事件及WorkflowPort；交纯DecisionPlan，不写账本。A给来的版本失效则重算；没有完备候选域证据，不写“全池不可行”。所有排序使用稳定同分键；策略不得读场景未来、隐藏工期或评估器。

WorkIntent持久化阶段必须调用A的原子create-bind-receipt接口。创建成功响应丢失时重试同一stage；不得重发无幂等保证的公共jobs并猜测成功。到达计时覆盖资源等待、job失败和恢复；自动退租按用户授权持久执行，不能依赖浏览器在线。

交接前勾选：

- [ ] 三角色六区有真实动作、错误/空态/部分成功；测试密钥不进入Web。
- [ ] 列表、events、metrics与工件无跨Owner泄露；reset后旧epoch不能覆盖新视图。
- [ ] 每个decision对应当时输入与实际提交；过滤理由不从事后状态猜。
- [ ] B0/B1/B2与P共享合法性/模型/预算；未完成、失败、反向seed保留。
- [ ] 每图可追到run/input/build；真实CPU与模拟分报；未运行不显示MET。
- [ ] 按 HANDOFF_TEMPLATE 提交自己的验证和最多三项跨边界问题。

`make checkpoint CP=CP1` 起验证真实旅程；未运行门槛保持NOT_RUN并失败。可直接按本任务书开工；业务HTTP/UI及策略收益需在后续节点实际验证。

# CP2 双人验收：启动与证据导出

A、B 在各自电脑上使用**同一提交版本**完成全部角色链，再交叉查看截图和导出记录。`127.0.0.1` 指各自电脑，两台机器不会连接到同一数据库。两人操作成功的确认需要单独记录，脚本不会生成“人工通过”。

## 1. 一次启动两种模式

在项目根目录执行；首次使用先装依赖（Python 3.12、Node 22.12–22.x）：

```bash
make setup
uv run python scripts/cp2_joint_session.py start
```

| 模式 | 浏览器打开 | API | 导入本地身份文件 |
|---|---|---|---|
| 逻辑模拟 | http://127.0.0.1:15180 | http://127.0.0.1:18087 | `runtime/cp2-joint/logical/client-identities.json` |
| 真实 CPU 参考执行 | http://127.0.0.1:15181 | http://127.0.0.1:18088 | `runtime/cp2-joint/cpu/client-identities.json` |

两种模式均使用 external / P 策略，独立身份、库存副本和数据库。请分别导入对应文件，再选择 `alice`（研究者）、`supplier`（资源贡献者）、`bob`（隔离检查）或 `admin`（管理员）。身份仅留在浏览器内存；整页刷新后重新导入。

逻辑模式通过管理员页面推进逻辑时间。CPU 模式按实际时间执行固定的整数计算，并以约 50 秒演示节奏保留可操作窗口；它真实保存、停止和载入状态，但不运行提交的镜像/命令，不代表 GPU 执行。

重复 `start` 不重置数据库、不更换凭据、不重复启动现有进程。已有端口被占用时脚本直接退出，不停止占用者。CP1/QA 的 `18081/18085/18086/15174/15178/15179` 被明确保护。

## 2. 操作时保留什么

按本轮人工验收步骤，分别完成研究者提交、贡献者回收、同工作恢复、完成/结算、另一 Owner 查询、刷新后查回记录。保存能够辨认**模式、身份、工作 ID、attempt、保存验证状态、结果及租约状态**的截图。A、B 各自记录操作者和实际观察，不把脚本健康状态当作人工验收结论。

查看服务与版本：

```bash
uv run python scripts/cp2_joint_session.py status
```

输出包含进程身份、HTTP 健康状态、启动时 build 和当前 build。`source_matches_start=false` 表示启动后源码有变化：先完成正在运行工作的结算，`stop` 后再 `start`，再做对应验收。

## 3. 导出与停止

在服务仍运行时导出：

```bash
uv run python scripts/cp2_joint_session.py export
```

输出目录默认位于 `runtime/cp2-joint/exports/<UTC 时间>/`，包含 `receipt.json`、`logical.json` 和 `cpu.json`：启动 build、逐项读取版本、事件、工作详情、租约、任务和可读取的保存点/结果。导出只读 HTTP，不导出身份文件、Bearer token、数据库或服务日志；分页/读取失败会明确标记 `complete=false` 并返回非零状态。服务活动期间各次读取可能有不同 revision，不冒充同一原子快照。

将**导出目录与截图**交给对方检查即可。不要共享 `auth.json` 或 `client-identities.json`；它们权限为 `600`，默认目录被 Git 忽略。支持 `export --output <新的目录>`，不会覆盖既有导出。

所有工作已完成或取消、租约已释放后：

```bash
uv run python scripts/cp2_joint_session.py stop
```

`stop` 仅按启动时间和唯一会话标识停止自己创建的 API/Web 进程，保留数据和凭据。它不等同取消任务或释放租约；若仍有 CPU 工作，工作可能在服务离线时完成，下一次启动会按实际时间恢复核对。脚本不会按端口杀进程，也不在停止超时时强杀不明进程。

需要另开一套临时实例时，可使用 `--runtime <新目录>` 和四项端口参数 `--logical-api-port`、`--logical-web-port`、`--cpu-api-port`、`--cpu-web-port`；同一实例再次启动必须使用原端口。仓库内的私有实例仅允许放在 `runtime/` 下。

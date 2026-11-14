# CP4 候选实施与验证入口

本轮在 `work/cp4-acceptance` 开发，原 CP3 及研究分支、运行目录和端口保留。
这是候选实现说明，不是通过回执。七项原目标、性能负结果、真人要求均不降低。

## 实际变更

- SQLite schema2 同事务追加完整持久状态差分 journal；旧 schema1 原子迁移并标明历史起点。公开事件的资源折叠与私有 journal 的完整重放分别核验。
- 库存推导一次索引活跃资源声明，避免每个节点重新解析所有租约；隔离节点使用只读叠加，避免复制整个资源池。这两项不改变调度策略或账本语义。
- CP4 七项登记入口具备实际执行或只读证据核验程序。可靠性含真实 HTTP、32 并发、未见输入组合、原核心生命周期、实际 CPU 杀进程/恢复等价及完整重放。
- 正式门禁重新核验 CP4 原始材料及嵌套摘要；不以单项回执的 PASS 字符串、自动换角色或视频文件名代替事实。
- 规模负载、时间预算和延迟定义预先登记在 `tests/acceptance/cp4/LOAD_PROTOCOL.md`。达到预算但未完成目标仍为 NOT_MET。

## 冻结后的执行顺序

```sh
# 一次完整必要工程回归；不运行125次策略实验
make cp0

# 本轮可靠性与继承质量；其余缺证据项目仍会使总入口非零退出
uv run python scripts/accept_cp4.py --checks reliability

# 先按 deploy/cp4-load.sh 准备独立4CPU/8GiB容器，再在容器内执行
python tests/acceptance/cp4/cp4_scale.py
python tests/acceptance/cp4/cp4_latency.py

# 只读复核和组装，不重复负载/CPU实验
uv run python scripts/accept_cp4.py --collect-only
make checkpoint CP=CP4
```

单项失败保留原始记录，修复后只运行受影响项；最终所有材料必须对应同一候选构建。
不将旧125次的实验构建改为当前构建，历史图表仅做来源与计算审计。

## 独立人工实例

依赖准备完毕后，以以下独立目录和端口启动。身份文件只在本机，不进入 Git、验收包或录像。

```sh
uv run python scripts/cp2_joint_session.py start \
  --runtime runtime/cp4-final \
  --logical-api-port 18094 --logical-web-port 15186 \
  --cpu-api-port 18095 --cpu-web-port 15187 \
  --policy-archive /Users/xz/Projects/campus-gpu-fabric/.worktrees/cp3-acceptance/evidence/cp3/policy-suite \
  --policy-archive-manifest-sha256 305a4dc7fa3dd1ebc797315367eaa19b883b45a86b0a93b7771bd150250c51ee \
  --policy-archive-index-sha256 ff5567ab3a94d22d6a03496a83188e5c95fb6ae2dab1294ce59ec7f28630df34
```

访问逻辑实例 `http://127.0.0.1:15186` 与 CPU 实例 `http://127.0.0.1:15187`。
若前期诊断会话仍占用上述端口，先用其原目录执行 `stop`，不重置其中数据。
换机器时仅将 `--policy-archive` 改为原封存实验目录；两个固定摘要不能从待核验文件重新生成替换。
三个归档参数必须同时提供并与会话记录一致。未提供时仍严格核验当前构建，旧报告不会自动冒充新实验。
历史模式保留原实验构建，页面明确不能验收当前构建。每次查询核验协议、固定索引和分析摘要；
各原始工件在下载时重新核验字节。页面的“原运行审核结果”不代表本次扫描了全部原始工件。
会话管理脚本沿用旧名称，但独立目录归本候选，不会重置原实例。
实际换人部署、六任务交叉操作和实录按 `CP4_HUMAN_ACCEPTANCE.md` 执行。
自动验证不能签署 A/B，缺少材料时正式门禁必须拒绝放行。

所有实际结果、失败原因及命令写入 `evidence/cp4/`，而不是回写本文为“通过”。

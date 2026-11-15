# 神经分配版本：运行入口

基线 `0217e45`，分支 `research/b-reclaim-breakthrough`。此版交付核心算法及可运行接线，不继续进行性能调参。

## 普通产品页面

```bash
.venv/bin/python scripts/cp2_joint_session.py start \
  --runtime runtime/neural-product-20261115 \
  --logical-api-port 18282 --logical-web-port 15282 \
  --cpu-api-port 18283 --cpu-web-port 15283
```

打开逻辑模式 `http://127.0.0.1:15282`，或 CPU 参考执行 `http://127.0.0.1:15283`。Node 需要 22.12+ 的 22.x，并已安装 web 依赖；Python 使用项目虚拟环境。

按本轮用户要求，已有账号 `alice`、`bob`、`chen`、`admin` 的密码框输入任意非空内容即可登录，未知账号仍拒绝。**此版取消了密码身份校验，知道账号名的人可进入该账号，包括管理员。** 权限范围、会话 Cookie、同源检查和资源所有权检查继续由服务端执行。此说明同时适用于直接启动服务的正式配置，没有额外演示开关。

退出登录后可切换账号；同一浏览器的普通窗口共用会话，不用于模拟同时在线的两个用户。默认页面库存及任意新任务仍使用原有策略，资料不满足新算法的支持范围时不会伪造预测。

停止上述本轮独立实例：

```bash
.venv/bin/python scripts/cp2_joint_session.py stop --runtime runtime/neural-product-20261115
```

## 一条命令展示真实神经分配

```bash
.venv/bin/python -m experiments.research.neural_worker_demo \
  --study-dir evidence/neural-availability-20261115-run01 \
  --output runtime/my-neural-demo
```

输出目录必须全新。无需重训；程序使用已训练模型、审核历史输入、实际工作流 worker 和独立 SQLite 账本，打印每项工作的默认 GPU 与神经分配 GPU。`task-gpu-rankings.json` 是每项任务各自的显卡排序；`learned-worker-commits.json` 等文件保存实际提交。随后核对贡献者 1 秒收回及原 900 秒租约到期。

已有一次完整成功回执：`evidence/neural-worker-demo-20261115-run01/RECEIPT.md`。四项工作真实提交到模拟资源账本，其中两项改变选卡。这里的“真实提交”指实际应用内核，不是真实 CUDA/GPU 训练。

## 核心算法和接线

过去供给窗口与时间规律 → MLP 可用窗口分布 → 结合任务工期、保存成本、恢复预算计算每项任务的 GPU 排名 → 有界联合匹配 → A 内核校验并分配。预测失准也不影响贡献者按期收回；既有自适应收回与恢复仍有效。

- 实际训练及模型加载：`src/campus_fabric/policies/availability_model.py`。
- 任务条件排名及策略：`src/campus_fabric/policies/learned.py`。
- 有限恢复预算的完成成本：`src/campus_fabric/policies/renewal.py`。
- 审核历史到快照的适配：`src/campus_fabric/workflows/availability_inputs.py`。
- 接入配置、输入结构与 A 需要提供的生命周期流：`docs/research/NEURAL_ALLOCATION_HANDOFF.md`。

目前训练数据为 896 条合成样本，数据量与代表性不足，神经预测额外收益尚未证实。它们是负面结果的可能原因，未证明数据量是主要原因。本版不会以研究权重冒充真实用户在线学习结果；完整自动历史采集、一般多卡/MIG 库存和已持续一段时间的窗口还未接入神经分配。当前完整演示覆盖独立单卡节点、初始分配、显式任务 profile。

## 保存和回滚

本轮在独立 Git 分支开发，原预览目录及数据保留。撤销本轮登录提交可恢复密码核验；神经策略移除两个 `FABRIC_AVAILABILITY_MODEL_*` 配置及三个输入配置后恢复原策略。不覆盖历史研究记录，不需要数据库迁移。

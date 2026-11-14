# CP4 B 自动清洁后端部署预演

入口 `experiments/cp4_clean_deploy.py` 自动验证一个已提交候选能否从新目录安装并完成真实 HTTP 后端旅程。它不签署正式 `CP4-CLEAN-DEPLOY`：换人实际操作、UI 构建和 TypeScript 重新生成分别保留 `NOT_RUN`。Python JSON Schema 的生成一致性会实际核对；完整生成检查和前端构建由根回归入口另行记录。

## 运行前提与入口

需要本机 Git、Python 3.12 和用户指定的现有 uv 可执行文件，以及安装 `uv.lock` 中依赖所需的网络或可用包源。使用 POSIX 进程组清理，适用于当前 macOS/Linux 开发环境；不需要 GPU。先提交并稳定所有候选源文件、测试、文档与部署文件。当前目录内容与干净 HEAD 克隆不一致时，预演会在安装依赖前拒绝继续；不自动提交或复制未提交源码。

```sh
# 在仓库根目录运行，替换为本机现有 uv 的绝对路径
.venv/bin/python -m experiments.cp4_clean_deploy --uv /absolute/path/to/uv

# 可指定一个尚不存在的输出目录及总执行时限
.venv/bin/python -m experiments.cp4_clean_deploy --uv /absolute/path/to/uv \
  --output evidence/cp4/b/clean-deploy/candidate-unique --timeout-seconds 300

# 仅复验当前环境的两个 TCP 旅程，不执行克隆或安装
.venv/bin/python -m experiments.cp4_clean_deploy --worker \
  --output evidence/cp4/b/clean-deploy/worker-unique
```

输出目录由当前运行独占创建，连已有空目录也拒绝复用。默认输出位于 `evidence/cp4/b/clean-deploy/<UTC>-<随机标识>/`。控制入口和 worker 都拒绝 `python -O/-OO`，防止断言被禁用后生成成功回执。

## 实际执行链

1. 从本地仓库 `git clone --no-hardlinks --no-checkout` 到新的临时目录，然后 detached checkout 调用时的 HEAD。
2. 比较原工作树与 clone 的提交、源文件摘要及文件数；额外比较锁文件和完整 `deploy/` 文件清单、内容哈希与可执行位。旧共享 build 摘要没有覆盖 `deploy/`，这份清单独立补齐该边界；不修改 A 的部署配置。
3. 使用指定 uv 执行 `sync --locked --python <控制进程的 Python>`，创建 clone 内全新 `.venv` 和独立临时 uv 缓存。不会继承原虚拟环境、身份令牌、Git 配置覆盖或含凭据的包源环境变量。
4. 使用新环境执行 `scripts/export_contracts.py --check`，实际检查已提交 JSON Schema 与 Pydantic 新生成内容相同。
5. 使用新环境运行 `--worker`。worker 启动自己的随机 loopback 端口、临时数据库与临时身份，运行实际 A+B 服务和策略 P。依次完成正常工作自动归还、W2 尽力运行任务超时 2 秒并推进逻辑时间 10 秒后的失败自动归还。逐项核对一次 attempt、同一工作/租约/job、结果事实、结算事件、归还容量以及其他 Owner 无权读取。
6. 再次比较原工作树与 clone 的构建及部署清单，清理自己创建的实例和临时 clone，保留输出日志与回执。源码中途变化时不能通过。

执行使用当前声明的固定逻辑模型，不执行提交的任意 image/command，不生成真实 CPU/GPU 结果。独立 `--worker` 的成功只证明两个 HTTP 旅程，不证明干净安装。

## 完整日志与证据边界

输出包含 `receipt.json`、`source_release_manifest.json`、`clone_release_manifest.json`、`commands.json`、每条命令的完整脱敏日志，以及 `worker/worker_receipt.json` 和 `worker/http_history.json`。HTTP 文件保留请求和响应、角色别名、请求 ID、实际状态、服务命令与服务日志，不包含临时认证值。锁文件、部署文件、命令日志和 HTTP 文件均有 SHA-256；记录 uv 文件身份、版本输出和控制 Python 身份。

父进程有总时限，每条子命令也有时限。超时只终止本运行建立的进程组，随后清理临时 clone；不停止现有开发实例。失败步骤记 `NOT_MET`，尚未运行步骤保留 `NOT_RUN`，命令返回非零；失败记录不会被后续运行覆盖。源码或文档后续变化会使旧证据过期，应对新提交重新运行。

即使自动预演为 `MET`，`formal_cp4_clean_deploy`、`human_different_operator`、`ui_build`、`typescript_regeneration` 仍为 `NOT_RUN`。此入口不替代 README 换人验收、完整 CP4 可靠性/规模/延迟、真实 CPU 恢复或视频证据。

# B 工作台第一版

React / TypeScript / Vite。沿用现有视觉，只增加真实工作流和查询交互。

## 运行

使用 `bash web/dev-local.sh api` 与 `bash web/dev-local.sh web` 启动集成版；也可在 Node 22 环境单独启动前端，代理指向自己的 API 端口：

```sh
FABRIC_API_TARGET=http://127.0.0.1:8081 npm --prefix web run dev -- --port 5174
npm --prefix web run test:client
npm --prefix web run build
```

## 身份

“当前身份”面板支持 Owner、可选 bearer，以及导入 A 的 `client-identities.json`。
导入文件仅在浏览器内存解析；不会上传原文件、写入 localStorage/sessionStorage 或日志。
关闭面板保留导入列表；刷新页面或“清除身份”移除该会话凭据。
profile 名字只是本地选择器标签，不是权限凭据。所有权限由 A 的身份适配核验。
管理员 profile 没有 `X-Owner` 时，客户端不发送这个头。
目前尚无已冻结的服务端 session/me 端点；UI 不自报可信角色，也不请求不存在的 session 端点。
后续若 A 提供受权会话投影，可用其角色/可用动作替代 profile 名称对管理入口的显示提示。
管理入口即使显示，服务端仍必须验证管理员 bearer。

## 真实接线

- `POST /x/v1/work-intents` 发送 `WorkIntentRegistration`，接受的是登记回执，不宣称任务已运行。
- 创建时保存稳定 intent ID 和同一输入；响应丢失只重试同一登记。工作推进和持久状态归服务端。
- 工作详情读真实 `legal_actions`，CP1 动作使用 `allocate/create_job/release/cancel`；两个创建阶段保持 `allocate-v1` / `job-v1`。
- 资源租约的续租、释放和任务取消走冻结公共 API。公共租约默认保留；工作意图按结算选择处理，最终以租约真实状态为准。
- 贡献登记直接发送 `NodeInventory`；资源恢复和贡献收回入口禁用并标明 CP2 待接入。
- 页面不请求测试控制路由、不携带测试密钥。
- 当前 CP1 constant-work-v1 为 10 秒逻辑工期，不实际执行镜像命令，不构造结果文件。
- 模拟时钟只有导入 admin profile 时展示，使用普通管理员写扩展；不代表真实计算加速。
- 实验页面只读取既有隔离 run 和导出真实记录；实验提交与场景选择待正式写契约接入。

## 查询与状态

B 查询列表使用 `items`，详情使用 `item`，带 `version/now/request_id`。
对象分页使用服务端返回的 `next_cursor=epoch:revision:offset`；版本冲突后从首页刷新。
事件续读使用 `next_after=epoch:sequence`，不把全局最新 sequence 当成已交付位置。
每次查询替换快照，不将不同 epoch/身份的数据合并。旧请求会 abort，迟到响应不能覆盖新请求。
查询失败与真正的空列表分别显示；501/503 不渲染伪造库存或运行数据。
Checkpoint 报告、工件核验、实际接续分开呈现。计划选中和实际提交分开呈现。

## 验证范围

`test:client` 使用明确的隔离 transport fixtures，覆盖身份头、无 owner 管理员、测试路由拒绝、
503、HTTP 成功但业务拒绝、202 接受、HTML 代理兜底、响应丢失和对象身份。
这些单元测试不是产品旅程验收，也不是 GPU 执行证据。真实浏览器/HTTP 旅程由根目录 B 联调记录单独给出。

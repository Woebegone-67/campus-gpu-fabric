# B 交互动效与 06d7958 整合

2026-11-15，分支 `feat/b-cp4-starwave`。本轮在 `913545f` 的双空间界面上增加交互反馈，并快进合入 A 的 `06d795805af4ce9c30fc226e9e18e9579efd3c17`。最新 UI 终稿建议已接收；本文只记录本轮实现，不宣称整份终稿交付或完整 CP4 通过。

## 改动

- 侧栏与空间切换的选中背景平滑移动；页面内容短距离淡入。原有路由、独立滚动位置和表单草稿保留。
- 按钮按住缩至 0.96，增加黑色内阴影；释放轻微超弹回到原尺寸。键盘操作、移开取消和窗口失焦均清理按压状态。
- 查询成功与重复刷新加入短淡入，刷新期间继续展示原结果；不通过重新挂载结果清空展开状态或输入。工作详情四块面板仍保持 20px 间距。
- 原生折叠入口统一呈现按钮边框、背景、可点击区域与旋转箭头；展开内容淡入，关闭仍按原生语义即时隐藏。键盘及长记录的内部滚动保持可用。
- 工作列表、详情、提交结果与总览的工作 ID 增加复制按钮；成功有提示，失败给出可选中的完整 ID。异步反馈不能跨工作或账户残留。
- 遵循系统减少动态效果设置。身份与实例代次切换立即清除旧内容，动画不保留上个账户的画面。

未更改业务命令、请求载荷或权限机制。黑色 Starwave、玻璃基线与不透明工作台继续保留。

## 文件

- `web/src/motion.tsx`、`interaction-motion.css`：页面/查询揭示、移动选中层、按压与回弹。
- `web/src/disclosure-motion.css`：原生折叠入口的按钮外观与展开反馈。
- `web/src/components/CopyWorkId.tsx`、`copy-work-id.css`：复制及失败回退。
- `web/src/App.tsx`、`components/common.tsx`、`main.tsx`：接入交互层。
- `web/src/pages/{Run,Continuity,Overview}.tsx`：工作 ID 复制入口。
- `web/tests/copy-work-id.test.tsx`、`overview.test.tsx`、`interaction-motion.browser.mjs`：组件与浏览器回归。

## 验证

本轮日志与真实 Chrome 截图位于本机 `evidence/cp4/interaction-motion/20261115/`。浏览器回归使用明确标识的合成响应，拦截全部业务 API 请求；这是实际浏览器渲染/交互证据，不是后端业务执行或正式比赛结果。

- 合入后 Chrome 157：新增交互检查 **8/8 PASS**，见 `post-06d7958/interaction-motion-browser.json`；实际按压比例 0.96，释放峰值约 1.016。
- 双空间原有浏览器基线 **17/17 PASS**，见 `dual-space-baseline/browser-results.json`。该基线在合入前运行，上游未改生产前端文件。
- 详情面板间距 **20/20/20px**，见 `post-06d7958-gap/detail-gap.json`。
- 两套浏览器检查均无业务写入、外部请求和页面异常；桌面 1440×900 与手机 390×844 截图在 `post-06d7958/`。
- API 客户端测试 **15/15 PASS**，见 `integrated/client.log`。
- 整合版本 `make cp0` **PASS**：契约生成一致性、Ruff、**1231** 项 Python 测试、**108** 项 UI 测试、TypeScript/Vite 构建、CP0 静态检查和 **5** 项真实 HTTP 子进程冒烟均通过，见 `integrated/cp0.log`。这不代表完整 CP4 验收通过。

未验证：Safari/WebKit、物理触控板/触屏手感、操作系统剪贴板权限弹窗、全站 UI 终稿真人交叉验收。本轮不重跑封存的 125 组策略实验。

## 上游与后续依赖

`06d7958` 带入后端资源局部校验、写入准入与请求隔离改进，以及联合验收历史材料。其 SQLite schema 仍为 3，运行配置与前端契约没有改动。上游的历史回执不自动成为当前整合版本的通过证据。

已收到 `docs/handoff/B_UI_FINAL_REVISION.md`。其中工作名称仍缺后端持久化接口：当前 `WorkIntentRegistration` 和 `WorkIntent` 无 `display_name`。完整信息结构调整与命名流程不属于本轮已完成项，不以浏览器别名替代持久化。

## 保存与回退

合入前创建了 `backup/b-motion-before-06d7958-20261115-032808`，指向原 `913545f`。未提交的动效、复制与折叠修改另存于 Git stash `43bd1b41b932f97fcd1906a23d5ea537514f65c9`，已应用回当前分支，备份仍保留。

只回退本轮界面调整时，对本交付提交执行 `git revert <交付提交号>`，保留 A 的 `06d7958`。需要查看完整旧版本时，从上述备份分支创建独立 worktree；不要在有新改动的目录执行 hard reset，也不要覆盖已有运行目录。未执行远端发布。

# CP4 小组件毛玻璃调整

前半部分记录 `80662d4` 的初版验证。用户随后要求降低透明度；当前加厚版本的参数、验证与回滚见末节，初版证据保留。

日期：2026-11-15（北京时间）。基于 `65986610f23b0c49e0af72ec04d316be8361c12a`，分支 `feat/b-cp4-starwave`。开始前工作区干净；此前 Starwave 版本保留在 `backup/cp4-starwave-20261114`。

用户要求把现有小组件做成毛玻璃。当前已安装的 skills 中没有 Apple Liquid Glass skill；这是参考 [Apple 材质规范](https://developer.apple.com/design/human-interface-guidelines/materials) 的 CSS 网页适配，没有调用或宣称实现 Apple 的原生 Liquid Glass API。

## 调整范围

- 按钮采用胶囊圆角、柔和表面高光、边缘亮线及按下内阴影；主要动作与轻量刷新按钮保持不同层级。
- 输入框、原生下拉框、文本域和文件按钮使用中性玻璃表面。输入框圆角为 11px，保留原生输入、选择和尺寸调整行为。
- 租约摘要、结算摘要、保存点事实和服务能力小卡片采用 14px 圆角及相同材质。
- 基础填充仍是 20%，高光为独立的低透明度灰白渐变；没有给文字或整块业务内容设置透明度。
- 大面板继续提供一次 `blur(14px)` 背景采样；内部控件和小卡片不重复采样。只有没有玻璃父面板的顶部按钮使用局部 `blur(12px)`。
- 纯黑侧栏、连续星芒、弹性波纹、冻结参数及所有业务组件、接口、状态处理保持原样。未增加依赖。

修改文件仅有：

| 文件 | 内容 |
| --- | --- |
| `web/src/visual/starwave/cp4-glass-widgets.css` | `.cp4-starwave` 内的小组件材质、状态与降级样式 |
| `web/src/main.tsx` | 导入上述样式 |
| `docs/CP4_GLASS_WIDGETS.md` | 本说明 |

## 验证与证据

证据根目录：`evidence/cp4/glass-widgets/`。没有覆盖上一轮已交付的 Starwave 证据包。

| 检查 | 本轮结果 |
| --- | --- |
| Chromium 开发页回归 | 18/18 通过，`browser-2026-11-14T15-57-07.051Z/` |
| 构建后静态预览回归 | 5/5 通过，`production-2026-11-14T16-01-25.578Z/` |
| 小组件与尺寸补充检查 | 7/7 通过，`supplemental-2026-11-14T16-00-51.828Z/` |
| 接口客户端测试 | 15/15 通过，`client.log` |
| TypeScript / Vite 构建 | 通过，`build.log` |
| `make cp0` | 通过：804 项 Python、52 项 UI 测试，合同/ruff/构建检查及 5 项子进程 smoke；`cp0.log` |
| 冻结引擎等五文件校验 | 与已确认工具包字节一致，`source-parity.json` |

浏览器接口全部被合成 fixtures 拦截，包括提交、取消、释放验证；没有向正在运行的 Fabric 后端发送业务写入。完整门禁使用项目自身的本地隔离测试。浏览器检查没有未捕获 JavaScript 异常和外网请求，预期权限错误仍按原样展示。

Python 测试有一条既有 Starlette/httpx 弃用提示；JSDOM 的 Canvas 未实现输出不影响本轮真实 Chromium 检查。门禁没有失败，不将 CP0 门禁通过解释为新的 CP4 业务验收通过。

实际检查覆盖一个 Canvas、纯黑侧栏、内部滚动、六路由、控件点击不触发背景波纹、焦点、disabled、普通按钮静态渐变、primary/quiet 层级、20% 基础填充和嵌套控件无额外 blur。390×844 无文档横向溢出，360×225 仍有可用的内部滚动区域；classic 回退仍有效。

真实 Chromium 截图（业务内容为合成数据）：

- `browser-2026-11-14T15-57-07.051Z/run-1440x900.png`：桌面工作台。
- `supplemental-2026-11-14T16-00-51.828Z/run-form-controls-1440x900.png`：桌面表单与动作。
- `supplemental-2026-11-14T16-00-51.828Z/continuity-checkpoint-1440x900.png`：保存点小卡片。
- `supplemental-2026-11-14T16-00-51.828Z/run-form-controls-390x844.png`：手机宽度表单。
- `supplemental-2026-11-14T16-00-51.828Z/continuity-checkpoint-390x844.png`：手机宽度小卡片。

保留两轮补充脚本准备失败的原始记录（`15-59-10.572Z` 与 `16-00-24.016Z`）：最初 fixture 没有保存点字段，无法找到小卡片；headless macOS 的原生下拉弹出菜单键盘序列未可靠提交选择。第二轮 Enter 触发了合成表单提交，仍被 fixture 拦截。最终补充 fixture，并使用 `selectOption` 验证原生 HTML select 的值和事件。没有修改产品逻辑来迁就测试，也不把最终通过解释成操作系统弹出菜单的键盘路径已通过。

未验证：Safari/Firefox、实体手机、原生 macOS 减少动态设置切换、原生下拉弹出菜单键盘路径、真实后台标签切换、长时能耗、生产任务。短浏览器检查不构成性能无损或全平台效果一致的承诺。

## 预览与回滚

本地静态预览：<http://127.0.0.1:15190/>。刷新可读取新构建；该预览没有连接后端，服务离线提示正常。截图中的在线状态来自测试拦截数据。

只撤销本次小组件效果：移除 `web/src/main.tsx` 中 `cp4-glass-widgets.css` 的导入并重新构建，即恢复 `6598661` 的材质，保留星芒。也可对本次独立提交执行 `git revert`，避免覆盖其他改动。

暂时关闭整个 Starwave 皮肤：访问 <http://127.0.0.1:15190/?visual=classic#run>。未发布上线或推送远端。

## 加厚版本（2026-11-15）

基于干净的 `80662d48aa2b205f2de65d94e2c2184291042d2e`，仍在 `feat/b-cp4-starwave`。按用户“组块太透明”的反馈调整 CP4 局部皮肤：面板和小卡片填充改为 64%，输入框和普通/主要按钮填充改为 52%；面板模糊从 14px 调为 24px，独立顶部按钮从 12px 调为 16px。提高小卡片正文与提示文字亮度，悬停仍保持或增加填充；轻量按钮保留层级。禁用输入不回退至旧的 20% 填充，原生 select 的既有禁用 opacity 保留。

冻结引擎、预设及其 20% 原始变量保持不变，新材质由 CP4 专用变量控制。面板内控件不增加嵌套模糊；不支持 backdrop-filter 时使用 96% 灰底降级。业务逻辑、路由和接口均未改动。

本次修改文件：`web/src/visual/starwave/cp4-glass-widgets.css`、`web/tests/starwave.browser.mjs`（更新实际面板填充和模糊期望）、本说明。

新证据目录：`evidence/cp4/glass-dense/`。

| 检查 | 本次结果 |
| --- | --- |
| UI 测试 | 52/52，通过；`ui.log` |
| TypeScript / Vite 构建 | 通过；`build.log` |
| 默认 Chromium 回归 | 18/18，通过；`browser-2026-11-14T16-13-18.485Z/` |
| 材质与尺寸补充 | 9/9，通过；`supplemental-2026-11-14T16-14-59.604Z/` |
| 最终构建预览 | 5/5，通过；`production-final-2026-11-14T16-15-00.657Z/` |
| 冻结文件 SHA-256 | 五文件保持一致；`source-parity.json` |

本次重新检查桌面 1440×900、手机 390×844、短屏 360×225、禁用输入、按钮 hover、文字亮度和嵌套模糊。实际截图位于补充目录的 `run-form-controls-1440x900.png`、`continuity-checkpoint-1440x900.png`、`run-form-controls-390x844.png` 和 `continuity-checkpoint-390x844.png`。浏览器业务请求全部由测试 fixtures 拦截，真实业务写入为零。

首轮补充断言误要求禁用原生 select 的 opacity 为 1，实际原产品为 0.7；已修正测试以保留原禁用状态，并检查其基础填充为 52%。失败原始记录仍保留在 `supplemental-2026-11-14T16-14-11.480Z/`，没有为通过测试改动该产品行为。

本轮仅重新执行上述与视觉修改相关的检查；前文 `make cp0` 和接口客户端结果属于 `80662d4` 初版历史证据。Safari/Firefox、实体手机、原生系统菜单键盘操作、长时能耗与真实业务未重验。

预览仍为 <http://127.0.0.1:15190/>，刷新即可看到新构建；静态预览服务离线提示正常。撤销这次加厚：对本次独立提交执行 `git revert <提交号>`，然后 `npm --prefix web run build`，恢复 `80662d4` 的较透明材质。不要使用 hard reset 覆盖后续改动。提交号记录在交付目录的 `manifest.json`。

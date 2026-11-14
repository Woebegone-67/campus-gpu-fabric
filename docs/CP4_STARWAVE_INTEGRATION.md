# CP4 Starwave 视觉接入记录

日期：2026-11-14。范围：B 线现有 CP4 视觉层；不代表整个 CP4、正式业务或生产部署验收完成。

## 版本与工作区

- 目标目录：`/Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b`。
- 开始时分支：`feat/b-cp4`；工作区干净，无用户未提交改动。
- 基线：`bf75c2d4eeec593518660b4bfdb8f0a82c3eeda3`；已有保护标签 `backup/cp4-non-ui-20261114-bf75c2d`。
- 本次独立分支：`feat/b-cp4-starwave`。集成提交由 `backup/cp4-starwave-20261114` 标签标识；具体提交号见交付目录的 `manifest.json`。
- 保留其他 CP1/CP2/CP3 工作树、旧实验、运行实例与数据；无强制重置、清理、推送或发布。
- 已阅读工具包 `CODEX_TASK.md`、视觉契约、引擎生命周期、框架示例、验收与证据边界，以及仓库 `AGENTS.md`、README、B 线分工。

## 实现与冻结边界

CP4 使用 React 19、TypeScript、Vite 和现有 hash 路由。只更换页面壳的视觉容器、背景及组件皮肤；六项导航、业务组件、表单字段、身份状态、接口、事件处理函数均保留。没有导入 `demo/main.js`、Demo HTML、三张演示卡或调参面板，没有新增产品依赖。

以下文件从工具包逐字节复制，SHA-256 与来源写入交付证据 `source-parity.json`：

| 工具包文件 | 接入目录内文件 | 用途 |
| --- | --- | --- |
| `integration/starwave-engine.mjs` | 同名 | 冻结星芒与弹性波纹算法 |
| `integration/starwave-preset.mjs` | 同名 | 冻结增强版参数 |
| `integration/create-starwave.mjs` | 同名 | 生命周期适配器 |
| `integration/starwave-scoped.css` | 同名 | 局部材料与容器样式 |
| `LICENSE` | 同名 | 保留原许可 |

共同接入目录为 `web/src/visual/starwave/`。预设维持：上限 18,000 星芒、spacing 12、density 1、brightness 1.02、波强度 18、传播速度 320、活动动画时间 2,200ms、最多 4 个波纹、DPR 上限 1.75、绘制频率上限 45。没有降低密度、随机化曲面或添加紫色渐变。

真实主区域和侧栏底色均为 `#000`，侧栏不含 Canvas。卡片填充为 `rgba(92,92,92,0.20)`，卡片整体 opacity 为 1；嵌套输入框与按钮不重复背景模糊。文字正常不透明，成功、等待和错误保留可区分的语义颜色，未对业务 DOM 使用灰度滤镜。

必要的 CP4 适配：

1. 长事件页可超过 6,900px，不能让画布随整页增长。画布固定在主视口，业务内容在子容器滚动，点击继续使用主容器坐标。桌面主画布为 1220×900 CSS 像素，不进入 220px 侧栏。
2. 保留原来的移动端两列导航，画布从导航下方开始。短视口将主区域最小高度保留为 160px，并允许文档滚过导航，避免把业务内容压成 0px。正常 390×844 布局不受影响。
3. React effect 负责初始化和 dispose，StrictMode 未关闭。普通业务路由切换保留同一视觉宿主；卸载整壳或关闭视觉层时清理实例。
4. 顶部新增本地暂停按钮，响应系统减少动态偏好；不使用后端配置、浏览器存储或新的网络请求。开发模式才提供宿主调试句柄，正式构建没有该句柄。
5. 增加前端 URL 回退开关 `?visual=classic`，保留原始 `style.css`，便于局部撤销。

## 修改文件清单

| 文件 | 修改目的 |
| --- | --- |
| `web/src/App.tsx` | 挂载主区域视觉壳、局部开关与暂停控件；业务处理函数不变 |
| `web/src/main.tsx` | 在原样式后引入两份作用域样式 |
| `web/src/visual/starwave/StarwaveSurface.tsx` | React 生命周期、内部滚动容器、减少动态提示、静态降级 |
| `web/src/visual/starwave/create-starwave.d.mts` | 冻结适配器的 TypeScript 类型 |
| `web/src/visual/starwave/cp4-starwave.css` | `.cp4-starwave` 范围内的真实组件皮肤与响应式适配 |
| `web/src/visual/starwave/{starwave-engine.mjs,starwave-preset.mjs,create-starwave.mjs,starwave-scoped.css,LICENSE}` | 上述五个原样复用文件 |
| `web/tests/starwave.test.tsx` | 引擎与 React 挂载、销毁、降级及暂停回归 |
| `web/tests/starwave.browser.mjs` | 真实 Chromium 验收、截图与边界记录 |
| `web/tests/starwave.fixtures.mjs` | 仅测试使用的隔离 API 拦截数据 |
| `docs/CP4_STARWAVE_INTEGRATION.md` | 本记录 |

## 测试结果与证据范围

接入前，原有 46 项前端测试和原构建通过；从基线提交归档出独立副本，构建后保存原版桌面与移动资源页截图。原版与新版均使用相同的隔离数据检查页面结构。

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| `make cp0` | 通过 | 合同生成一致性、ruff、804 项 Python 测试、52 项前端测试、TS/Vite 构建、CP0 静态门禁和 5 项真实子进程 smoke |
| `npm --prefix web run test:client` | 15/15 通过 | 既有接口客户端回归 |
| 新增生命周期测试 | 6/6 通过，包含在 52 项中 | 真实冻结引擎 + JSDOM/受控绘图环境；不是原生浏览器后台切换 |
| Chromium 开发页面 | 18/18 通过 | 六路由、真实 DOM 操作、截图、波纹、暂停、权限/加载/空态等 |
| Chromium 构建后 preview | 5/5 通过 | 无开发调试句柄，路由、表单、暂停/减少动态、390px、classic 回退 |
| 补充浏览器检查 | 4 项通过；原生后台切换未验证 | DPR=2、720×450、360×225、无未捕获异常/外部请求；保留后台切换未触发的原始 FAIL 记录 |
| 冻结文件校验、`git diff --check` | 通过 | 五文件逐字节一致，无空白错误 |

浏览器业务检查全部使用 Playwright 路由拦截：API 读写返回合成数据，未连接运行中的 Fabric 业务后端。已点击表单校验、模拟提交、取消确认框的取消/确定、模拟释放，检查异步回执及权限错误。它们证明前端交互未被视觉层遮挡，不能当作真实任务、资源申请、支付或生产执行成功。没有为本次视觉验证触发此类真实业务写入。

检查到的具体结果：

- 空白点击 `0→1→0`，连续点击最多 4 个波纹；双击仅首次点击可能生成波纹，右键不生成。截图保留可见波纹。
- 输入框、下拉选择、标签、身份按钮、禁用分页等不产生额外波纹；文字可选择，Tab 焦点清晰；原生确认框可取消和确定。
- 长页内部滚到 2600px 后，画布仍为 1220×900，波纹位置与点击位置对应。
- 本地暂停前后 elapsedMs 和 renderedFrames 均保持不变，恢复后继续增长且不补算暂停时间。
- Chromium 媒体模拟 `prefers-reduced-motion: reduce` 后静态显示，本地恢复不能绕过它；按钮状态同步。该检查不是原生 macOS 设置切换。
- 开发 StrictMode、状态更新、六路由与前进/后退只有一个 Canvas；受控单测确认销毁移除 RAF、Canvas、ResizeObserver 与监听器。
- Canvas2D 返回 null 时，保留纯黑壳和可用业务控件，无未捕获异常；构造中后期任意异常不在此证明范围。
- 窗口 1100、760、390px 均无文档横向溢出；720×450 和 360×225 为小 CSS 视口检查，不冒充实际浏览器 200%/400% 缩放。
- 浏览器无未捕获 JS 异常，无新外部请求。控制台保留预期的合成 HTTP 403；故意禁用 Canvas 的测试保留静态降级警告。JSDOM 既有测试的 Canvas 未实现提示属于非浏览器环境；Python 有一条既有 Starlette/httpx 弃用提示，没有失败。

CP4 当前没有 Portal/Popover/Tooltip、自定义拖拽区或侧栏折叠实现，这些控件的专门验收不适用；没有新增它们来替代原页面。

## 性能观察

环境为本机 macOS ARM64、Chrome 157.0.8103.51。浏览器操作主要在隔离 headless 上下文完成；补充检查使用独立临时配置的 headed Chrome。视口、DPR、参数、帧计数和原始测量均在 JSON 记录中。

桌面观测 14,190 个采样点；工具包 Demo 的 14,300 点依赖其容器尺寸，不要求不同主区域逐点一致。最终引擎约 2.08 秒短采样的绘制时间中位数 5.1ms、p95 5.8ms，仅代表引擎绘制采样。`performance-comparison.json` 另记录原版构建与新版构建的输入、滚动和四路由操作；该测量包含 Playwright 开销，不能当作 INP、稳定 FPS 或统计显著的性能结论。

DPR=2 时，1220×900 CSS 主区得到 2135×1575 位图，确认 1.75 上限。3,362,625 像素的单份 RGBA 数据约 13.45MB，未包括浏览器的额外缓冲、合成和模糊开销。内部长页不会继续增大该位图。

动画增加 CPU 绘制工作；本轮既定交互检查均完成，但不承诺无性能影响、全设备 45 FPS 或低功耗。上限、密度、亮度与波纹参数均未因测试而降低。

最终构建对照的 12 次输入操作耗时范围：原版 3.18–21.12ms，新版 3.74–16.26ms；这些数值含自动化开销，不能据此宣称性能提升。该短运行的主线程 TaskDuration 原版为 0.144s，新版为 1.054s；新版观察到一次 57ms 长任务，原版为零，尚未将其单独归因到绘制、玻璃合成或业务渲染。此项保留原始数据，未以“无长任务”或“零性能成本”结案。

## 截图与证据位置

仓库证据根目录：`/Users/xuyang/Documents/GitHub/campus-gpu-fabric-cp4-b/evidence/cp4/starwave/`，按仓库既有规则不纳入 Git。选定证据另复制到：

`/Users/xuyang/Downloads/黑客松/CP4_Starwave_接入验收_20261114/`

- `screenshots/desktop-1440x900.png`：真实 CP4 工作台，合成业务数据。
- `screenshots/mobile-390x844.png`、`mobile-scrolled-390x844.png`：390px 首屏与滚动后真实截图。
- `screenshots/ripple-1440x900.png`：真实点击后波纹。
- `screenshots/long-page-scrolled-ripple-1440x900.png`：长页坐标与边界。
- `screenshots/resources-1440x900.png`：表格及业务状态颜色。
- `baseline/`：原版截图与检查；`browser/`、`production-preview/`、`supplemental/`：最终运行的原始截图与 JSON。
- `final-cp0.log`、`final-client.log`、`source-parity.json`、`performance-comparison.json`：检查日志与参数证据。
- `manifest.json`、`integration.patch`：实际提交号、文件清单和可审阅补丁。

截图是浏览器直接捕获，不是重绘、图片生成或 Demo 整页替换。它们验证视觉与前端操作，不证明正式业务执行。

## 未验证项

- 原生后台标签页暂停：独立 headed Chrome 尝试切页后两页均返回 visible/hasFocus=true，未触发所需条件。原始补充报告为 FAIL，并明确原因是 NOT_VERIFIED；不把它改写成通过或断言为引擎缺陷。受控 visibility 单测已经通过，两者分别记录。
- macOS 原生减少动态开关、Safari、Firefox、实体手机触摸/系统键盘、屏幕阅读器、浏览器实际缩放、BFCache 恢复。
- 不支持 backdrop-filter 的旧浏览器（保留局部 CSS 可读性降级，未在该类浏览器运行）。
- 视觉引擎构造中后期任意异常的事务式清理、长时间功耗、持续高负载及超大视口。
- 正式后端、真实 GPU/任务、支付及生产部署；本次没有要求或执行。

## 本地复核与回滚

复核命令在目标仓库执行，沿用项目 Node 22 与 Python 3.12/uv 环境：

```sh
make cp0
npm --prefix web run test:client
npm --prefix web run dev -- --port 15186
```

浏览器脚本使用独立工具目录的 `playwright-core`，不加入产品依赖。默认位置为 `runtime/starwave-browser/node_modules/playwright-core/index.mjs`，也可通过 `PLAYWRIGHT_CORE` 指定；`CHROME_PATH` 可指定浏览器。脚本只接受 loopback 地址，并拦截业务 API：

```sh
STARWAVE_URL=http://127.0.0.1:15186 node web/tests/starwave.browser.mjs
STARWAVE_URL=http://127.0.0.1:15187 STARWAVE_PREVIEW=1 node web/tests/starwave.browser.mjs
STARWAVE_URL=http://127.0.0.1:15186 STARWAVE_EXTRA=1 node web/tests/starwave.browser.mjs
```

preview 命令需先构建并启动 `npm --prefix web run preview -- --port 15187`。这些命令是测试复现，不应将开发代理指向生产服务。交付时另提供仅静态页面的本地预览，不附带业务后端。

快速关闭视觉层：将 URL 改为 `http://127.0.0.1:15190/?visual=classic#run` 并重新打开。Starwave Canvas 不再创建，原 CP4 配色与文档滚动恢复；该路径已经在浏览器中验证。

代码回滚：先保存当前工作区中后来产生的个人改动，核对 `git status`，再对本次独立提交新增撤销提交：

```sh
git show --stat backup/cp4-starwave-20261114
git revert backup/cp4-starwave-20261114
```

撤销后重新运行前端测试、构建，检查六路由、表单、无残留 Canvas。不要 `reset --hard`、`clean` 或强推；不要撤销基线以后的其他业务提交。如果本次集成后来以不同提交号合入另一分支，应撤销那一笔实际集成提交。

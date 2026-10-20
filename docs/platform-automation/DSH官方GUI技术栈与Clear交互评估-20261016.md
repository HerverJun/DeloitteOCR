# DSH 官方 GUI 技术栈与 Clear 交互评估

核对日期：2026-10-16。官方基线为 `0.2.1-alpha.4`、提交 `86a402200d95fecef4529d969836f1fe13c6cb0d`。本次官方 master 查询也指向这一提交。以下为源码审查与改进建议，不是界面改版完成声明。

## 官方实际技术栈

| 层次 | 官方实现 |
|---|---|
| Web | React / React DOM 18 系列、TypeScript、Vite；入口依赖声明分别为 `^18.2.0`、`^6.0.3`、`^6.4.3` |
| 桌面 | Electron 44 系列外壳，承载同一套 Web UI |
| 状态 | Zustand vanilla `~4.5.7`、Immer `^10.1.1`、use-sync-external-store；部分通知按 requestAnimationFrame 批处理 |
| 样式 | CSS Modules、clsx、统一 `--dsw-*` 语义令牌、明暗主题与表面层次 |
| 组件 | 自研 ui-primitives、ui-chat、ui-conversation、ui-tool、ui-dockkit 等模块 |
| 动效 | CSS transitions/keyframes、原生 Web Animations API，以及协调折叠和滚动的 requestAnimationFrame 时钟 |
| 输入 | Lexical `^0.49.0`，支持引用 chip、命令、草稿和历史 |
| 大会话 | TanStack React Virtual `3.14.10` |
| 内容 | micromark/mdast/GFM/数学扩展，Shiki、KaTeX 等 |

官方样式规范明确要求 CSS Modules 和 clsx，并禁止增加组件库或 Tailwind。核对的依赖及锁文件没有 Framer Motion、Motion、GSAP、React Spring、Ant Design、MUI、Radix 或 Tailwind。

来源：

- [Web 入口依赖](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/apps/web/package.json)
- [桌面入口依赖](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/apps/desktop/package.json)
- [官方样式规范](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/docs/web-styling.zh.md)
- [主题设计](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/packages/client/ui-theme/README.zh.md)

## 官方质感来自哪些具体实现

1. **列表变化具有连续性。** AnimatedRows 对新增、移除和重排分别处理；新增约 100ms，重排约 200ms。首次历史渲染和视图切换不重播动画，遵守减少动效偏好。
2. **执行过程逐渐收起。** 工具过程使用紧凑披露行，当前内容与已完成内容有不同权重。图标、悬停展开箭头和相邻内容排布使用短过渡。
3. **折叠与滚动共同协调。** flow-motion 使用同一个帧时钟改变折叠几何和滚动位置，读者主动操作可取消自动落点；不是简单给每行加 CSS 动画。
4. **输入区拥有独立层次。** 统一输入表面承载草稿、引用与主要动作，辅助信息渐进披露。
5. **材质与动效有共同规则。** 主题提供 100/200/300ms 时长阶梯、缓动、圆角、浅投影和菜单表面；状态文字的轻量 shimmer 也属于同一体系。
6. **工作台支持空间操作。** Dockkit 是官方自研的分栏/标签/浮动窗口布局引擎，拖拽目标与分隔线有明确反馈；它不是一个替换 CSS 即可得到的第三方皮肤。

可复核源码：

- [AnimatedRows](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/packages/client/ui-workspace/src/client/rows/AnimatedRows.tsx)
- [flow-motion](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/packages/client/ui-chat/src/client/chat/flow-motion.ts)
- [DisclosureRow](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/packages/client/ui-primitives/src/DisclosureRow.module.css)
- [TextShimmer](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/packages/client/ui-primitives/src/TextShimmer.module.css)
- [Dockkit](https://github.com/deepseek-ai/deepseek-harness/blob/86a402200d95fecef4529d969836f1fe13c6cb0d/packages/client/ui-dockkit/package.json)

## Clear 当前诊断

Clear 使用 React 19、TypeScript 5.9、Vite 8、Ant Design 6.6、TanStack React Query 和自有 CSS。此前接入 DSH 的是服务端运行内核；当前门户仍使用 Clear 的组件、状态与事件投影。

| 当前表现 | 对应源码 | 改进方向 |
|---|---|---|
| 多种执行阶段共享三点和文字，主要动态仅为三点脉动 | `RunStatus.tsx`、`agent.css` | 紧凑阶段栏呈现真实阶段、计时和关联作业；暂停/待核实显示原因与恢复动作 |
| 工具过程默认平铺，已完成与执行中条目同权重 | `ToolActivity.tsx` | 已完成过程折为摘要，当前项和错误展开；操作身份与参数继续留在诊断层 |
| 状态、上下文、暂停、停止、取消、接管和整理堆在输入区 | `AgentWorkbench.tsx` composer | 统一输入容器，资料 chips、轻量状态摘要和明确主动作；次级控制集中披露 |
| 宽屏计划与当前作业也只能打开抽屉查看 | `AgentWorkbench.tsx` task Drawer | 宽屏可折叠任务侧栏；窄屏保持抽屉，突出目标、计划、作业和阻碍 |
| 新消息、状态替换和列表增减直接出现 | 消息/工具组件及样式 | 新内容首次出现时短淡入与小位移；状态切换柔和过渡，历史恢复不重播 |
| 自动追随主要直接写 scrollTop | `useConversationScroll.ts` | 保持现有手动阅读保护与锚点，按帧协调新增内容、折叠和尾部跟随 |

现有滚动代码已经区分“追随最新”与“手动阅读”，也实现了历史阅读锚点。后续不能为了动画破坏这些行为。动效需尊重 `prefers-reduced-motion`，不逐 token 制造打字效果，不制造假进度，不重复播放历史消息。

## 适合 Clear 的实施顺序

1. 输入区与执行阶段栏：先降低同屏控件的竞争，保留当前绿色配色。
2. 工具过程渐进折叠：把运行中的当前项、错误、作业等待与已完成摘要分清。
3. 宽屏任务侧栏：让目标、计划、作业和交付条件持续可见。
4. 统一 120/180/240ms 级别的短过渡、列表变化和阅读滚动协调。
5. 最后评估引用输入、长会话虚拟列表和可停靠工作台，按实际会话长度与业务需求引入。

采用现有 React 技术路线即可完成前四项。更换 React 版本、状态库或整套 UI 框架不会自动产生官方的交互细节。官方完整 GUI 还依赖 Cordis 前端插件、插槽、客户端状态和会话投影；整体移植需要单独适配 Clear API、SSE、任务权限、资料与成果引用，不应当作安装一个 UI 包处理。

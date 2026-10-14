# 视觉美感、版式与设计一致性审查

审查日期：2026-09-12。对象：当前工作区 `C:/Users/A/Desktop/OCR`，包含当前尚未提交的功能修复。仅审查并新增本报告，未改动产品代码。

采用 `C:/Users/A/.codex/skills/redesign-skill/SKILL.md` 的审查维度，但按 Windows 离线 OCR 校对工具取舍：优先阅读、任务上下文、编辑空间和状态辨识，不建议营销站式大标题、滚动动画、纹理或换用陌生字体。既有设计规范明确以中文表格、凭证和长编号为主要场景。

证据边界：本报告为当前源码审查；未操作浏览器，也没有把历史截图作为当前界面的验证结果。涉及实际裁切和可见行数的项目明确标为待运行验证，主代理统一补充当前浏览器证据。P1 表示阻断主要任务；P2 表示明显影响使用效率或可靠理解；P3 表示设计维护与一致性改进。本次静态审查没有足够证据认定 P1。

## 1. P2：分栏工具条按窗口宽度响应，忽略用户实际分配的面板宽度

- **性质**：确定存在约束冲突；实际按钮裁切范围待浏览器验证。
- **证据**：[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:949) 在整个窗口宽度至少 1200px 时强制表格命令和六个操作按钮不换行，每个按钮最小宽度 42px；[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:325) 却允许结果面板缩至 340px。[TableEditor.tsx](C:/Users/A/Desktop/OCR/frontend/src/TableEditor.tsx:75) 在同一行还放了表格选择、单元格地址和六个操作。六个按钮已占至少 252px，再加选择框、地址、间距与左右内边距，超过面板最小宽度。上方页签与撤销/复制也仅在整个窗口小于 1200px 时允许换行（[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:774)）。
- **场景与影响**：1200–1366px 宽窗口保留资料栏，或把原图分栏拖到 65%，右侧仍被视作“大屏”，有被裁切、文字挤压或工具难以触达的风险。面板设置 `overflow: hidden`，仅检查页面有无横向滚动可能漏掉问题。
- **建议**：以结果面板宽度作为响应依据，可使用容器查询。先保留表格选择与合并/拆分等核心动作，空间不足时允许独立工具行或明确的更多菜单，避免继续压缩字号和点击目标。
- **验证**：在 1200×800、1366×768 下分别使用 30%、48%、65% 分栏；对每个按钮测量边界是否完整位于结果面板内，并通过鼠标和键盘执行；不能只断言 `document.scrollWidth <= innerWidth`。

## 2. P2：中小窗口把当前文档身份从工作区隐藏

- **性质**：确定的呈现行为；对专业校对是否可接受属于产品设计决策。
- **证据**：[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:771) 在窗口宽度小于 1200px 时直接隐藏 `.result-filename`；[WorkspaceLayout.tsx](C:/Users/A/Desktop/OCR/frontend/src/WorkspaceLayout.tsx:36) 在该尺寸首次打开默认收起资料栏，从宽窗口缩小时也自动收起；[App.tsx](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:689) 的结果文件名是工作区内显示当前文件名的地方。原图标题是固定的“原始文档”（[ImageCanvas.tsx](C:/Users/A/Desktop/OCR/frontend/src/ImageCanvas.tsx:166)），面包屑显示的是项目名。
- **场景与影响**：1100×750 校对多个相似凭证或批量识别结果时，关闭队列后用户看到项目名、引擎和“校对结果”，却看不到当前具体文件，核对和导出前还要额外展开资料栏确认。
- **建议**：在收起资料栏和单区模式下始终保留文件名，可以用文件名替换固定标题，配合中间省略、完整名称提示以及上一张/下一张位置。可隐藏次要面包屑，避免隐藏文档身份。
- **验证**：1100×750、900×768、1093×614，使用前缀相同而末尾编号不同的长文件名，关闭资料栏与队列后仍能区分当前文档，并能查看完整名称。

## 3. P2：窄窗口首次使用的视觉引导跳过“导入”步骤

- **性质**：确定的初始状态与文案不匹配。
- **证据**：[WorkspaceLayout.tsx](C:/Users/A/Desktop/OCR/frontend/src/WorkspaceLayout.tsx:48) 默认显示校对单区，宽度小于 1000px 或高度小于 680px 时生效；资料栏在中小窗口默认收起。导入按钮位于侧栏（[App.tsx](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:552)），原图空状态的拖入指引在隐藏面板中（[ImageCanvas.tsx](C:/Users/A/Desktop/OCR/frontend/src/ImageCanvas.tsx:332)）。当前可见校对空状态提示“选择识别方式后点击「开始识别」”（[App.tsx](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:819)），而没有图片时此按钮被禁用（[RecognitionBar.tsx](C:/Users/A/Desktop/OCR/frontend/src/RecognitionBar.tsx:128)）。
- **场景与影响**：新用户以小窗口启动，看到的主体指引要求进行尚不能执行的动作；真正第一步隐藏在只用图标表达的资料栏开关之后。
- **建议**：按“无图片 / 有图片未识别 / 正在识别 / 识别失败”组织空状态。无图片时在当前可见区域直接显示“导入图片”主按钮和拖入指引；有图片后再显示开始识别指引，并复用现有导入逻辑。
- **验证**：清空 UI 偏好后以 900×768 和 1093×614 打开空项目；无需猜测侧栏图标即可从主体完成导入。使用已有图片、失败任务和正在识别任务分别验证文案及主动作。

## 4. P2：小笔记本的空间优先级仍偏向界面说明，而非连续校对

- **性质**：设计判断；源码能确认尺寸分配，实际可见行数需要当前浏览器测量。
- **证据**：[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:835) 在高度不超过 800px 时仍为工作标题保留至少 76px；[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:867) 的队列占 150px 加 10px 间隔。识别后自动打开队列（[App.tsx](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:326)）。结果区还包含标题、引擎选择、页签、表格命令、设置和最少 60px 的导出栏（[style.css](C:/Users/A/Desktop/OCR/frontend/src/style.css:2001)）。[WorkspaceLayout.tsx](C:/Users/A/Desktop/OCR/frontend/src/WorkspaceLayout.tsx:50) 在高度低于 680px 时不论窗口多宽都强制单区。
- **场景与影响**：1366×768 完成识别后队列继续展开，用户需要频繁滚动才能核对连续表格；较宽但较矮的窗口失去并排对照，只能在原图和结果间切换。现有回归接受表格编辑区域只剩 90px（[ui-redesign.mjs](C:/Users/A/Desktop/OCR/frontend/scripts/ui-redesign.mjs:201)），这个底线保证了“存在”，不足以说明校对舒适。
- **建议**：加入紧凑工作模式，把固定工作标题与项目上下文合并一行，识别完成且无失败项后将队列收为摘要；给用户保留并排/单区的手动选择。以至少连续展示若干完整行作为空间目标，在实际任务上确定数量。
- **验证**：1366×768、1920×650、Windows 125% 缩放等效尺寸；用 20 行凭证表记录展开/收起队列时完整可见行数、校对 10 行的滚动与切换次数。避免只用元素存在或 90px 高度作为通过标准。

## 5. P2：识别质量信息没有形成可扫描的视觉校对层

- **性质**：产品视觉设计提升方向，不等于 OCR 结果错误。
- **证据**：[types.ts](C:/Users/A/Desktop/OCR/frontend/src/types.ts:1) 已允许单元格带置信度与坐标；[TableEditor.tsx](C:/Users/A/Desktop/OCR/frontend/src/TableEditor.tsx:223) 的单元格仅按当前选择添加样式，没有置信度或待核对提示。文字块的置信度只在默认折叠的“定位文字区域”中用百分比显示（[App.tsx](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:894)）。主编辑区的高、低置信结果在视觉上相同。
- **场景与影响**：长编号、金额、手写资料中，用户需要逐格查找可疑内容，无法快速确定应先核对哪里。当前设计已具备原图/结果双栏，但质量信息尚未帮助用户安排注意力。
- **建议**：为确实提供置信度的引擎增加可开关的“待核对”标记、数量和下一处定位，使用低饱和警示底色加符号/文本，避免只靠颜色；缺失置信度明确视为未知，禁止当成零分或伪造统一准确率。不同引擎的分数不要直接设为可横向比较的质量排名。
- **验证**：使用带高/低/空置信度的受控结果，检查可疑项可被定位、标记不覆盖原文、选择态与提示态仍可区分；无置信度引擎不产生虚假警告。再用真实凭证确认标记密度不会淹没正文。

## 6. P3：设计系统依赖多层覆盖，视觉调整难以预测

- **性质**：确定的设计实现债务；没有据此宣称当前所有样式冲突或不可用。
- **证据**：[main.tsx](C:/Users/A/Desktop/OCR/frontend/src/main.tsx:5) 同时加载基础样式、Fluent 主题和后置精修样式。[style.css](C:/Users/A/Desktop/OCR/frontend/src/style.css:1207) 已追加一套品牌变量与组件规则，[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:9) 再覆盖相同变量；Fluent 主题仍使用另一组正文/辅助/边界值（[theme.ts](C:/Users/A/Desktop/OCR/frontend/src/theme.ts:49)）。同一张表格工具按钮的最小宽度与字号先后出现多次 `!important`（[style.css](C:/Users/A/Desktop/OCR/frontend/src/style.css:1923)、[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:930)、[professional.css](C:/Users/A/Desktop/OCR/frontend/src/professional.css:957)）。
- **场景与影响**：新增对话框、修复响应式或调整密度时，开发者很难从一处变量推导最终结果；原生控件、Fluent 控件和自制编辑表格容易逐步出现不同的颜色、尺寸与断点行为。现在的浅绿色灰差距较小，问题主要在后续可维护性。
- **建议**：整理单一的颜色、字号、控件高度、间距和层级来源，让 Fluent theme 与 CSS 同源；按“布局 / 通用控件 / 工作区组件 / 响应式”合并有效规则，删除已被完全覆盖的历史声明。保留现有品牌、字体与稳定交互，做渐进整理。
- **验证**：对导入、表格、文字、对比、空状态、错误和导出对话框留存同尺寸前后截图；抽查字体、焦点、控件高度和颜色的 computed style，并运行既有构建与 UI 回归。

## 已有优点

1. 黑色品牌区、浅色原图画布与白色编辑面板有清晰的任务区分；绿色集中在关键操作和选择态，整体适合严肃的离线文档工具，无需重新发明品牌视觉。
2. 本地打包 Noto Sans SC，正文与表格保持 14px、辅助信息至少 12px；表格数字启用 tabular-nums，长编号保持文本且不自动换行，符合实际业务阅读需求。
3. 原图/结果分栏支持拖动和键盘调整，资料栏与分栏偏好可保留；隐藏面板保持挂载，兼顾布局变化与编辑状态保存。
4. 图标线宽已统一，原生按钮与表单有明确焦点样式；深色侧栏另设较亮焦点色；尊重 prefers-reduced-motion。静态审查没有把深色侧栏焦点误判为使用浅底绿色。
5. 表格提供固定行列头、A1 地址、选择态和聚焦态；宽表格在编辑区域内部滚动，导出操作位置稳定。
6. 表格详细说明和低频图像操作采用渐进展开，已减少工具拥挤；保存失败、版本不匹配、空状态、拖入状态等也已有明确视觉结构。

建议执行顺序：先补足首次使用与文档身份，再验证并修复按面板宽度响应的工具条；随后优化小屏编辑密度和质量提示；最后整理设计 token 与覆盖层。

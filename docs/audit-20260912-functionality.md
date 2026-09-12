# 功能与数据正确性审查 · 2026-09-12

审查对象是当前工作区源码，包括尚未提交的修改。已先阅读《功能审计修复说明-20260912》，下面不重复其中已修复的六项问题。此次只读产品代码，使用独立临时数据执行定向复现，没有加载 OCR 模型、操作浏览器或修改既有项目。

结论：当前已经具备完整的图片导入、不可变版本、任务队列、校对历史和多格式导出主链路。下一步最值得补强的是“识别已有部分结果时如何继续工作”和“交付文件如何追溯原图”。本次发现 3 项确定缺陷、3 项能力提升方向，未确认必须立即停止使用的 P1 问题。

## 1. P2 · 确定缺陷：表格解析或附带 Excel 失败，会使已识别的整页结果不可用

- **位置**：[worker.py:202](C:/Users/A/Desktop/OCR/src/ocr_workbench/worker.py:202)、[worker.py:227](C:/Users/A/Desktop/OCR/src/ocr_workbench/worker.py:227)、[worker.py:231](C:/Users/A/Desktop/OCR/src/ocr_workbench/worker.py:231)。
- **触发**：模型已经返回普通文字和一个截断 HTML 表，例如 `Recognized invoice 123\n<table><tr><td>001`；或返回了有效表格，但某单元格超过 Excel 的 32,767 字符上限。
- **影响**：解析、附带 Excel 写出都先于正式 `result.json` 发布。前者抛错时连 `result.txt` 都没有，后者即使已写出 TXT，也不发布正式结果；队列把整项记为失败，界面无法打开已有文字进行校对或导出。`raw.json` 仍在磁盘，因此这是可用性与恢复路径缺陷，不能描述成原始模型数据已经永久丢失。重试同一引擎可能反复得到同一失败结果。
- **建议**：把可用的原始文字、区域、模型输出作为独立结果先持久化；表格解析失败保存明确警告与失败片段。把 Excel 兼容性检查留到用户选择 Excel 时执行；可选文件失败不应使识别结果消失。允许用户导出 TXT/JSON 后继续修复表格。
- **验证**：已用当前 `process_image` 和合成会话分别复现以上两种输入，无模型推理。两种结果均 `raw_preserved_on_disk=true`、`result_json_published=false`。修复后应验证部分成功状态、原文字可见、原始 JSON 可导出，以及 Excel 失败不会导致重跑 GPU。

## 2. P2 · 确定缺陷：一次结果读取失败后，界面不会随正常轮询恢复

- **位置**：[useEditor.ts:64](C:/Users/A/Desktop/OCR/frontend/src/useEditor.ts:64)、[useEditor.ts:73](C:/Users/A/Desktop/OCR/frontend/src/useEditor.ts:73)、[App.tsx:183](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:183)、[App.tsx:811](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:811)。
- **触发**：首次打开一个已有成功结果的图片时，`GET /results/:id` 短暂失败，然后服务恢复。这里无需发生保存冲突。
- **影响**：加载前已清空 `result/edit/current`；失败后只显示错误提示和“识别结果将在这里显示”。项目仍每 1.2 秒刷新，但结果加载 effect 仅依赖 `active/currentResult`，这两个 ID 未变化就不再请求结果。空态还建议用户“开始识别”，容易让用户重复消耗推理时间。重新加载方法依赖 `current.current?.id`，此时也没有目标 ID；用户需切换到其他图片再回来，或从队列“查看”显式触发加载。
- **建议**：区分未识别、加载中和加载失败；保留请求的结果 ID，提供“重试加载”。对瞬时读取错误使用有限次数重试或连接恢复后的刷新，保持导出/编辑依赖明确的成功加载状态。
- **验证**：已直接转译当前 `useEditor.ts`，用最小 hook 状态容器与一次失败的 API 复现：失败后结果为空；调用 `reload()` 请求数仍为 1；显式再次 `load(id)` 后请求数为 2，结果恢复。这个证据是 hook 状态路径验证，不是浏览器回归。修复应另加真实界面测试：让第一次 GET 返回 500，下一次成功，验证无需重跑 OCR 即可恢复。

## 3. P2 · 确定缺陷：普通竖线文字被误判成表格，复制/TXT 会改写内容

- **位置**：[tables.py:108](C:/Users/A/Desktop/OCR/src/ocr_workbench/tables.py:108)、[tables.py:113](C:/Users/A/Desktop/OCR/src/ocr_workbench/tables.py:113)、[worker.py:202](C:/Users/A/Desktop/OCR/src/ocr_workbench/worker.py:202)。
- **触发**：识别文本有独立一行 `|x|`，或者首尾为竖线但没有 Markdown 表头分隔行的普通内容。
- **影响**：解析器只看首尾竖线就创建结构化表格，并未要求 Markdown 表格的分隔行。所有引擎都经过此解析，包括普通文字引擎。当前 TXT 导出会按表格来源范围替换，`Absolute value:\n|x|\nEnd` 实际变成 `Absolute value:\nx\nEnd`，合法文字中的竖线消失。复制使用相同 TXT 导出，因此同样受影响。原始 text/JSON 仍保留输入。
- **建议**：标准 Markdown 表格至少要求合法表头分隔行和一致的列结构；对引擎的非标准表格输出使用明确的引擎适配规则或候选状态，避免将所有文本中的竖线自动视为结构。保留不满足结构条件的原文。
- **验证**：已直接调用当前 `parse_tables` 与 `export_text`，上述输入产生 1 张表并丢失竖线。应补充绝对值表达式、普通分隔符、代码文字、合法 Markdown 表格以及转义竖线测试。

## 4. P2 · 能力提升：模型比较缺少同条件分组与结构化表格差异

- **位置**：[App.tsx:168](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:168)、[App.tsx:201](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:201)、[App.tsx:963](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:963)、[App.tsx:967](C:/Users/A/Desktop/OCR/frontend/src/App.tsx:967)。
- **场景**：同一图片反复识别、裁剪后再次识别，或用户只在“表格”页校对单元格后打开比较。
- **现状与影响**：比较收集该图片全部成功任务，没有按图像版本和批次分组；候选只展示 `edited.text`，差异也只检查这段文字。结构化表格的单元格文字、行列数和合并跨度没有比较界面。两个候选即使表格校对内容不同，也可以显示“文字完全一致”，这句话在纯文字意义上成立，却不足以帮助用户选择要导出的表格。历史版本混入也会削弱引擎比较的公平性。
- **建议**：默认比较同图像版本、同批次的引擎结果，并显示版本/时间；增加按单元格匹配的文字与合并结构差异，允许选择基准结果。当前按同序号逐行比较的算法也可升级为对齐差异，减少插入一行后整页显示差异的噪声。
- **验证方式**：构造两次识别批次与两个图像版本，再构造 `edited.text` 相同但单元格值/合并跨度不同的结果；确认默认分组一致，表格差异可见，采用后实际导出与选中候选一致。本项依据代码审查，未运行浏览器比较测试。

## 5. P2 · 能力提升：批量 Excel 导出缺少原图与结果来源映射

- **位置**：[exporting.py:69](C:/Users/A/Desktop/OCR/src/ocr_workbench/exporting.py:69)、[exporting.py:83](C:/Users/A/Desktop/OCR/src/ocr_workbench/exporting.py:83)、[tables.py:135](C:/Users/A/Desktop/OCR/src/ocr_workbench/tables.py:135)。
- **场景**：把几十张凭证交给其他人，或一张图片含多个表格，随后需要核对某工作表对应哪张原图、哪次识别和哪一版校对。
- **现状与影响**：逐图 ZIP 仅使用 `0001.xlsx` 等数字文件名，汇总文件仅使用 `Table 1` 等工作表名，没有来源索引。汇总时 `yield from` 还直接抹去了表格的图片分组边界。内容没有遗漏，但交付文件无法独立说明“来自哪里”，接收者需回到应用手工对照。单独选择完整 JSON 可以保留来源，但当前 Excel 交付没有附带它。
- **建议**：保留现有安全数字文件名，同时在 ZIP 增加来源清单，在汇总工作簿增加“来源索引”页：原图片名、图像版本、引擎/包版本、结果 ID、校对 revision、表格序号、工作表名。也可提供经清洗、去重的可读文件名选项。
- **验证**：已对两张具有不同原文件名的合成结果导出。ZIP 只有 `0001.xlsx/0002.xlsx`；汇总只有 `Table 1/Table 2` 和单元格值，无映射。修复应验证同名图片、非法文件名字符、多表图片和跨引擎采用结果都可准确追溯。

## 6. P3 · 能力提升：校对只能修已有表，无法手工补表或删除误判整表

- **位置**：[TableEditor.tsx:31](C:/Users/A/Desktop/OCR/frontend/src/TableEditor.tsx:31)、[TableEditor.tsx:43](C:/Users/A/Desktop/OCR/frontend/src/TableEditor.tsx:43)、[TableEditor.tsx:101](C:/Users/A/Desktop/OCR/frontend/src/TableEditor.tsx:101)。
- **场景**：模型漏掉整张表、把两个表识别成一个，或者把普通竖线文字误判成表格。
- **现状与影响**：无表时只有推荐其他引擎的空态；有表时只提供增删行列、合并拆分和标题编辑，更新函数只替换已有表。用户无法增加或删除整表，也无法把从 Excel 复制的一块 TSV 一次粘贴成单元格区域。复杂误识别只能重新推理或逐格绕行修复。
- **建议**：增加“新建表格”“删除此表”和 TSV 区域粘贴，并接入现有撤销/重做。新建表应明确来源区域或未绑定状态；删除表时明确原文如何处理，避免只移除结构而留下难以理解的重复文本。
- **验证方式**：零表结果建立表格后可保存、撤销、重做及导出；删除误判表可以恢复；多行多列粘贴保留前导零、长编号和公式样式文本。本项是范围扩展建议，不是已有按钮失效。

## 已有优点与建议顺序

- 原图与处理版本分离，EXIF、透明底和透视处理有对应测试，后续校对可以回溯输入。
- 任务持久化、重启后明确恢复、单 GPU 所有者、取消结果防迟到发布、失败释放引擎等边界已有实现与测试。
- 保存使用 revision 冲突检测，撤销/重做持久化；当前未提交修改已将保存、历史与重新加载串行化，并保护切图切项目。
- Excel 明确使用字符串单元格，保留前导零、长编号和合并关系；本轮此前修复已避免缺表汇总静默遗漏，并在失败时清理导出临时文件。
- HTML/Markdown 表格现已记录来源范围和摘要，混排替换和前后普通文字保留有专门回归。

建议先完成 1–3 的结果可恢复性与内容正确性，再实施 5 的交付追溯和 4 的比较能力，最后按用户的人工校对频率决定 6 的投入。

## 本代理验证产物

- [后端定向复现脚本](C:/Users/A/Desktop/OCR/build/audit-20260912-functionality/reproduce.py)
- [后端复现结果](C:/Users/A/Desktop/OCR/build/audit-20260912-functionality/backend-evidence.json)
- [编辑器状态复现脚本](C:/Users/A/Desktop/OCR/build/audit-20260912-functionality/reproduce-editor.cjs)
- [编辑器状态复现结果](C:/Users/A/Desktop/OCR/build/audit-20260912-functionality/editor-evidence.json)

后端使用 `E:/OCR-deloitte-build/bundle/runtimes/service/python.exe -B -X utf8`，入口显式优先导入当前仓库 `src`；前端脚本直接转译当前工作区 hook。合成会话只验证模型输出之后的真实处理路径，不代表重新测过 OCR 识别准确率、GPU 性能或真实浏览器行为。

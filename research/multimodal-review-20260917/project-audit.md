# 当前项目多模态二轮审校接入审计

审计日期：2026-09-17。仅阅读当前源码和本地质量报告，未执行模型、未修改产品代码。本记录区分既有事实与架构建议；模型和 GitHub 外部调研另见本轮总报告。

## 结论

当前项目已经具备多模态 OCR 和可追溯人工复核的基础。最有价值的新增能力是“看图、有依据、会弃权的局部审校”，以及“由已采用内容生成受约束导出规格”。无需把现有 OCR 全部换成通用视觉语言模型，也不宜让二轮模型的输出重新进入现有投票以形成循环证据。

建议把二轮模型作为独立 `review` 提供方：读取不可变图像、选中修订与原始候选快照，只产生带依据的建议。用户采用后通过既有修订/撤销链保存；导出继续由确定性代码完成。这个方向与项目的离线 Windows、单 GPU、来源保留和候选先审阅架构直接兼容。

## 已有能力与边界

| 方面 | 当前事实 | 对二轮模型的意义 |
|---|---|---|
| OCR 引擎 | `config/engines.json:2` 起列出 PP-OCRv6、PaddleOCR-VL-1.6、GLM-OCR、HunyuanOCR-1.5；后三者已是视觉语言/文档解析路线 | 新增通用 VLM 的价值应在审校、跨区域推理和输出语义，而不能只用“支持图片”证明必要性 |
| 部署 | Windows 离线便携包，目标 Xeon W5-2455X / 64GB / RTX A4000 16GB；单 GPU 独立模型进程；Hunyuan 已有 llama.cpp F16 GGUF + projector | 有模型隔离及 GGUF 先例；新模型仍须实际验证 Windows CUDA、视觉投影、量化与离线打包 |
| 原始与修订 | `Store.complete()` 保存 original 与 edited 两份，`Store.save()` 只更新 edited；有 revision 与撤销重做 | 二轮输出应另存，不覆盖 original 或既有人工修改 |
| 融合 | CPU 对齐与候选、同图同版本同批次校验；每引擎只能贡献一票；人工修订/融合输出不能计票 | 通用审校器不能作为独立 OCR 票再次背书自己看过的候选 |
| 审阅 | 融合疑点、结构建议、页面冲突、文档复核队列；决定绑定修订、版本、依据哈希；重复请求幂等 | 新审校建议可以复用交互模式与事务约束，但需要独立状态/来源表 |
| 结构 | Paddle/local-v2 默认；TableFormer raw/local-v3 实验；pdfplumber 候选显式采用；文字池和几何证据分离 | 可让 VLM 比较现有骨架，不能凭通顺文字或自由生成框代替结构证据 |
| 输出 | TXT/Markdown/JSON/XLSX；文档工作流支持可搜索 PDF、来源附件、预检 | 没有现成 Word 导出；增加 DOCX/模板化报告须明确为新功能 |

开发总纲中“融合尚未实现”和首版“不支持 PDF”等是历史计划描述；当前状态应以 README 的 0.10.0rc3、源码和最新文档为准。

## 精确接入点

以下行号对应本次读取的当前工作区，文件内容继续变化后应重新核对。

| 位置 | 现有职责 | 推荐接入方式 |
|---|---|---|
| `src/ocr_workbench/adapter.py:32` `EngineAdapter`；`:64` `load()`；`:159` `recognize()` | GPU 文件锁、隔离 runtime、IPC、取消卸载 | 增加独立审校 adapter / review request 契约；当前 recognize 接口主要接受图像，不是任意 prompt + 上下文的通用聊天接口 |
| `src/ocr_workbench/engine_host.py:18` `Resident`；`:73` `recognize()` | 引擎装载与统一调用 | 增加明确的 reviewer 分支和 schema 输出校验，避免当前 else 默认走 Hunyuan；锁定模型、runtime、prompt 版本 |
| `src/ocr_workbench/task_queue.py:179` `step()`；`:239` 引擎切换；`:276` region crop | 持久任务、顺序 GPU、区域裁剪 | 以 review kind 进入同一 GPU 所有权，按引擎分组审校；不与 OCR 并发争用 16GB 显存 |
| `src/ocr_workbench/store.py:425` `complete()`；`:476` `save()`；`:515` `history()` | 原始快照、修订与历史 | 新 `review_requests/proposals/decisions` 单独存模型提案；显式采用时沿现有 revision 与历史事务提交 |
| `src/ocr_workbench/fusion.py:55` `validate_sources()`；`:95` `choose()`；`:119` `location()` | 原始来源校验、建议、真实位置降级 | 输入可取原始候选及真实位置；严禁把 reviewer 建议伪装成 single-engine 来源或独立多数票 |
| `src/ocr_workbench/fusion_store.py:243` `decide_issue()` | 原子、幂等、版本绑定的文字候选采用 | 可复用其 revision/version/basis/current_fingerprint/request_id 守卫模式；不宜直接塞进 fusion 候选表冒充原始来源 |
| `src/ocr_workbench/review_issues.py:98` `apply_choice()`；`src/ocr_workbench/editing.py:7` `validate_edit()` | 对精确目标做 literal patch，验证表格跨度与边界 | 二轮模型返回 target + before + after + evidence IDs；确定性代码验证并应用，而非接收整篇自由重写 |
| `src/ocr_workbench/document_review.py:11` `document_review_queue()` | 汇总采用修订的结构、融合、冲突任务 | 增加 multimodal review 任务类别与拒答/不可定位原因；保留 `empty_means_correct=False` 语义 |
| `src/ocr_workbench/structure_store.py:43` `record_candidates()`；`:145` `refresh_proposals()`；`:318` `decide_structure()` | 固定文字池、结构候选、冲突、显式采用 | VLM 可评估候选与提出局部结构建议，仍须 token lineage、格拓扑、空值和人工绑定校验 |
| `src/ocr_workbench/geometry.py:125` `enqueue_geometry()`；`:324` `bind_manual()`；`:360` `geometry_view()` | 图像/修订绑定的定位与人工优先 | 先用可靠 crop；无可靠格框时退到行/表/全图，不将 VLM 坐标当准确检测框 |
| `src/ocr_workbench/table_tool.py:22` `prepare()`；`:81` `view()`；`:140` `retry_stage()` | 独立辅助任务、状态、配置绑定缓存、单独重试 | 是审校辅助失败不丢原结果、缓存变更作废、独立恢复的合适工程范式 |
| `src/ocr_workbench/exporting.py:13` `build_export()`；`:32` `capture_snapshot()` | 同一数据库快照的采用内容与来源导出 | 加入审校来源 sidecar；模板输出规格只能引用快照中的事实与目标 ID |
| `src/ocr_workbench/structure_export.py:30` `structure_sources()` | 候选、采用、决策可追溯导出 | 二轮审校应有对应 review_sources，记录模型/提示词/输入/建议/采用链 |
| `src/ocr_workbench/pdf_export.py:170` `capture_pdf()`；`:264` `build_pdf_export()` | 实际采用结果、定位、原生重叠/空区域确认预检 | VLM 不得自行解除 PDF 预检或把“我认为无误”写成人工确认 |
| `frontend/src/QuickReview.tsx`、`StructureReview.tsx`、`DocumentReviewQueue.tsx`、`RegionPreview.tsx` | 已有原图/候选/采用交互 | 可以新增二轮建议面板，显示原文、建议、原图证据、采用/保留/存疑，避免另建聊天主流程 |

## 必须保持的产品语义

1. **原文不覆盖。** 原图、图像版本、原始 OCR 和人工修订分开；审校失败不阻止已有结果可用。未经采用的建议不进入正式导出。
2. **依据不能循环。** 被 OCR 候选或融合结果提示过的 VLM 不是独立投票来源。重复 prompt、多次自我反思也不是独立证据。
3. **修订会过期。** model/prompt/runtime、image hash、crop、OCR snapshot、selected result revision、scope 内容哈希都绑定；用户编辑或采用另一版本后旧提案不可直接提交。
4. **数字不润色。** 金额、小数点、正负号、日期、编号、前导零、单位、空白应保持字面保真。算术或格式异常可以标记，不据合计反推并覆盖不可见字符。
5. **无证据就弃权。** 输出 `keep/correct/uncertain`，并给出已有 evidence IDs。解释不能充当图像证据；模型自报 confidence 不是校准概率。
6. **格式与事实分层。** 模型可建议章节、表头角色、模板字段映射、摘要、工作表名；事实层保留 adopted source 与 span/cell IDs。摘要是派生内容，不是原始识别转录。
7. **现有人工状态保留。** 现有功能含“人工确认”状态与显式采用操作，这是产品事实；不等于本次研究要求用户先执行额外人工验收才可继续。

## 质量现状与研发优先级

`docs/fusion-quality-report.md`：200 张均已有历史曝光，没有独立留出或真人效率结论。积极手写策略曾修正 3 处、引入 2 处，且改坏原本正确样本；两档/三类别均关闭自动替换。回顾性表格 +27 正确格全部来自一张表的完整骨架改选，而不是自动文字候选采用。不要据此预期“再加一个模型，多数投票会可靠变好”。

`docs/tableformer-next-20260915-quality.md`：新测试 120 文档 / 11,278 格，TableFormer-local-v3 的控制轨完整格覆盖 77.50%，实际采用结构轨 69.71%；实际合并格仅 43.75%，153/352 合并目标没有可关联采用输出。说明定位和结构仍是主要瓶颈，不能只在有框的容易格上评测二轮模型再外推全表。

`docs/structure-workflow-quality-20260916.md`：公开材料仅开发/工程，不是独立质量通过；真实人工效率 0 位参与者；导出与采用修订一致不表示采用内容符合原图。

`docs/public-quality-20260916.md` / `docs/generic-tool-fixes-20260917.md`：辅助工具失败、空区域、过期缓存和单独重试已被明确处理。二轮审校应沿这套辅助功能生命周期，不能把“返回 JSON”算作“内容通过校验”。

推荐顺序：

1. **P0：疑点局部视觉复核。** 用现有差异、金额/编号/日期、空区域和结构冲突触发。首阶段只给建议、显式采用。首选“原图独立转录，再比较候选”的两阶段实验，以测量候选诱导偏差。
2. **P0：确定性约束 + VLM 找依据。** 代码做列类型、字符串长度、加总、跨页字段一致性检查；VLM 负责定位、解释与有限候选，不执行自由脚本。
3. **P1：结构候选仲裁。** 输入固定 OCR token IDs、多种骨架及局部原图，输出候选排序/局部结构 patch；与原生 PDF 工具、TableFormer 能力互补。
4. **P1：输出规格助手。** 以严格 JSON 指定工作表、字段、列格式、排序、模板映射；代码从已采用快照生成 XLSX/Markdown/JSON，后续再扩 DOCX。复杂文件生成交给现有库，不让模型生成二进制或直接写工作区。
5. **P2：跨页语义整理。** 文档级条目合并、重复表头、单位继承、附注关联、带引用摘要；始终保留页码/格 ID 的反向跳转。

## 最小独立实验

候选路线须做同输入消融：现有 OCR 基准；纯规则标疑；纯文本小模型；图像小模型独立转录；图像 + OCR 候选二轮审校；相同 VLM 整页重识别。这样才能分辨收益来自视觉、语言先验还是多一次识别。

冻结新文档/模板组，排除历史所有公开实验材料。覆盖中文印刷、手写、金额/编号/日期、稀有姓名、化学/法律术语、重复值、合并格、空格、扫描与拍照、跨页和原生 PDF。把模型/quantization/prompt、图像尺寸和 crop 策略一起冻结；量化模型分别计分，不能只验全精度。

至少报告：修正错误数、引入错误数、净纠错、建议 precision/recall、弃权与覆盖、关键字段完全正确率、CER、结构/整表可用率、定位可得率、JSON 合法率、失败/OOM、加载与推理 P50/P95、VRAM，以及真实复核时间和最终残留错误。含失败/未触发/无可靠 crop 的全量分母，并以文档组做成对区间。模型自评与同模型复读不能充当真值。

推荐先在独立研究 runner 和保存提案 JSON 上证明净收益，再接产品队列及 GUI。当前审计没有执行任何新模型，因此性能、准确率提升和 16GB 可用性均尚未实测。

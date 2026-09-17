# GitHub 多模态 OCR 二轮修正与文件输出调研

调研日期：2026-09-17（Asia/Hong_Kong）。方法：读取官方 GitHub API、README、许可证、固定提交的实现代码；模型许可补充读取官方 Hugging Face API / model card。这里只验证「代码确实存在并如何工作」，未下载模型、未运行第三方项目、未在本项目数据上验证精度/速度。README 跑分不作本项目效果保证。`repo-index.json`、`source-index.json`、`supplemental-sources.json` 保存来源及快照，关键源码保存在各仓库目录。

最值得组合借鉴的是：**Marker 的“图像 + 现有结构 → 定位到块的修改”、Docling 的 bbox 裁图与结构回写、Scribe.js 的独立视觉比较、MinerU 的受限结构决策、olmOCR 的失败检测和回退**。它们解决不同问题；不能把模型的自评分、合法 JSON 或多模型一致，等同于原文准确。

## 精选项目及真实定位

| 项目 | 已核实的实现与作用 | 对本项目的借鉴方式 | 局限 / 许可 | 最近 push（API 快照，非稳定版本发布日期） |
|---|---|---|---|---|
| [Marker](https://github.com/datalab-to/marker) | 真正的二轮视觉修正：给整页图 + 块 JSON，可返回 `no_corrections`、块重写、顺序调整；表格有图 + HTML 校正；可接 Ollama / OpenAI-compatible | 复用思路：给 crop + 原 OCR + 稳定 block_id，让模型提出小范围 patch；表格与公式单独处理 | 默认模型/服务不代表小模型也有效；有格式检查但缺乏独立字面正确性验真。**当前代码 Apache-2.0**，模型权重 modified AI Pubs OpenRAIL-M，超 $5M funding/revenue 适用商业条款，不能混同代码许可 | 2026-09-13 |
| [Docling](https://github.com/docling-project/docling) | 官方已有 `post_process_ocr_with_vlm.py` 示例：在已生成 DoclingDocument 上逐 bbox / cell 裁图重识别并回写；统一文档树、多格式输入、Markdown/HTML/JSON 输出 | 最直接的 bbox → crop → VLM → 文档节点回写参考；先建立文档中间表示，再做多格式导出 | 该示例是**全量可处理裁图重识别**，不是低置信度保守纠错；默认只送图，不送原 OCR；MIT。Granite-Docling-258M 权重 Apache-2.0，但中文标记 experimental，不宜直接认定能胜任中文纠字 | 2026-09-16 |
| [Scribe.js](https://github.com/scribeocr/scribe.js) | 非 LLM：将候选词按字体/几何位置渲染，与原扫描图像比较；融合两路 OCR、保留 losing reading 为 alternative；可导出带不可见文本层的 PDF | 最有启发性的校验器：模型负责提出候选，另一路视觉/几何方法检验；保存候选来源和替代文本；输出可搜索 PDF | 字体、字距、倾斜和低质量扫描会影响像素比较；需要本项目中文字体实测。**AGPL-3.0**，直接集成前评估许可；思路参考不等于可任意复制代码 | 2026-09-13 |
| [MinerU](https://github.com/opendatalab/MinerU) | 文档解析和统一中间表示；当前实现有**后置文字 LLM**辅助标题层级、跨页表格 cell 续接；续接让 LLM 只输出 0/1 数组；中间表示可交给确定性 DOCX 渲染 | 学习“规则先筛候选 + LLM 只做有限决策 + schema 校验 + 程序输出文档”；适合跨页表格与标题层级 | 该辅助模块不是图像纠字。当前默认分支代码使用 **MinerU 自定义许可（Apache-2.0 + 附加条件）**，包括大型商业阈值、在线服务标识义务，不能只写 Apache 或沿用旧 AGPL 印象；不同模型另查许可 | 2026-09-16 |
| [olmOCR](https://github.com/allenai/olmocr) | 整页 VLM 文档识别；当前代码检查输出截断、结构字段、旋转，再重试；失败可回退 `pdftotext`，按失败页比例拒收整文档；另有细粒度文档单元测试基准 | 复用运行可靠性：超时/截断/重复/旋转/异常格式分开处理，保留 fallback 标记；学习以内容测试断言评估 OCR | **当前 v2 管线不再传 anchor 文本**；`anchor.py` 是可参考的历史机制，不应宣传成当前默认二轮 OCR 校验。代码与 olmOCR-2-7B-1025 模型卡均 Apache-2.0；7B 对小显存较重；`is_valid` 不是原文准确性证明 | 2026-03-25 |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | PP-StructureV3：版面、表格、公式、阅读序；PP-ChatOCRv4：OCR + 向量检索 + LLM/MLLM 字段抽取融合；现文档包含 Markdown/XLSX 保存和 DOCX 服务输出，README 也宣布 DOCX 支持 | 中文复杂表格/印章资料的结构化基线；借鉴字段抽取、图文融合和文件输出；PaddleOCR-VL 可作为另一专用 OCR 候选 | PP-ChatOCRv4 主要是**关键字段提取/问答**，不是逐字全文校对器。PP-StructureV3 也不是后纠错。代码 Apache-2.0，已核对 PaddleOCR-VL 模型卡 Apache-2.0；其它模型逐个确认 | 2026-09-16 |
| [GLM-OCR](https://github.com/zai-org/GLM-OCR) | 0.9B 专用多模态 OCR；源码为加载 → 版面检测 → 区域 VLM 识别的并行管线；支持本地模型服务 | 很合适作为小模型**独立重读者/争议区域候选生成器**，与通用 Qwen 的“校对决策者”分工；评测其不看 OCR 的独立转录能否减少锚定偏误 | 原项目并非保守后纠错器；0.9B 不能推出显存/速度结论。代码 Apache-2.0、模型 MIT、PP-DocLayoutV3 Apache-2.0，许可证分层 | 2026-04-21 |

## 重点源码核查

### 1. Marker：最接近图文二轮修改协议

固定提交：`8a1d2344de25d7ec4c5209133aed7af565874ff0`。

- [LLMPageCorrectionProcessor 的协议](https://github.com/datalab-to/marker/blob/8a1d2344de25d7ec4c5209133aed7af565874ff0/marker/processors/llm/llm_page_correction.py#L33)：输入 JSON 含 `bbox/id/block_type/html`，与原页图片共同送入模型，限制允许的 HTML 标签，要求忠实于原图。
- [真实请求与分支](https://github.com/datalab-to/marker/blob/8a1d2344de25d7ec4c5209133aed7af565874ff0/marker/processors/llm/llm_page_correction.py#L150)：`page1.get_image(..., highres=False)` 与带 `page_json` 的 prompt 一起调用 `self.llm_service(..., PageSchema)`；支持 `no_corrections/reorder/reorder_first/rewrite`。这不是单凭 README 的 `use_llm` 口号。
- [启用条件](https://github.com/datalab-to/marker/blob/8a1d2344de25d7ec4c5209133aed7af565874ff0/marker/processors/llm/llm_page_correction.py#L268)：通用 page correction 需非空 `block_correction_prompt`，不能推断仅开启 `use_llm` 就自动逐字校验全部正文。处理器遍历页面，不是根据 OCR 置信度选块。
- [表格区域校正](https://github.com/datalab-to/marker/blob/8a1d2344de25d7ec4c5209133aed7af565874ff0/marker/processors/llm/llm_table.py#L185)：原表格 HTML + 区域图，检查 `</table>` 和解析出的 cell 数；可循环修订，循环评分来自模型本身。该检查证明结构可用性，不证明每个数字正确。
- [本地 Ollama 实际载荷](https://github.com/datalab-to/marker/blob/8a1d2344de25d7ec4c5209133aed7af565874ff0/marker/services/ollama.py#L24)：`model/prompt/format/images`，展示本地 VLM 接入点；不能假定任意文本模型均支持图片。

建议：只借其协议与适配器设计，另加 `original_text/candidate_text/evidence_region/decision/reason` 的 patch 记录、不可修改字段策略、数字一致性检查、人工争议队列。不要直接把模型回复覆盖为唯一事实。

### 2. Docling：非常直接，但本质是区域重识别示例

固定提交：`d4fa979af44a878700f8576eaba7e27dd9330005`。

- [默认配置](https://github.com/docling-project/docling/blob/d4fa979af44a878700f8576eaba7e27dd9330005/docs/examples/post_process_ocr_with_vlm.py#L50)：LM Studio `http://localhost:1234/v1/chat/completions`，默认 `nanonets-ocr2-3b`；prompt 是从图提取纯文本并合并为一行，不是“比较这段 OCR 与图片并做最小修改”。
- [坐标和裁图](https://github.com/docling-project/docling/blob/d4fa979af44a878700f8576eaba7e27dd9330005/docs/examples/post_process_ocr_with_vlm.py#L271)：将文档 bbox 映射到渲染图大小，支持 Form/KeyValue graph cells、table cells、普通 text 多 provenance crop，排除空裁图。这对多页来源映射和保持表格结构很有用。
- [真正输入与门控](https://github.com/docling-project/docling/blob/d4fa979af44a878700f8576eaba7e27dd9330005/docs/examples/post_process_ocr_with_vlm.py#L465)：`is_processable` 仅返回 `enabled`；`api_image_request` 的输入是图片和固定 prompt，未把原 OCR 文本传给模型。没有置信度触发、分歧优先或字段风险门控。
- [写回及防护边界](https://github.com/docling-project/docling/blob/d4fa979af44a878700f8576eaba7e27dd9330005/docs/examples/post_process_ocr_with_vlm.py#L517)：去部分 HTML、合并换行、针对某一幻觉前缀清空、过滤超长重复字符，然后直接 `item.text = output` 并更新 charspan。不是独立真实性判定，也没有通用的纠错 patch 审批机制。
- [原结果保留](https://github.com/docling-project/docling/blob/d4fa979af44a878700f8576eaba7e27dd9330005/docs/examples/post_process_ocr_with_vlm.py#L577)：先保存带嵌入图片的 intermediate JSON，再读取并另存 final JSON，故可保留前后文档对比；不能简单说“完全不留原文”。示例并未实现逐修改原因/置信区间/人工批准的审计账本。

适配建议：沿用数据结构与裁图函数，把全量请求替换成低置信度/多引擎分歧/关键字段抽样；输出候选变化后由另一层决定是否接受。不要照搬其“一行纯文本”的默认 prompt 到保真 DOCX 流程，否则可能丢失需要保留的换行语义。

Granite-Docling-258M 是 Docling 支持的另一条小型视觉转录路径，不等同于上述默认 3B 示例。官方卡：[固定 revision](https://huggingface.co/ibm-granite/granite-docling-258M/blob/982fe3b40f2fa73c365bdb1bcacf6c81b7184bfe/README.md)。它输出 DocTags，可处理公式/表格结构；中文、日文、阿拉伯语标为 experimental，定位应是轻量对照实验，不能取代中文真实样本评测。

### 3. Scribe.js：独立于语言流畅性的视觉候选校验

固定提交：`6ff50c5d8f0d0434290c1a0736b73b7e74278bf5`。

[evalWords](https://github.com/scribeocr/scribe.js/blob/6ff50c5d8f0d0434290c1a0736b73b7e74278bf5/js/worker/compareOCRModule.js#L186) 裁取原词图并渲染候选 A/B，以像素重叠差异估算视觉误差；[compareOCRPageImp](https://github.com/scribeocr/scribe.js/blob/6ff50c5d8f0d0434290c1a0736b73b7e74278bf5/js/worker/compareOCRModule.js#L465) 可更新置信度、融合文本、保留失败候选的 `alt/source`。这是实际可读的视觉交叉检查实现，**不是**多模态大模型。

创新迁移：将 Qwen/GLM 提议的两个字词作为候选，由原图裁剪 + 字符几何 + 独立 OCR/候选图像相似度辅助判决。若字体/扫描质量不匹配，视觉误差只能作为特征，不作绝对裁决。该源码 AGPL-3.0，优先研究机制，独立设计本项目接口。

### 4. MinerU：受限 LLM 决策辅助文件输出

固定提交：`22f5abb681c2314feffb9353fdff919d52dd8369`。

- [编排器](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/backend/postprocess/llm_aided.py)：仅启用配置指定的标题层级与跨页表格续接；标题处理要求完整文档。
- [单元格续接](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/backend/postprocess/table_merge/llm_cell_merge.py)：先只收集确定性规则已标记 `continues_prev=True` 的相邻续表，提取上一页末行/下一页首行的对应单元格文字；请求等长 JSON 0/1 数组，拒绝非整数、非法值、长度错误；成功后只更新 `cell_merge` 属性，失败则跳过。
- [LLM 客户端](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/backend/postprocess/llm_client.py)：OpenAI-compatible、共享并发限制、最多 3 次重试、调用方 validator、剔除 thinking 前缀。该接口只发文字，不发图。
- [DOCX 门面](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/mineru/render/docx.py)：`MiddleJson` 交给 `docvortex.render.docx.render_docx`，由程序写 DOCX；这是“模型做结构决策，代码做文档生成”的明确实例。
- [当前许可证](https://github.com/opendatalab/MinerU/blob/22f5abb681c2314feffb9353fdff919d52dd8369/LICENSE.md)：Apache-2.0 加附加条件，包括 MAU > 1 亿或月收入 > $20M 须另取商业许可，以及面向第三方在线服务的显著标识义务。这里只记录文字，不作法律判断。

### 5. olmOCR：可迁移运行可靠性，但别把旧 anchor 当新默认

固定提交：`f7cfe4c22098b154c76b6ec950d1c0a464eecf8d`。

[build_page_query](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L106) 明确使用 `build_no_anchoring_v4_yaml_prompt()` + 整页图。模型返回的 YAML 前置字段包括旋转、语言、表格等。[try_single_page](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L148) 检查 token 上限和 `finish_reason`，并解析响应；[失败回退](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L233) 使用 `pdftotext`。扫描 PDF 没有可用文本层时，回退也未必能提供内容；本项目应保留本来的一轮 OCR，不能生搬其回退策略。

[bench/tests.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/tests.py) 将“包含/不包含某文字、阅读顺序、表格关系”等拆成测试项，是可借鉴的评测方式。`anchor.py` 可以研究坐标化文字证据的历史设计，但目前 CLI 已标 `target_anchor_text_len ... not used for new models`。

### 6. PaddleOCR：分清结构解析与字段抽取

固定提交：`dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf`。

[PP-ChatOCRv4 包装器](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/paddleocr/_pipelines/pp_chatocrv4_doc.py#L212) 暴露 `build_vector/mllm_pred/chat`，`chat` 默认启用 vector retrieval，可融合 MLLM 结果；调用面围绕 `key_list`，适合将票据/合同关键信息填入 schema。它不是“给所有 OCR 字符做最小修改”的接口。

[PP-StructureV3 官方使用文档](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/docs/version3.x/pipeline_usage/PP-StructureV3.en.md) 核实了 Markdown、XLSX 和 `outputFormats` DOCX 结果契约。精确生产接口应以安装版本为准，避免从 main 文档推断旧 pip 版本也支持全部功能。

### 7. GLM-OCR：小参数的第二观察者

固定提交：`cef4d0ea120d1741f5cefe8985eee45f6c8eff1d`。

[真实管线](https://github.com/zai-org/GLM-OCR/blob/cef4d0ea120d1741f5cefe8985eee45f6c8eff1d/glmocr/pipeline/pipeline.py#L1) 是加载页面、布局检测、区域 VLM 识别的有界队列并发处理，可替换 layout detector / formatter。这为“只给争议区域重读”提供实用的模型/服务参考，但现成项目没有替我们完成候选比较和审计逻辑。

模型卡 [GLM-OCR](https://huggingface.co/zai-org/GLM-OCR) 和仓库 README 核对到 0.9B 与模型 MIT 许可；此处未对 0.9B 的中文精度、显存或量化退化进行实测。

## 可形成差异化的组合方向（设计推导，非已经验证）

1. **识别器与裁决器分离**：传统 OCR 为主结果；小型专用 VLM 在不看原 OCR 的条件下独立重读疑难 crop；只有分歧进入图 + 原 OCR + 新候选的裁决步骤。避免模型顺着错误原文“确认”。
2. **受限修改协议**：只能对已有 block/行/单元格给 `keep/replace/uncertain` 与证据坐标；限定候选范围，保存原值、新值、触发原因、模型版本、原图哈希。错误处理保持一轮结果，额外标记待审。
3. **数值约束先行**：金额、日期、证件号、单位、表格合计、跨页一致性用确定性规则检验。规则冲突触发重读，不能让语言模型为了“总和合理”篡改原图实际写的数值。
4. **模型决策与渲染分离**：模型识别标题层级/表格续接/字段 schema；程序生成 DOCX/XLSX/可搜索 PDF；导出后再检查文本数量、字段缺失、表格结构和坐标映射。可借鉴 MinerU 与 Docling 中间表示，避免让 LLM 直接输出完整 XML 或二进制文件。
5. **评估净收益，而不只算修正数**：除 CER/WER 外，记录纠正错误数、引入新错误数、关键字段 exact match、自动接受覆盖率、需人工审核率、每页延迟/成本和资源峰值。特别保留“原 OCR 正确但二轮改错”的负样本。

## 资料边界

- 当前主分支随时变化；报告中的源码固定 SHA，README/API 元数据为采集时快照。近期开源活跃不意味着 API 稳定，也不是质量保证。
- 未成功的候选（例如 `ibm-granite/granite-docling` GitHub 路径返回 404）未作为项目引用；正确证据来自 `docling-project/docling` 与 IBM 官方 Hugging Face 模型卡。
- 独立后 OCR 纠错研究、评测集与 Qwen 模型型号核查由主报告另行汇总，避免把上述文档解析项目误称为专门的学术后纠错基准。

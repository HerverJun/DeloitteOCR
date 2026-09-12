# GitHub 相似 OCR 工作台调研

> 产品名称已统一为 DeloitteOCR。调研结论保留；后续开发范围、策略与排期以 [整体开发计划](../docs/DeloitteOCR-多引擎融合与快速校对开发计划.md) 为准。

调研日期：2026-09-12。对照对象为当前「DeloitteOCR」0.7.0rc2 源码及工作台使用说明。

**有多个高度相似的开源项目。PaddleOCR Local 最接近你的多模型处理架构，Folio-OCR 最接近你的三栏文档工作台，Umi-OCR 最适合参考 Windows 离线便携交付。** OctoOCR、Scribe OCR 则值得参考人工校对和表格交互。本轮未发现一个经公开资料充分证实，同时覆盖你全部需求组合的现成替代品；这不是对 GitHub 所有项目的穷尽结论。

本轮通过 GitHub API 进行 9 组关键词检索，筛选并读取 12 个仓库的元数据、README，对重点项目检查发布资产、路线图及部分源码。未安装运行这些应用，也未进行同样本识别准确率或性能测试。下述能力来自项目文档；有源码证据的地方单独注明。“未证实”不代表功能绝对不存在。Star 是查询时快照，不是质量或准确率评分。

**你的工作台实际对照基准**

根据 [README](../README.md)、[工作台使用说明](../docs/工作台使用说明.md) 和 [开发总纲](../开发总纲.md)，当前产品具有以下特征：

- Windows 单机，托盘启动器＋本机浏览器；React / TypeScript＋FastAPI / SQLite。
- 完整包携带 Python、运行库、模型及静态资源，目标机无需 Docker、WSL 或首次联网下载。
- PP-OCRv6、PaddleOCR-VL、GLM-OCR、HunyuanOCR 四引擎独立进程，单 GPU 顺序执行。
- 项目及持久化任务队列，暂停、取消、重试、异常后手动恢复。
- 保留原图和处理版本，支持透视校正、裁剪、旋转、去弯曲及区域重识别。
- 同图同批次模型对比，显式采用结果；人工修订与原始输出分离。
- 文字校对、表格行列及合并关系编辑、撤销重做、复核状态、TXT / MD / JSON / XLSX 导出。
- 离线引擎包导入、校验、启用、回退，以及导出来源追溯。

这些是当前仓库记录的能力，不是本轮重新验收的结果。首版尚未包含 PDF 输入；目标内网 A4000 和另一台干净 Windows 的验收状态仍以项目既有文档为准。

**最值得看的 6 个项目**

| 项目 | 相似程度及定位 | 已确认的相似点 | 与你的主要差别 | 仓库许可证 / Star |
|---|---|---|---|---|
| [PaddleOCR Local](https://github.com/CHEN010325/paddleocr-local) | 高；多模型本地文档工作台 | 五模型切换、单 GPU 互斥、任务持久化与恢复、文字编辑、原文对照、CLI 顺序对比、表格 XLSX 导出 | Windows 主线依赖支持 GPU 的 Docker Desktop；首次部署下载模型；未证实与你等价的 GUI 差异校对和结构化表格编辑 | Apache-2.0 / 128 |
| [Folio-OCR](https://github.com/vorojar/Folio-OCR) | 高；三栏批量文档工作台 | FastAPI＋SQLite、缩略图/原图/结果三栏、PDF/图片批量、自动保存、区域双向高亮、多文档管理 | 以 Ollama＋GLM-OCR 为主；编辑重点是文本/Markdown；未证实多引擎对比及 XLSX 表格编辑闭环 | MIT / 470 |
| [OctoOCR-offline](https://github.com/1ampa55ag3/octo-ocr) | 中高；离线办公校对 | PDF/图片、PP-OCRv5＋版面/表格管线、Quill 富文本、低置信提示、单元格编辑、Word/PDF 导出 | Windows 文档要求先装 Python、依赖和模型；未证实完整 Windows 便携包、多引擎对比及合并/拆分编辑闭环 | Apache-2.0 / 11 |
| [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) | 中高；成熟离线 OCR 工具 | Windows 解压即用、批量图片/PDF、可编辑文字、忽略区域、Paddle/Rapid 插件、CLI/HTTP | 表格识别导出 Excel、历史记录系统仍列远期计划；不能视为你的项目式表格校对替代品 | MIT / 47,258 |
| [Scribe OCR](https://github.com/scribeocr/scribeocr) | 中高；人工校对交互参考 | 全浏览器计算、原图叠加可编辑文字、低置信颜色提示、已有 OCR 数据校对、可搜索/数字化 PDF；源码有表格区域和 XLSX 出口 | 不是 Windows 便携应用；识别库独立于 UI 仓库；未证实你的四引擎、图像版本及引擎包机制 | AGPL-3.0 / 810 |
| [gImageReader](https://github.com/manisandro/gImageReader) | 中；传统桌面识别校对 | Windows 安装/便携包，PDF/图片批量、手动识别区域、原图和文字并排、拼写检查、hOCR/PDF | 以 Tesseract 为基础，非多 VLM 工作台；未证实结构化 Excel 校对流程 | GPL-3.0 / 1,995 |

**PaddleOCR Local：最优先研究的架构近邻**

其当前默认模型为 PaddleOCR-VL 1.6、PP-OCRv6、OvisOCR2、HPD-Parsing、NaviDC-OCR。和你的思路相近：统一入口、模型按需启动、切换时释放旧模型显存。它还覆盖 PDF / Office 输入、可编辑 DOCX、可搜索 PDF、目录监控和 CLI 自动化，适合参考后续输入输出扩展。

源码核查确认了三项边界：

- [CLI 文档](https://github.com/CHEN010325/paddleocr-local/blob/main/CLI.md) 和 [compare_file](https://github.com/CHEN010325/paddleocr-local/blob/main/pandocr_cli.py#L301) 实现同一文件的多模型顺序处理及 Markdown/JSON 对比报告。主前端文件和页面模板中未找到相应对比入口，故本报告只确认 CLI 对比，不能直接等同你的 GUI 文字/单元格差异、基准选择和采用状态。
- [前端编辑逻辑](https://github.com/CHEN010325/paddleocr-local/blob/main/static/app.js#L2829) 提供 Markdown 编辑，并有 PP-OCR 文字校正逻辑；未在本次检查中证实独立的合并/拆分表格编辑器。[路线图](https://github.com/CHEN010325/paddleocr-local/blob/main/ROADMAP.md)仍把人工修订历史和差异审计列为未完成项。
- [build_xlsx](https://github.com/CHEN010325/paddleocr-local/blob/main/exporters.py#L1881) 把 Markdown/HTML 表格转成多工作表工作簿，并处理公式注入；当前该函数逐格写值，没有调用单元格合并 API。不能把“可以导出 XLSX”当作“完整保留并可编辑合并关系”。

部署上，README 明确要求 Windows 用户准备支持 GPU 的 Docker Desktop，首次部署下载镜像/模型。它的本地数据处理目标与你一致，但尚不能直接代替你的原生 Windows 完整离线 ZIP。

**Folio-OCR：最适合参考工作台操作与 PDF 工作流**

它采用三栏布局，支持图片和 PDF 混合导入、逐页处理、版面区域与文本双向高亮、跨页搜索、文档恢复和自动保存。[README](https://github.com/vorojar/Folio-OCR#readme) 的功能说明与 [前端源码](https://github.com/vorojar/Folio-OCR/blob/main/folio_ocr/script.js)中的搜索及 800ms 防抖保存相符。

它的输出是 Markdown、TXT、DOCX、EPUB，适合书籍和扫描文档转写。默认识别路线是 PP-DocLayoutV3＋Ollama GLM-OCR。虽然可以配置模型名，但这不足以证明有异构引擎的能力声明、独立运行时管理和结果差异校对。

当前正式 Release 是 [v3.4.0](https://github.com/vorojar/Folio-OCR/releases/tag/v3.4.0)，发布资产为 Python wheel 和源码 tar.gz。README 的 Windows `start.bat` 仍以 Python/Ollama/模型准备为前提，不能误认成你要求的完整 Windows 离线便携包。仓库里的速度数字属于作者环境说明，本轮未复测，也不应用来推断 A4000 表现。

**OctoOCR 与 Scribe：校对功能需要看源码，不能只看 README**

OctoOCR 的 [MdunTableBlot 和 openCellEdit](https://github.com/1ampa55ag3/octo-ocr/blob/main/src/mdun/web/static/app.js#L63) 确实实现了表格单元格编辑：单击单元格进入输入浮层，将修改写回 Quill Delta。同时实现低置信标记、拼写提示和文字修复。它具备真实的人工校对逻辑，而不仅是结果预览。

但本次检查的表格块使用二维文字数组；未证实行列增删、合并拆分、多模型对比或 XLSX 导出闭环。仓库虽然有 [build_offline_package.py](https://github.com/1ampa55ag3/octo-ocr/blob/main/scripts/build_offline_package.py)，脚本明确面向鸿蒙 PC 的 Linux aarch64 环境，而且模型与应用代码不在脚本产物范围内；不能据此认定已提供 Windows 完整离线包。

Scribe OCR 的突出特点是把可编辑识别文字直接叠在原图上，以位置和颜色辅助校对。[主程序](https://github.com/scribeocr/scribeocr/blob/master/main.js#L627) 也有启用 XLSX 导出的逻辑，并要求开启布局功能；同文件有添加/删除表格区域的入口。它并非仅支持纯文本校对，但这些证据仍不足以确认与你相同的表格合并关系编辑和导出语义。其界面也有简体中文识别选项，中文实际效果仍需同样本测试。

这两个项目值得借鉴的是让用户更快定位和改正错误。自动标点、段落修复等能力若引入 DeloitteOCR，宜继续遵循你已有的“原始输出保留、人工修改独立保存”规则，避免修复结果覆盖识别证据。

**另外 6 个相关项目**

| 项目 | 类型及适合借鉴的部分 | 本轮判断 |
|---|---|---|
| [PP-OCRv6 Studio](https://github.com/andyhuo520/ppocrv6-studio) | MIT；113 Star；PP-OCRv6 Tiny/Small/Medium、本地历史、参数页、OmniDocBench 演示评测、浏览器 ONNX Demo | 是模型工作台近邻，主要测试环境为 Apple Silicon；Excel 导出不应未经核实就当作结构表还原 |
| [Local OCR Workbench](https://github.com/stevibe/local-ocr-workbench) | MIT；55 Star；Ollama GLM-OCR 图片/PDF 识别、流式结果、Markdown 渲染、模型端点设置 | README 自述小型测试 Web App，适合轻量接入示例；未证实完整项目、队列及表格校对体系 |
| [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF) | MPL-2.0；34,726 Star；可搜索 PDF/PDF-A、纠偏、OCR 插件接口 | 适合未来 PDF 处理管线参考，是命令行工具，不是现有工作台替代品 |
| [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) | GPL-3.0；45,022 Star；扫描件归档与检索，Docker Compose 部署 | 面向文档管理与可搜索档案；与精细表格校对的产品重点不同 |
| [MinerU](https://github.com/opendatalab/MinerU) | 79,738 Star；复杂文档解析、版面/表格/公式、CLI/FastAPI/Gradio | 更适合作为解析能力参考；不要把官网客户端功能直接当作 GitHub 仓库已开源的工作台能力 |
| [Marker](https://github.com/datalab-to/marker) | 39,677 Star；PDF 等文档转 Markdown/JSON、结构化内容处理 | 更适合参考解析/导出管线；代码与模型许可证分别计算，见下文 |

**维护与交付快照**

“最近 push”来自 GitHub `pushed_at`，以 UTC 日期展示；它是仓库推送时间，不等于最后业务代码修改时间。以下仓库查询时均未标记 archived。

| 项目 | 最近 push | 最新正式 Release / 发布资产核查 |
|---|---|---|
| PaddleOCR Local | 2026-09-10 | latest 接口未返回正式版本；路线图仍待发布 v0.2.0 |
| Folio-OCR | 2026-06-18 | v3.4.0，2026-06-18；wheel＋源码包 |
| OctoOCR | 2026-08-17 | latest 接口未返回正式版本；README/pyproject 标示 0.23.2 |
| Umi-OCR | 2025-11-20 | v2.1.5，2025-03-25；Paddle/Rapid Windows 自解压包及 Linux 包 |
| Scribe OCR | 2026-08-28 | latest 接口未返回正式版本；README 明确目前没有独立桌面应用 |
| gImageReader | 2026-01-15 | v3.4.3，2025-08-04；Windows 安装包及 portable.zip |

latest 接口不返回正式版本不等于没有 tag、预发布或其它下载渠道。Umi 的用户规模和可下载成品支持其作为成熟交付参考，但最近推送早于其它候选，不宜描述为持续高频更新。

**与你当前产品的差异，集中在组合与交付约束**

| 能力组合 | 本轮看到的情况 | 对 DeloitteOCR 的含义 |
|---|---|---|
| 多模型、单 GPU 顺序切换、任务恢复 | PaddleOCR Local 已有相近方案 | 这些并非独有能力，可直接对标实现和使用成本 |
| 三栏、自动保存、文档管理、原图定位 | Folio-OCR 等已有 | 三栏布局本身不足以形成差异，校对效率更值得验证 |
| 本地/离线 OCR | Umi、Octo、Folio 等均覆盖不同层次 | 需要强调完整依赖与模型随包交付的具体边界 |
| 异构引擎＋Windows 原生完整离线包＋显存串行调度 | 分别有相似部件，未证实候选完整覆盖此组合 | 你的主要工程定位之一；仍须完成目标机验收 |
| 同图同批次对比＋人工修订＋合并关系编辑＋明确采用＋确认后导出 | 各项目覆盖其中一部分，未证实等价闭环 | 可作为你的产品重点，但不能据此宣称识别准确率领先 |
| 图像版本、原始输出、人工修改、导出来源及离线引擎包回退 | 候选公开资料中未证实与你等价的完整机制 | 适合强调可追溯与后续维护能力，避免仅以“模型更多”描述价值 |

**建议的阅读和借鉴顺序**

1. **先看 PaddleOCR Local**：单卡模型调度、失败批次重试、多模型 CLI 对比，以及 PDF/Office 导入、可搜索 PDF/DOCX 导出。重点比较接口和用户流程，不必照搬 Docker 部署。
2. **再看 Folio-OCR**：跨页搜索、PDF 页面导航、多文档切换、区域双向高亮和长任务反馈。它的 PDF 与电子书方向可以补充你首版后的文档场景。
3. **然后看 Scribe OCR 与 OctoOCR**：原图叠字校对、低置信定位、表格单元格编辑；用相同错误样本比较完成校对的时间。
4. **最后看 Umi-OCR 和 gImageReader**：普通办公电脑上的首次启动、便携包、语言资源、批量交互和错误提示。

如果下一阶段只选择一个新增方向，优先考虑 **PDF 输入、逐页项目管理与可搜索 PDF 导出**：多个近邻已经覆盖，而你的首版明确暂未包含。若主要用户仍是拍照表格录入，则优先量化现有表格校对耗时和目标机稳定性，不必因为其它项目有 PDF 就立即改产品范围。

已有多个近邻并不自动意味着值得迁移代码库。以你当前已完成的工程范围，优先参考和定点借鉴更合理；替换项目需要重新验证现有离线交付、版本追溯与表格语义，不能只按 README 功能数量决定。

**源码复用时的许可证事实**

你的仓库顶层为 MIT。Folio、Umi、PP-OCRv6 Studio、Local OCR Workbench 为 MIT；PaddleOCR Local、OctoOCR 为 Apache-2.0。它们适合优先考察可复用代码，但依赖及模型仍各自适用许可证。

Scribe OCR 为 AGPL-3.0，gImageReader 与 Paperless-ngx 为 GPL-3.0。可以研究交互和架构；直接复制或形成衍生分发时，不能默认仍仅受你现有 MIT 许可覆盖。

两项容易使用旧信息的变化：

- [MinerU 当前 LICENSE](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md) 是基于 Apache-2.0 的自定义附加条款许可，包含规模门槛与对外在线服务标识要求；GitHub API 返回 `NOASSERTION`。本报告将它列为附条件源码参考，不把它当作无附加条件的 Apache-2.0 项目。
- [Marker 当前 README](https://github.com/datalab-to/marker#commercial-usage) 标示代码 Apache-2.0，模型为修改版 AI Pubs Open Rail-M，并对超过指定融资/营收规模的商业模型使用另设许可条件。代码许可不能代替权重许可。

**证据与复查**

检索快照、12 个仓库 README/元数据、重点项目目录树及发布资产信息保存在 [github-ocr-20260912](github-ocr-20260912/)。其中 [code-sources.json](github-ocr-20260912/code-sources.json) 与 [comparison-sources.json](github-ocr-20260912/comparison-sources.json) 记录源码取得地址和引用 SHA；目录树返回的 SHA 是树对象标识，不将其称为发布版本或 commit。

本报告中的 GitHub 链接指向可阅读的上游页面，页面随上游更新会变化；要复核本轮判断，应同时查看本地快照。未执行上游安装脚本、未加载模型、未修改 DeloitteOCR 应用源码。

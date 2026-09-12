# 公开样本识别结果抽查

观察时间：2026-09-12T07:38:33.092648+00:00。只读数据库，对照不可变 original 与 raw，不使用人工编辑后的结果。

本轮发现模型识别错误；抽查的数据转换路径未发现工作台额外丢行、改数值或破坏表格单元格。该结论限于本报告的图片/结果，不代表全项目无 bug。

## 文字锚点

| 图片 | 引擎 | 锚点匹配 | raw 与工作台 original 一致 |
|---|---|---:|---|
| chinese_scanned_exam.png | ppocr | 6/6 | True |
| chinese_handwriting.png | ppocr | 4/5 | True |
| chinese_store.jpg | ppocr | 8/8 | True |
| chinese_handwriting.png | hunyuan | 5/5 | True |

只忽略空白，不忽略错字。路牌额外识别到背景中的 PAD，不计作路牌八个锚点的错误。中文扫描锚点通过，但仍出现“縠→觳”；手写内容出现 >300→7300 等需要人工修正的问题。

## PubMed 表格

| 引擎 | 数据行 | 数值单元格正确 | 全部数据单元格正确 | raw HTML → 工作台 cells 保真 |
|---|---:|---:|---:|---|
| paddlevl | 4/4 | 28/28 | 32/32 | True |
| glm | 4/4 | 26/28 | 28/32 | True |
| hunyuan | 4/4 | 28/28 | 32/32 | True |

PaddleVL/Hunyuan 四条数据行和 28 个数值单元格均匹配。GLM 第二数据行 Km 10.8 识别成 18.8，其 SD ±5.7 识别成 ±1.7，另有两个组名错误；没有漏数据行。

原图没有物理单位标签，因此没有可验证的单位转写/转换结论；GLM 的 ³ 丢失属于科学记号丢失。± 与 \pm 在数值检查中视为等价，但原始写法完整保留于证据。

## 需校对的问题

- SPOT-01（model_recognition_error）：第 2 数据行 Km 10.8→18.8，紧随的 SD ±5.7→±1.7；WT w Veh→WT w Vbh，D3 KO w Veh→IKO w Vbh；[³H]DA uptake 丢失上标 ³。错误已存在 generated/official_markdown 原始模型输出，工作台忠实保留。
- SPOT-02（model_recognition_error）：手绘表格五级污染指数 >300→7300，改变数值阈值意义；“我们需要洁净的空气”→“我们需要洁净的气”；“冰雹”中的“雹”漏识别。均已存在 raw.rec_texts，非工作台漏字。
- SPOT-03（model_recognition_error）：六个指定锚点均匹配；额外目视抽查发现原图“縠纹”识别为“觳纹”，诗句与 C 选项均出现。源图局部证据 chinese-scan-poem-detail.png；不能将锚点通过解释为整页逐字正确。
- SPOT-04（model_structure_uncertainty）：原图是无竖线分组表头。三个模型生成的顶层合并范围不同；PaddleVL 将 DAT density 放在第 6–8 列，Hunyuan 放在第 6–7 列、末列空白，GLM 放在第 7–8 列。PaddleVL/Hunyuan 分组标题可能与实际 DAT/SD 列关联不当，需校对；这些 colspan 已存在模型 raw HTML，独立解析与工作台表格完全一致。
- SPOT-05（upstream_output_format_usability）：± 输出为字面 LaTeX \pm，³ 输出为 ^{3}。归一化后数据数值正确，但纯文本/单元格保留模型写法时用户可读性较差；此处未操作浏览器，因此不单独宣称已验证屏幕渲染缺陷。
- SPOT-06（model_comparison_success）：后续 Hunyuan 手写结果已纳入：5/5 文字锚点正确，手绘表格 5 数据行、15 数据单元格均与源图相符（仅归一全角 ＞/～）。正确保留 ＞300，并识别出“空气”“冰雹”；其 raw→original 文本及 HTML→cells 均保真。

Hunyuan 手写表格追加核验：5/5 数据行、15/15 数据单元格与原图相符，原始 HTML 独立解析与工作台 cells 完全一致。全角 ＞300 与原图 >300 按语义等价处理，不将其视作错误。

## 证据与边界

- accuracy-spotcheck.json：逐锚点、逐单元格比较，task/result ID，以及 raw 路径。
- spotcheck-original-results.json：本次只读抓取的原始结果，包括引擎 raw。
- pubmed-table-enlarged.png：仅用于目视核查的原图放大件；原始尺寸仍为 503×98。
- chinese-scan-poem-detail.png：原扫描诗句局部，用于核查“縠”字。
- 不调用浏览器、不启动识别、不写数据库。独立 HTMLParser 核对原始模型 HTML 与工作台单元格坐标、跨度和文本。

Hunyuan 手写新增结果本次是否已纳入：True。

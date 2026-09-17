# 后纠错研究与评测证据

检索日期：2026-09-17。本目录仅取得官方仓库资料及论文摘要，未下载评测图像、未运行模型。GitHub commit、抓取时间和正文 SHA-256 见 `source-index.json`；补充页面见 `supplemental-sources.json`，其中两个 HIPE 补充 README 使用抓取时 main 快照，未锁 commit。检索结果见 `searches.json`。

## 已有研究给出的启示

1. [Shef-AIRE/llms_post-ocr_correction](https://github.com/Shef-AIRE/llms_post-ocr_correction) 是真正的识别后纠错研究，代码为 MIT；README 给出 BART/Llama 2 微调与结果分析入口。对应[论文摘要](https://aclanthology.org/2024.lt4hala-1.14/)称，在 BLN600 的十九世纪英文报刊任务中，指令微调 Llama 2 的 CER 相对下降 54.51%，BART 为 23.30%。这是该论文特定数据和训练设置的结果，不能移用于本项目中文财务/手写材料，也不是通用 VLM 零样本成绩。
2. [impresso/llm-transcript-postcorrection](https://github.com/impresso/llm-transcript-postcorrection) 探讨不同模型、提示、噪声、语言与历史时期的变化。对应[论文摘要](https://aclanthology.org/2024.latechclfl-1.14/)评价了 14 个基础模型，并对其后纠错效率给出负面结论。这与上一项不构成直接矛盾：任务、训练和评价条件不同。它说明不能用“语言更通顺”替代识别纠错实验。README 顶部 MIT 徽章与底部许可描述不一致；GitHub license 元数据和正文均指向 AGPL-3.0，不按徽章宣称 MIT。
3. [HIPE-OCRepair-2026](https://github.com/hipe-eval/HIPE-OCRepair-2026) 已把 LLM 辅助 OCR 后纠错设为 ICDAR 竞赛；[数据仓库](https://github.com/hipe-eval/HIPE-OCRepair-2026-data)与[评价工具](https://github.com/hipe-eval/HIPE-OCRepair-2026-eval)可借鉴。评价输入是 OCR 文本及元数据，不能称为本项目需要的图像复核系统。评价支持 JSONL 契约、字符/词 MER、相对原 OCR 的改善/不变/恶化偏好分和 bootstrap 区间。MER 分母为 H+S+D+I，与常用 CER 的参考长度分母不同，不能混报。
4. [OCRBench v2](https://github.com/Yuliang-Liu/MultimodalOCR/tree/main/OCRBench_v2) 覆盖中英文本相关视觉定位、理解与推理。适合检验小型 VLM 是否有必要的视觉文字能力；其问答总分不能替代逐字符保真、金额完全匹配或二轮改错率。
5. [CC-OCR](https://github.com/AlibabaResearch/AdvancedLiterateMachinery/tree/main/Benchmarks/CC-OCR) 区分多场景文字、多语言、文档解析和信息抽取。适合把“读对字”“结构正确”“抽取正确”拆开评测。当前 README 示例榜单使用 2024 年模型，不能用于本轮 Qwen3.8/Qwen3.5 最新排序。

## 对当前项目建议的评测设计

把初始 OCR 完全正确、局部错误、结构错误、缺字、不可辨认的样本同时纳入。只有错误样本的修复率会掩盖原来正确内容被改坏的风险。

同一文档比较：现有 OCR；规则标疑；文本模型后纠错；图像独立重读；图像加原候选审校；先图像独立转录再比较；整页 VLM 重识别。加测候选顺序随机化和错误候选注入，以发现模型是否只附和提示。

报告原始严格 CER、规范化 CER、金额/编号/日期/单位完全匹配、修正数、新增错数、建议精确率/召回率、弃权、全量覆盖、定位可得率、完整表结构与最终导出读回。推理失败/缺定位/未触发不能从全量分母删除。中文不只看按空格切分的 WER。

风险示例（说明性推导，不是实测）：若基准 99% 字段正确，二轮修好剩余错误的一半，只带来 0.5% 全量收益；若它把原本正确字段的 1% 改坏，会新增 0.99% 全量错误，净效果反而为负。净收益条件为 `(1-e)*f < e*r`，其中 e 为初始错误率、r 为修错率、f 为正确字段改坏率。因此高质量 OCR 上的审校重点应是控制 f、支持弃权与合理选择复核范围。

后续自动采用的证据强度也要单独设计。在独立同分布伯努利假设下，n 次自动采用零引错，单侧 95% 错误率上界为 `1 - 0.05^(1/n)`，约为 `3/n`。n=300 时上界仍约 1%；证明低于 0.1% 需要约 3,000 次零引错。现实字符/字段同文档聚类，不能把同一页几千字当几千次独立试验；应按文档组估计区间，并同时报告自动采用覆盖率。零采用、零错误不构成安全性证据。

旧 200 张融合样本及已曝光的表格/PDF 材料可用于回归与设计，不可重新命名为独立测试。新文档按来源/模板分组隔离，模型、量化、prompt、裁剪策略和触发门槛在测试前冻结。用户复核效率另做同任务的顺序平衡对照，报告活跃操作时间与最终残留错误，不能把自动脚本点击时长当真人效率。

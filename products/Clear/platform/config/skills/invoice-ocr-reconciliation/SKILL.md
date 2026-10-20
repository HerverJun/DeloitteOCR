---
name: invoice-ocr-reconciliation
description: 处理复杂票据结构、识别冲突与台账交叉核对，保留单元格来源和证据；单纯图片/PDF识字直接用extract_text。
compatibility: Clear Workbench；需要已安装 OCR 原生服务，台账比对还需数据 worker。专业视觉模型为可选服务。
metadata:
  host: clear-workbench
  requires-tools: ocr_prepare ocr_run ocr_read_results results_describe results_to_input
---

围绕票据识别和核对执行已有 OCR 与数据工具。读取 `references/ocr-to-ledger.md` 了解结构桥边界，需报告结构时按需读取 `assets/invoice-workpaper-template.md`。模型识别值是专业模型产出，不能自动冒作人工确认值。

普通识字优先调用 `extract_text`，由服务完成准备、原生等待、可读全文与 TXT 导出；PDF 传入实际所需页码。需要样本选参时使用 `ocr_tune_batch`，比较真实候选结果并保留选参依据。普通提取无需建立审计计划或正式证据档案。下列细粒度接口用于需要明确控制的专业处理。

预处理、多引擎融合和专业视觉服务均通过已有目录调用，读取原始结果后按目标决定下一步。通用文件工具、终端和 Python 可用于整理、计算与输出，不能猜造识别值或隐去来源。

1. 读取已授权图片或 PDF 的真实资料版本。用 `assets_create_set` 固定图片集、`ocr_prepare` 查询实际引擎。PDF 先发现已有分页工具契约并固定页面，不能把 PDF 直接当图片输入。原生独立导入成果先通过目录查询、`native_attach_result` 接回；有 required_assets 时显式加入范围后读取。
2. `ocr_run` 选择真实可用引擎，必要时使用已有旋转/对比度预处理。原图及处理版本保留。已有多引擎结果确需融合时先核实同图、同输入版本和同批次，再调用 `ocr_fuse_results`；原引擎值与冲突不能丢弃。
3. 等待任务结束后分页 `ocr_read_results`；用 `ocr_read_issues` 查看失败或冲突，用 `documents_read` 读取有界原文。编号、金额、日期不能从缺失文字中猜出。需要专业视觉服务时先查 `ocr_list_visual_models`，独立调用 `ocr_visual_review` 并读取真实建议，不自动采用。
4. 需要修订/采用时使用 `ocr_edit_result` / `ocr_adopt_result` 的当前引用和 before/revision，并读历史保留原值；用户授权及工具范围仍由平台约束。不能因为技能指导便替用户把建议当确认。
5. 用 `results_describe` 核实原生结构表；存在真实表才用 `results_to_input` 固定 CSV。完整同表头无跨格的表可用 `results_concat_tables` 明确顺序拼接；不满足条件时报告限制，不能按 OCR 自由文本伪造完整财务表。
6. 读取台账实际列，沿用 data_import/read_table/apply_step/compare/read_result/export 完成核对，decimal 金额和文本编号保留。记录识别错误、缺失字段、失败页与不完整覆盖。
7. 用户需要底稿时，固定实际 OCR/比对结果为证据，按真实 evidence_ids 写发现并生成可编辑底稿。字段整理可使用辅助脚本，但每个财务事实须能回到实际原文；缺失或冲突保留待核实。完成后普通文字说明结果和已有下载成果。

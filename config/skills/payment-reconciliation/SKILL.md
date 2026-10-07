---
name: payment-reconciliation
description: 核对付款或往来台账，保留业务编号、精确金额、实际差异和来源，生成可下载结果及证据底稿。
compatibility: Clear Workbench；需要已登记的数据 worker，输入为已授权的 CSV、Excel 或 Parquet 台账。
allowed-tools: assets_search assets_describe assets_create_set data_create_session data_inspect_input data_import data_read_table data_apply_step data_compare data_read_result data_export audit_bind_evidence audit_read_evidence audit_write_finding audit_read_finding audit_generate_workpaper audit_update_plan artifacts_list artifacts_get delivery_publish
metadata:
  host: clear-workbench
  requires-tools: data_inspect_input data_import data_compare data_read_result data_export
---

以用户的核对目标组织实际工具调用。先查当前任务、项目口径和授权资料；金额阈值、期间、业务键或多工作表选择会实质改变结果而无法从现有信息确定时才提问。开始前读取 `references/columns-and-rules.md`；需要说明交付格式时按需读取 `assets/workpaper-template.md`。

1. 用 `assets_search` / `assets_describe` 找到左右台账的真实 ID、revision 和 blob_hash。用 `data_inspect_input` 查看工作表、列和样本，保留长业务编号和前导零，不能猜列 ID。仅处理明确授权的资料。
2. 用 `data_create_session` 建立数据作业。对每一侧调用 `data_import`，传入已读取资料版本、哈希、明确工作表和表头位置；每次写操作使用工具返回的最新 expected_version。
3. 用 `data_read_table` 确认真实列 ID、类型和原始值。确需清洗时调用 `data_apply_step`，保留原值及来源；金额显式转 decimal。无法转型、重复业务键和空值必须保留为事项，不能忽略后报告完整核对。
4. 用 `data_compare` 配置真实 key_pairs、compare_pairs、amount_pairs 和十进制字符串 amount_tolerance。等待已提交任务结束，不以第二次提交替代状态读取；原操作的结果/失败仍是事实。
5. 分页 `data_read_result`，读取不同、仅左、仅右及覆盖情况。解释用实际差异和 source_rows，未知原因写待核实，不能把推断当事实。
6. 用 `data_export` 导出实际结果。需要审计底稿时 `audit_bind_evidence` 固定结果及来源，`audit_write_finding` 分开写事实、解释和待补信息，再 `audit_generate_workpaper` 生成可编辑底稿。模板只是结构建议，真实编号、金额和证据由工具提供。
7. 用 `delivery_publish` 引用真实 artifact_refs/result_refs 交付，说明失败、未匹配和资料限制。用户只问核对口径时可直接对话，无需制造空作业。

只调用注册工具，不生成临时脚本代替数据 worker。不要改人工标签或签署编制、复核记录。

---
name: payment-reconciliation
description: 按业务键、期间与金额口径核对多份付款或往来台账，解释真实差异和来源；普通去空格等清洗直接用data_clean_files。
compatibility: Clear Workbench；需要已登记的数据 worker，输入为已授权的 CSV、Excel 或 Parquet 台账。
metadata:
  host: clear-workbench
  requires-tools: data_inspect_input data_import data_compare data_read_result data_export
---

以用户的核对目标组织实际工具调用。仅在目标依赖正式审计口径时读取项目背景；普通清洗直接处理已授权输入。金额阈值、期间、业务键或多工作表选择会实质改变结果而无法从现有信息确定时才提问。复杂核对按需读取 `references/columns-and-rules.md`；需要说明交付格式时读取 `assets/workpaper-template.md`。

普通批量清洗优先使用 `data_clean_files`，`trim_columns` 一次提交多列，服务完成导入、等待、读回和逐文件导出。已有算子未覆盖的规则可以在任务工作目录编写并运行 Python，保留编号文本、Decimal 金额及来源映射，读回实际输出。无需为普通清洗创建审计计划、证据档案或调用专用交付工具；下列细粒度接口用于确需控制版本、比较策略的任务。

1. 用 `assets_search` / `assets_describe` 找到左右台账的真实 ID、revision 和 blob_hash。用 `data_inspect_input` 查看工作表、列和样本，保留长业务编号和前导零，不能猜列 ID。仅处理明确授权的资料。
2. 用 `data_create_session` 建立数据作业。对每一侧调用 `data_import`，传入已读取资料版本、哈希、明确工作表和表头位置；每次写操作使用工具返回的最新 expected_version。
3. 用 `data_read_table` 确认真实列 ID、类型和原始值。确需清洗时调用 `data_apply_step`，保留原值及来源；金额显式转 decimal。无法转型、重复业务键和空值必须保留为事项，不能忽略后报告完整核对。
4. 用 `data_compare` 配置真实 key_pairs、compare_pairs、amount_pairs 和十进制字符串 amount_tolerance。等待已提交任务结束，不以第二次提交替代状态读取；原操作的结果/失败仍是事实。
5. 分页 `data_read_result`，读取不同、仅左、仅右及覆盖情况。解释用实际差异和 source_rows，未知原因写待核实，不能把推断当事实。
6. 用 `data_export` 导出实际结果。需要审计底稿时 `audit_bind_evidence` 固定结果及来源，`audit_write_finding` 分开写事实、解释和待补信息，再 `audit_generate_workpaper` 生成可编辑底稿。模板只是结构建议，真实编号、金额和证据由工具提供。
7. 用普通文字说明实际结果、失败、未匹配和资料限制，工作文件会登记为成果。正式业务需要显式核验时可使用 `delivery_publish`。用户只问核对口径时直接回答。

业务库写入、正式证据和原生版本继续经过平台服务。辅助脚本可处理授权工作副本；不要改人工标签或签署编制、复核记录。

# 已有 OCR 成果核对边界

用户只有 application=ocr、link_id、object_id 时，通过 native_attach_result 校验原图来源并取得 result_refs。已有已核实平台引用可进入 results_describe。仅来源仍不明确才有界 native_read_result；不要重复读取已经核实的原件。

results_describe 提供真实表目录；按用户指定 table_index 调用 results_to_input。该工具只支持完整无跨格网格，保留输入、结果版本、哈希及逐格原文来源。unsupported、缺格或合并语义不明时报告限制，不补猜、展开或创建新 OCR。

data_create_session 产生当前数据作业。data_inspect_input 探查两侧真实资产、版本、哈希、表头、工作表；data_import 顺序写 left/right，每次用回执最新 expected_version。之后 data_read_table 读两侧列 ID；data_compare 只采用用户明确规则，保留文本编号、空值、原文及来源行。不自动清洗、转金额或推断单位。data_read_result 按需要分页读取真实差异及覆盖。

audit_bind_evidence 绑定实际结果和来源；audit_read_evidence 核对 snapshot_hash 与 stale。data_export 导出该结果全部类别；delivery_publish 只引用实际 artifact_refs/result_refs，报告覆盖和待核边界。未取得完整导出或证据过期时不宣称全部完成。

阶段指导不改变资料集合、固定技能版本、工具权限或主控窗口。各工具契约须实际加载；成功加载下一步后执行它，不等未来阶段所有契约同时就绪。原始回执保留并能以 call_id 和 SHA 精确回读。

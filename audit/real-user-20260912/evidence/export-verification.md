# UI 原始导出文件核验

核验时间：2026-09-12T08:05:58.281222+00:00。已读取 14 份 UI 实际导出文件及 receipt。原始导出文件未改动。

已通过 21 项检查，0 项尚不满足预期。GLM 三种导出与已保存校对一致、原始 JSON 不变；行列与合并变化经操作人确认来自测试操作，归为中间编辑状态，不作为产品缺陷。

| 检查 | 结果 |
| --- | --- |
| retained_bytes_match_all_receipts | 通过 |
| earliest_road_txt_contains_edit_marker | 通过 |
| preview_and_selected_export_use_distinct_results | 通过 |
| selected_ppocr_txt_matches_its_original | 通过 |
| actual_identifier_values_remain_strings | 通过 |
| no_formulas_in_edited_workbook | 通过 |
| edited_table_has_six_rows_eight_columns | 通过 |
| markdown_tables_match_json_edited_tables | 通过 |
| xlsx_matches_json_edited_cells_and_merges | 通过 |
| json_original_matches_pre_edit_snapshot | 通过 |
| json_original_retains_unedited_values | 通过 |
| all_workbook_source_indexes_reference_correct_inputs | 通过 |
| all_workbooks_have_no_formula_cells | 通过 |
| road_single_image_exports_two_tables | 通过 |
| latest_road_tsv_identifiers_and_multiline | 通过 |
| zip_sources_match_workbook_indexes:20260912T073610.420575Z-01343611 | 通过 |
| aggregate_contains_all_three_tables:20260912T073627.374423Z-798f9083 | 通过 |
| confirmed_only_contains_road_two_tables:20260912T073628.800581Z-edbcfb49 | 通过 |
| batch_and_confirmed_tables_match_same_revision_standalone | 通过 |
| selected_txt_works_while_current_image_unrecognized | 通过 |
| review_only_txt_contains_saved_edit | 通过 |

实测说明：

- 最新路牌导出为 `20260912T074050.234526Z-42a60311/OCR-result.xlsx`，revision 11。第一表 A1 是标题，所以数据从第 2 行开始：A2=`00123`，B2=`90071992547409931234`，C2=`=1+1`，均是字符串；A3 为含真实换行的 `多行\n文本`，B3 为 `Yuyuan Rd.`，C3 为空。第二表独立保留。
- GLM revision 7 中间状态为 6×8，B3=`00123`、D3=`90071992547409931234`、E3=`=1+1`，均是字符串；MD/JSON/XLSX 单元格和合并结构一致。
- GLM 中间状态的空列/行与合并变化由测试插入、删除位置不同造成；路牌 revision 10 的空第一表由测试撤销时一并撤销了合并保存的 TSV 操作造成。操作人已确认，两者均保留为测试观察，不记为产品 bug。
- GLM 预览 XLSX 使用 `6993a7e01e8140439975951ec0b480ed` / GLM / revision 0；随后所选 TXT 使用 `d94ee08e0f834775ad38c32ff8951b1b` / PP-OCR，除 Windows CRLF 外内容等于其原文。
- 路牌最早 TXT 含完整 `QA 校对标记 00123`。GLM JSON original 与编辑前快照完全相等。
- ZIP 包含 2 个逐图工作簿与 sources.json，共 3 张数据表；汇总工作簿同样有 3 表及来源索引；confirmed_only 实际请求只含路牌结果，输出其 2 张表。批量与单独导出的相同 revision 数据、类型与合并完全一致。
- 较早批量文件固定在 GLM revision 7 / 路牌 revision 10，晚于它们的路牌 revision 11 不应反向改变这些历史导出。

文件清单：

| 文件 | 字节 | receipt SHA-256 |
| --- | ---: | --- |
| [exports\20260912T072822.545788Z-bb99b9f2\OCR-result.txt](exports\20260912T072822.545788Z-bb99b9f2\OCR-result.txt) | 70 | 一致 |
| [exports\20260912T072839.660656Z-00d9c47f\OCR-result.txt](exports\20260912T072839.660656Z-00d9c47f\OCR-result.txt) | 70 | 一致 |
| [exports\20260912T073100.990167Z-c6934b79\OCR-result.xlsx](exports\20260912T073100.990167Z-c6934b79\OCR-result.xlsx) | 6544 | 一致 |
| [exports\20260912T073120.639470Z-3d96497e\OCR-result.txt](exports\20260912T073120.639470Z-3d96497e\OCR-result.txt) | 298 | 一致 |
| [exports\20260912T073251.072311Z-4bc4dc70\OCR-result.xlsx](exports\20260912T073251.072311Z-4bc4dc70\OCR-result.xlsx) | 6528 | 一致 |
| [exports\20260912T073309.502659Z-63a38750\OCR-result.md](exports\20260912T073309.502659Z-63a38750\OCR-result.md) | 1730 | 一致 |
| [exports\20260912T073310.471393Z-9dbdc0a3\OCR-result.json](exports\20260912T073310.471393Z-9dbdc0a3\OCR-result.json) | 44337 | 一致 |
| [exports\20260912T073608.767665Z-ac0f90ca\OCR-result.xlsx](exports\20260912T073608.767665Z-ac0f90ca\OCR-result.xlsx) | 6892 | 一致 |
| [exports\20260912T073610.420575Z-01343611\OCR-results.zip](exports\20260912T073610.420575Z-01343611\OCR-results.zip) | 13049 | 一致 |
| [exports\20260912T073627.374423Z-798f9083\OCR-tables.xlsx](exports\20260912T073627.374423Z-798f9083\OCR-tables.xlsx) | 8045 | 一致 |
| [exports\20260912T073628.800581Z-edbcfb49\OCR-tables.xlsx](exports\20260912T073628.800581Z-edbcfb49\OCR-tables.xlsx) | 6891 | 一致 |
| [exports\20260912T074050.234526Z-42a60311\OCR-result.xlsx](exports\20260912T074050.234526Z-42a60311\OCR-result.xlsx) | 6960 | 一致 |
| [exports\20260912T075604.389359Z-4376c124\OCR-result.txt](exports\20260912T075604.389359Z-4376c124\OCR-result.txt) | 47 | 一致 |
| [exports\20260912T080402.091634Z-755dba7f\OCR-result.txt](exports\20260912T080402.091634Z-755dba7f\OCR-result.txt) | 76 | 一致 |

数据详情、所有单元格类型、完整来源索引及检查证据见 `export-verification.json`。这是本机源码/UI 导出核验，不代表便携发布包完整性验收。

# 0.9.0rc1 文档工作流实施记录

基准：`document-workflow-plan.md`（用户提供的完整计划，2026-09-13）。

M1–M5 开发及本机功能审计已完成，M6 按交付目录独立回执核验完整离线包。表格定位未达到精度目标，按计划保持实验性，不默认启用精确定位。

| 阶段 | 状态 | 验收证据 |
|---|---|---|
| M1 上游、许可、独立运行时、评测冻结 | 51 个 PDF wheel、5 个模型、3 套字体已锁定；许可原文与冻结评测完成 | config 下锁文件；licenses/document-workflow；pdf-clean-runtime-verification.json |
| M2 schema 9、文档页面、坐标和阶段记录 | schema 8→9 事务迁移、自动备份、旧 ID/原图保留与版本坐标追溯 | 完整 Python 回归 |
| M3 PDF/TIFF、原生和区域处理、导航恢复 | 懒展开、区域合并、原生结构预览、冲突校对、搜索、密码和真实 GPU 恢复完成 | document-recovery、document-capacity、ui-08、pdf-visuals |
| M4 几何证据、保守映射、校对与计时 | 保守降级、人工绑定、双向定位、上下文、草稿恢复及本地有效计时完成；精度未达标 | 100 表/5819 格评测、final-mapping-replay.json、final-performance.json |
| M5 可搜索 PDF、来源清单、读回核验 | 原页叠加、必要页重建、旧 OCR 清理、采用修订快照、缺定位预检、部分页与书签链接 | test_pdf_export.py、test_document_api.py、pdf-visuals/report.json |
| M6 评测、回归、离线交付和恢复说明 | Python 222、前端 32 通过，10 项几何补断言通过；最终版本构建完成 | 交付目录 application/document-acceptance/archive-verification/tested-components/source-verification 回执 |

证据目录：`audit/document-workflow-20260913/`。PDF 导出的来源清单嵌入 PDF 附件，多文档 ZIP 另带 sidecar。功能当前保持实验性，未测数据不得当作达到定位覆盖率目标。

效果、性能和测试边界详见 [质量报告](document-workflow-quality.md)，操作与回退见 [使用与恢复](document-workflow-usage-and-recovery.md)，上游复用见 [许可说明](document-workflow-licenses.md)。9 项外部 Edge 文档场景通过，errors=[] 且浏览器已关闭；合成 OCR/几何种子仅证明交互，不替代真实模型评测。

完整包核心工作台 48 项回归通过（application-full-02），另有中文双栏/mixed PDF 实际处理和导出读回通过；全部测试用的产品文件由 tested-manifest.json 冻结，最终归档的对应文件必须保持字节一致。

关机条件：完成实施、审计与证据落盘后，执行用户已授权的关机。未满足条件前不安排关机。

约束：只使用当前四引擎做文字识别；几何为辅助证据；无可靠框时降级，不制造等分单元格框。未获得的 A4000、另一台 Windows 及真实人工校对时间明确记为未测。

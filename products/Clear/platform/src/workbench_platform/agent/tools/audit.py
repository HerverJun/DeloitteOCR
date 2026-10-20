"""Agent registration and compatibility exports for shared audit operations."""
from workbench_platform.application_services.operations.audit import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    for name, schema, handler, description, effect in [
        ('conversation_answer', Answer, answer, '回答问题、讨论审计目标或解释现有结果；引用资料必须真实。此工具仅回复对话，不宣称执行任务完成。单独调用，执行成果仍用delivery_publish。', 'read'),
        ('audit_get_task', Empty, task, '读取本会话的审计目标、资料范围、工作安排、历史作业和成果引用。', 'read'),
        ('audit_get_project', Empty, profile, '读取用户保存的审计对象、期间、目标与规则口径；未填写的背景保持未知。', 'read'),
        ('audit_update_plan', Plan, update_plan, '记录并展示当前工作安排，按真实进展更新；不代替选择和执行工具。', 'derive'),
        ('audit_bind_evidence', DirectEvidenceRequest, bind_evidence, '固定直接数据/OCR/分类/取证结果或文件的实际证据，含来源位置与版本；不需要固定批次。大型结果指定分页。', 'derive'),
        ('audit_read_evidence', EvidenceRead, read_evidence, '读取固定证据、原始结果和来源位置。', 'read'),
        ('audit_write_finding', FindingWrite, write_finding, '保存或按expected_revision修订发现草稿，返回可用delivery_publish交付的固定版本引用，无需强制导出文件。事实必须绑定真实evidence_ids。解释/待补信息独立记录，Agent作者由服务器记录，不伪造人工署名。', 'versioned'),
        ('audit_read_finding', FindingRead, read_finding, '读取发现的最新版本及证据过期提示。', 'read'),
        ('audit_generate_workpaper', Workpaper, workpaper, '从已保存发现生成可编辑底稿草稿及证据附件，直接可下载，无需编制复核前置。', 'derive'),
        ('audit_create_workpaper', WorkpaperFromResults, create_workpaper, '从已经核实的真实结果/文件引用和发现，一次绑定证据、保存Agent发现草稿、生成可编辑HTML/Markdown与证据附件，并返回固定正文读回及实际下载。常规新底稿优先使用此组合入口，模型无需逐个搬运证据或发现ID；不会代写人工署名或改动已有发现。大结果明确指定证据分页；修订旧发现仍用audit_write_finding。', 'derive'),
        ('audit_search_documents', Search, search_documents, '在当前项目资料范围检索原文与已有OCR文字，返回文件、页或文本行来源。按 coverage.next_asset_offset/next_asset_match_offset 和 next_ocr_offset 接续；原文完结后 asset_offset 使用 asset_page_total。非语义检索；未识别图片须先OCR。', 'read'),
        ('audit_project_overview', Empty, overview, '读取项目背景、最近任务、发现和已有成果，供汇总或继续工作。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True))

"""Agent registration and compatibility exports for shared platform_management operations."""
from workbench_platform.application_services.operations.platform_management import *
from workbench_platform.agent.registry import ToolSpec


def register(registry):
    definitions = [
        ('assets_set_tags', Tags, tags, '更新当前范围资料标签，不删除原件。', 'versioned'),
        ('audit_update_task', TaskUpdate, task, '按所见版本更新当前任务名称与目标，不自行扩大资料范围。', 'versioned'),
        ('audit_update_project', ProfileWrite, profile, '按所见版本更新项目背景、期间与检查规则。', 'versioned'),
        ('batch_list_templates', Page, list_templates, '分页读取项目现有批次模板。', 'read'),
        ('batch_list', Page, list_batches, '分页列出当前资料范围内的既有批次，可接续手动或Agent执行。', 'read'),
        ('batch_create_template', Template, create_template, '保存现有业务批次模板，可继续手动使用；原生场景需当前关联link_id。', 'derive'),
        ('batch_template_from_session', FromSession, from_session, '复用真实数据会话中的配置保存批次模板，不手写替代脚本。', 'derive'),
        ('batch_preflight', Submit, preflight, '调用现有批次输入和配置预检查。', 'read'),
        ('batch_run', Submit, submit, '按模板与固定输入执行原生批次并等待真实结果。', 'derive'),
        ('batch_read', BatchRead, read_batch, '分页读取当前范围批次的状态、事项与成果。', 'read'),
        ('batch_control', BatchControl, control, '请求取消或继续现有批次，需读取实际状态确认；不自动重复控制。', 'control'),
        ('batch_dispose', Disposition, disposition, '按版本处置事项；明确AI操作人，采用需显式原生版本。', 'versioned'),
        ('batch_read_history', BatchRead, batch_history, '分页读取真实批次事项处置历史。', 'read'),
        ('batch_export', BatchExport, export_batch, '调用现有批次成果导出；未完成事项时采用intermediate，final沿用原生约束。', 'derive'),
        ('audit_formal_list', AuditPage, list_audit, '分页读取正式项目、程序和发现目录。', 'read'),
        ('audit_formal_read', AuditRead, read_audit, '读取正式项目、程序、发现或固定证据；evidence须提供entity_id。', 'read'),
        ('audit_formal_create_project', ProjectCreate, create_project, '创建正式审计项目草稿，保留手动编制复核能力。', 'derive'),
        ('audit_formal_update_project', ProjectWrite, lambda args, ctx: write_audit(args, ctx, 'project'), '按版本更新正式项目草稿。', 'versioned'),
        ('audit_formal_create_procedure', ProcedureCreate, lambda args, ctx: write_audit(args, ctx, 'procedure'), '创建审计程序并关联真实批次。', 'derive'),
        ('audit_formal_update_procedure', ProcedureWrite, lambda args, ctx: write_audit(args, ctx, 'procedure'), '按版本更新审计程序。', 'versioned'),
        ('audit_formal_create_finding', FindingCreate, lambda args, ctx: write_audit(args, ctx, 'finding'), '保存正式发现，或以source_draft固定版本转档；可关联批次、取证或直接证据。', 'derive'),
        ('audit_formal_update_finding', FindingWrite, lambda args, ctx: write_audit(args, ctx, 'finding'), '按版本修改正式发现、解释、结论和状态，可处理发现待办。', 'versioned'),
        ('audit_list_todos', TodoQuery, todos, '按项目/场景/状态分页查询现有待办，再调用实际事项或发现工具处理。', 'read'),
        ('audit_locate_todo', TodoLocate, locate_todo, '按待办身份直接定位实际事项、页面和筛选状态，再使用事项处置或发现修订工具。', 'read'),
        ('audit_formal_read_history', History, audit_history, '分页读取正式项目/程序/发现的真实操作历史及保存版本。', 'read'),
    ]
    for name, model, handler, description, effect in definitions:
        registry.register(ToolSpec(name, description, model, handler, effect=effect, idempotent=name != 'batch_control',
            lookup_operation={'batch_run': submit_lookup, 'batch_export': export_lookup, 'batch_dispose': disposition_lookup}.get(name)))

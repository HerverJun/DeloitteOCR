"""Audit conversation and business task tools on the platform registry."""
from typing import Literal

from pydantic import Field, model_validator, create_model
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult
from packages.contracts.audit import DirectEvidenceRequest, AuditDraftWrite, AuditDraftExport
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.application_services.references import verify


class Empty(AgentModel):
    pass


class Answer(AgentModel):
    content: str = Field(min_length=1, max_length=12000)
    references: list[AgentRef] = Field(default_factory=list, max_length=20)


class PlanItem(AgentModel):
    key: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(default='', max_length=500)
    status: Literal['pending', 'running', 'completed', 'blocked', 'skipped'] = 'pending'
    depends_on: list[str] = Field(default_factory=list, max_length=30)
    result_refs: list[AgentRef] = Field(default_factory=list, max_length=20)


class Plan(AgentModel):
    items: list[PlanItem] = Field(min_length=1, max_length=30)

    @model_validator(mode='after')
    def dependencies(self):
        keys = {item.key for item in self.items}
        if len(keys) != len(self.items):
            raise ValueError('步骤标识重复')
        visited = set()
        while len(visited) < len(keys):
            available = {item.key for item in self.items if item.key not in visited and set(item.depends_on) <= visited}
            if not available:
                raise ValueError('步骤依赖缺失或循环')
            visited.update(available)
        return self


class EvidenceRead(AgentModel):
    evidence_id: str = Field(min_length=1, max_length=64)


class FindingRead(AgentModel):
    draft_id: str = Field(min_length=1, max_length=64)


class Search(AgentModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)
    context_chars: int = Field(default=1200, ge=100, le=4000)
    asset_offset: int = Field(default=0, ge=0)
    asset_match_offset: int = Field(default=0, ge=0)
    ocr_offset: int = Field(default=0, ge=0)


FindingWrite = create_model('FindingWrite', __base__=AgentModel, **{key: (field.annotation, field) for key, field in AuditDraftWrite.model_fields.items() if key != 'request_id'})
Workpaper = create_model('Workpaper', __base__=AgentModel, **{key: (field.annotation, field) for key, field in AuditDraftExport.model_fields.items() if key != 'request_id'})


def author(ctx):
    run = ctx.store.run(ctx.run_id)
    connection = ctx.services.agent.connection.view()
    return {'kind': 'agent', 'task_id': ctx.session_id, 'run_id': ctx.run_id,
            'connection_revision': run['connection_revision'], 'model': connection.get('model', 'engineering_test_provider')}


def bind_evidence(args, ctx):
    value = ctx.services.audit_service.drafts.bind(args, ctx)
    return AgentToolResult(status='success', summary='已固定直接工具结果的证据与来源，不要求处理批次', data=evidence_data(value),
                           result_refs=[evidence_reference(value)]).model_dump()


def evidence_reference(value):
    return AgentRef(kind='evidence', workspace_id=value['workspace_id'], object_id=value['evidence_id'],
                    revision=1, sha256=value['snapshot_hash'], parent_id=value['task_id'])


def evidence_data(value):
    return {**{key: item for key, item in value.items() if key != 'source_assets'},
            'source_asset_count': len(value.get('source_assets', []))}


def read_evidence(args, ctx):
    value = ctx.services.audit_service.drafts.evidence(args.evidence_id, ctx)
    return AgentToolResult(status='success', summary='已读取固定证据、来源与位置', data=evidence_data(value),
                           result_refs=[evidence_reference(value)],
                           warnings=value.get('outdated_reasons', [])).model_dump()


def write_finding(args, ctx):
    command = AuditDraftWrite(request_id=ctx.operation_id, **args.model_dump())
    value = ctx.services.audit_service.drafts.write(command, ctx, author(ctx))
    return AgentToolResult(status='success', summary='已保存可编辑发现草稿；事实、解释、待补信息分别记录', data=value).model_dump()


def read_finding(args, ctx):
    value = ctx.services.audit_service.drafts.get(args.draft_id, ctx)
    return AgentToolResult(status='success', summary='已读取发现草稿及过期提示', data=value).model_dump()


def workpaper(args, ctx):
    command = AuditDraftExport(request_id=ctx.operation_id, **args.model_dump())
    ctx.current()
    bundle = ctx.services.audit_service.drafts.export(command, ctx, author(ctx))
    return AgentToolResult(status='success', summary='已生成Markdown/可编辑HTML底稿及JSON/CSV证据附件，可直接下载', data={'bundle': bundle},
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'], revision=1, application='platform')]).model_dump()


def search_documents(args, ctx):
    from workbench_platform.application_services.search import search
    value = search(args, ctx)
    refs = {str(item['reference']): item['reference'] for item in value['items'] if item.get('reference')}
    return AgentToolResult(status='success', summary='已检索项目原文与已有OCR结果，引用附文件/页来源', data=value,
                           result_refs=[AgentRef.model_validate(ref) for ref in refs.values()]).model_dump()


def overview(args, ctx):
    value = {'profile': ctx.services.agent.tasks.profile(ctx.workspace_id), 'tasks': ctx.services.agent.tasks.list(ctx.workspace_id),
             'findings': ctx.services.audit_service.drafts.list(ctx), 'artifacts': ctx.services.artifact_center.list(ctx.workspace_id, 0, 50)}
    return AgentToolResult(status='success', summary='已读取项目任务、发现与已有成果，可据此回答项目摘要', data=value).model_dump()


def answer(args, ctx):
    for reference in args.references:
        verify(reference.model_dump(), ctx)
    return AgentToolResult(status='success', summary='已回复审计对话',
        data={'conversation_answer': args.content}, result_refs=args.references).model_dump()


def task(args, ctx):
    return AgentToolResult(status='success', summary='已读取当前审计任务、范围与执行记录',
        data=ctx.services.agent.tasks.get(ctx.session_id)).model_dump()


def profile(args, ctx):
    return AgentToolResult(status='success', summary='已读取用户保存的审计背景与检查口径',
        data=ctx.services.agent.tasks.profile(ctx.workspace_id)).model_dump()


def update_plan(args, ctx):
    for item in args.items:
        for reference in item.result_refs:
            verify(reference.model_dump(), ctx)
    value = ctx.services.agent.tasks.set_plan(ctx.session_id, [item.model_dump() for item in args.items], ctx.operation_id, ctx.run_id, ctx.generation)
    ctx.store.event(ctx.session_id, ctx.run_id, ctx.operation_id + ':plan', 'audit_plan', value)
    return AgentToolResult(status='success', summary='已更新当前审计工作安排', data=value).model_dump()


def register(registry):
    for name, schema, handler, description, effect in [
        ('conversation_answer', Answer, answer, '回答问题、讨论审计目标或解释现有结果；引用资料必须真实。此工具仅回复对话，不宣称执行任务完成。单独调用，执行成果仍用delivery_publish。', 'read'),
        ('audit_get_task', Empty, task, '读取本会话的审计目标、资料范围、工作安排、历史作业和成果引用。', 'read'),
        ('audit_get_project', Empty, profile, '读取用户保存的审计对象、期间、目标与规则口径；未填写的背景保持未知。', 'read'),
        ('audit_update_plan', Plan, update_plan, '记录并展示当前工作安排，按真实进展更新；不代替选择和执行工具。', 'derive'),
        ('audit_bind_evidence', DirectEvidenceRequest, bind_evidence, '固定直接数据/OCR/分类/取证结果或文件的实际证据，含来源位置与版本；不需要固定批次。大型结果指定分页。', 'derive'),
        ('audit_read_evidence', EvidenceRead, read_evidence, '读取固定证据、原始结果和来源位置。', 'read'),
        ('audit_write_finding', FindingWrite, write_finding, '保存或按expected_revision修订发现草稿。事实必须绑定真实evidence_ids。解释/待补信息独立记录，Agent作者由服务器记录，不伪造人工署名。', 'versioned'),
        ('audit_read_finding', FindingRead, read_finding, '读取发现的最新版本及证据过期提示。', 'read'),
        ('audit_generate_workpaper', Workpaper, workpaper, '从已保存发现生成可编辑底稿草稿及证据附件，直接可下载，无需编制复核前置。', 'derive'),
        ('audit_search_documents', Search, search_documents, '在当前项目资料范围检索原文与已有OCR文字，返回文件、页或文本行来源。按 coverage.next_asset_offset/next_asset_match_offset 和 next_ocr_offset 接续；原文完结后 asset_offset 使用 asset_page_total。非语义检索；未识别图片须先OCR。', 'read'),
        ('audit_project_overview', Empty, overview, '读取项目背景、最近任务、发现和已有成果，供汇总或继续工作。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True))

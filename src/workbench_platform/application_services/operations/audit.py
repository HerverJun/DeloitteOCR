"""Audit conversation and business task tools on the platform registry."""
from typing import Literal

from pydantic import Field, model_validator, create_model
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult
from packages.contracts.audit import DirectEvidenceRequest, AuditDraftWrite, AuditDraftExport
from workbench_platform.application_services.identity import digest
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


class ResultFinding(AgentModel):
    title: str = Field(min_length=1, max_length=200)
    facts: str = Field(min_length=1, max_length=12000)
    interpretation: str = Field(default='', max_length=12000)
    missing_information: str = Field(default='', max_length=8000)
    recommendations: str = Field(default='', max_length=8000)
    rule: str = Field(default='', max_length=8000)
    evidence_indices: list[int] = Field(min_length=1, max_length=12, description='本次 evidence 数组中支撑此发现的零起始索引。')


class WorkpaperFromResults(AgentModel):
    title: str = Field(default='审计工作底稿草稿', min_length=1, max_length=200)
    evidence: list[DirectEvidenceRequest] = Field(min_length=1, max_length=12, description='已经读到、核实的真实结果或文件固定引用；服务负责绑定证据ID。')
    findings: list[ResultFinding] = Field(min_length=1, max_length=10, description='依据上述真实资料形成的发现，事实、解释和缺口分开；全部保存为Agent草稿。')

    @model_validator(mode='after')
    def source_indices(self):
        if any(index < 0 or index >= len(self.evidence) for finding in self.findings for index in finding.evidence_indices):
            raise ValueError('发现证据索引超出本次实际证据列表')
        return self


def create_workpaper(args, ctx):
    """Reuse the existing evidence/draft/export services with stable child IDs."""
    from workbench_platform.audit.direct import snapshot
    from workbench_platform.application_services.result_text import read_text
    from workbench_platform.artifacts.store import ArtifactUnavailable
    ctx.require_write()
    # Check every source before any write. Full observations stay in the
    # evidence store, rather than becoming another model-sized tool result.
    for item in args.evidence:
        if item.reference.kind == 'evidence':
            verify(item.reference.model_dump(mode='json'), ctx)
        else:
            snapshot(item, ctx)
    service = ctx.services.audit_service.drafts
    evidence = [service.bind(item, ctx) for item in args.evidence]
    identity = author(ctx)
    findings = []
    for index, finding in enumerate(args.findings):
        command = AuditDraftWrite(request_id='o' + digest([ctx.operation_id, 'finding', index])[:40],
            expected_revision=0, evidence_ids=list(dict.fromkeys(evidence[i]['evidence_id'] for i in finding.evidence_indices)),
            **finding.model_dump(exclude={'evidence_indices'}))
        findings.append(service.write(command, ctx, identity))
    saved = {'evidence_ids': [item['evidence_id'] for item in evidence],
             'findings': [{'draft_id': item['draft_id'], 'revision': item['revision'], 'title': item['title']} for item in findings]}
    bundle, reference = None, None
    try:
        bundle = service.export(AuditDraftExport(request_id='o' + digest([ctx.operation_id, 'export'])[:40],
            title=args.title, draft_ids=[finding['draft_id'] for finding in findings]), ctx, identity)
        reference = AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'], revision=1, application='platform')
        readback = read_text(reference, ctx, file_name='workpaper.md', limit=4000)
        if readback['status'] != 'success':
            raise ValueError('固定正文读回尚未通过')
    except (OSError, ValueError, ArtifactUnavailable) as error:
        from workbench_platform.agent.store import RunSuperseded, Conflict
        if isinstance(error, (RunSuperseded, Conflict)):
            raise
        repair = ({'tool': 'results_read_text', 'arguments': {'reference': reference.model_dump(), 'file_name': 'workpaper.md', 'limit': 4000}}
                  if reference else {'tool': 'audit_generate_workpaper', 'arguments': {'draft_ids': [item['draft_id'] for item in findings], 'title': args.title}})
        return AgentToolResult(status='error', summary='证据及发现已保存；底稿导出或正文读回未完成，按返回引用仅继续缺失步骤',
            error={'code': 'workpaper_export_incomplete', 'message': '已保存的发现不需重新创建；使用repair提供的原发现或成果引用继续。', 'retryable': False},
            data={**saved, 'repair': repair, **({'bundle': bundle} if bundle else {})},
            result_refs=[finding_reference(item) for item in findings], artifact_refs=[reference] if reference else []).model_dump()
    return AgentToolResult(status='success', summary='已绑定真实证据、保存发现草稿、生成可编辑底稿并核验实际正文及哈希，可直接下载',
        data={**saved, 'bundle': bundle, 'readback': readback['data']}, artifact_refs=[reference],
        result_refs=[finding_reference(item) for item in findings]).model_dump()


def author(ctx):
    run = ctx.store.run(ctx.run_id)
    controller = ctx.services.agent.connection
    connection = (controller.resolve(run['connection_revision'])[0]
                  if run['connection_revision'] > 0 and hasattr(controller, 'resolve') else controller.view())
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
    data = {**{key: item for key, item in value.items() if key != 'source_assets'},
            'source_asset_count': len(value.get('source_assets', []))}
    observation = value.get('observation') or {}
    reference = value.get('reference') or {}
    if reference.get('application') == 'ocr' and isinstance(observation.get('snapshot'), dict):
        snapshot = observation['snapshot']
        text = (snapshot.get('edited') or {}).get('text')
        compact = {key: item for key, item in snapshot.items() if key not in {'original', 'edited'}}
        if isinstance(text, str):
            compact['edited'] = {'text': text[:8000], 'text_total_chars': len(text),
                'text_complete': len(text) <= 8000, 'next_offset': 8000 if len(text) > 8000 else None}
        data['observation'] = {**observation, 'snapshot': compact}
        data['observation_preview_only'] = True
        data['complete_observation_location'] = value.get('location_url')
        data['text_readback'] = {'tool': 'results_read_text', 'reference': reference}
    return data


def read_evidence(args, ctx):
    value = ctx.services.audit_service.drafts.evidence(args.evidence_id, ctx)
    return AgentToolResult(status='success', summary='已读取固定证据、来源与位置', data=evidence_data(value),
                           result_refs=[evidence_reference(value)],
                           warnings=value.get('outdated_reasons', [])).model_dump()


def write_finding(args, ctx):
    command = AuditDraftWrite(request_id=ctx.operation_id, **args.model_dump())
    value = ctx.services.audit_service.drafts.write(command, ctx, author(ctx))
    return AgentToolResult(status='success', summary='已保存可编辑发现草稿；事实、解释、待补信息分别记录', data=value,
                           result_refs=[finding_reference(value)]).model_dump()


def read_finding(args, ctx):
    value = ctx.services.audit_service.drafts.get(args.draft_id, ctx)
    return AgentToolResult(status='success', summary='已读取发现草稿及过期提示', data=value,
                           result_refs=[finding_reference(value)], warnings=value.get('outdated_reasons', [])).model_dump()


def finding_reference(value):
    """Pin saved bytes, excluding transient stale/link read projections."""
    snapshot_hash = value.get('transfer_source', {}).get('snapshot_hash') or digest(value)
    return AgentRef(kind='result', application='platform', workspace_id=value['workspace_id'],
        object_id=value['draft_id'], parent_id=value['task_id'], revision=value['revision'], sha256=snapshot_hash,
        coverage={'kind': 'audit_draft', 'evidence_count': len(value['evidence_ids']), 'draft_status': value['status']},
        sources=[AgentRef(kind='evidence', workspace_id=value['workspace_id'], object_id=identifier, revision=1,
                         sha256=value['evidence_hashes'][identifier], parent_id=value['task_id'])
                 for identifier in value['evidence_ids']])


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

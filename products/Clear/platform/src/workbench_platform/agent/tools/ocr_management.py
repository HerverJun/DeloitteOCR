"""Native literal edits, adoption, history and professional visual models."""
from typing import Literal
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import Conflict
from workbench_platform.application_services.references import verify
from workbench_platform.application_services.native import scoped_handoff, availability, read_job
from workbench_platform.jobs.native_ocr import OcrResultConflict


class Result(AgentModel):
    reference: AgentRef


class Page(Result):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class Change(AgentModel):
    kind: Literal['text', 'cell', 'caption']
    before: str = Field(max_length=100000)
    after: str = Field(max_length=100000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    table_index: int | None = Field(default=None, ge=0, le=99)
    row: int | None = Field(default=None, ge=0)
    column: int | None = Field(default=None, ge=0)


class Edit(Result):
    changes: list[Change] = Field(min_length=1, max_length=100)


class Adopt(Result):
    note: str = Field(default='', max_length=1800)


class Visual(Result):
    model_id: str = Field(min_length=1, max_length=200)
    scope: Literal['target', 'page'] = 'page'
    target: dict | None = None
    target_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class Models(AgentModel):
    link_id: Identity


def bound(ctx, ref):
    if ref.application != 'ocr' or ref.kind != 'result' or not ref.parent_id:
        raise ValueError('需要OCR结果引用')
    value = verify(ref.model_dump(), ctx)
    from workbench_platform.application_services.native_mapping import binding, adapter_for
    imported = binding(ctx, ref.parent_id)
    if imported:
        adapter = adapter_for(ctx, 'ocr', imported['link_id'])
        result = next(result for result in adapter.results(imported['native_items']) if result.native_result_id == ref.object_id)
        return value, adapter, result, imported
    service = ctx.services.ocr_handoff
    row = scoped_handoff(ctx, service, ref.parent_id)
    adapter = service._adapter(service._context(ctx.workspace_id, row['link_id']))
    task_id = value['snapshot']['task_id']
    result = next(result for result in adapter.results([task_id]) if result.native_result_id == ref.object_id)
    return value, adapter, result, row


def updated(ctx, ref, adapter, result):
    revision = adapter.result_revision(result)
    tables = adapter.result_tables(result, revision)
    return ref.model_copy(update={'revision': revision, 'sha256': tables['content_sha256']})


def edit(args, ctx):
    value, adapter, result, _ = bound(ctx, args.reference)
    body = {'project_id': adapter.context.module_project_id, 'result_id': result.native_result_id,
            'revision': args.reference.revision, 'changes': [change.model_dump(exclude_none=True) for change in args.changes],
            'actor': {'kind': 'agent', 'id': ctx.session_id}}
    try:
        receipt = adapter._request('POST', '/api/platform/edits/'+ctx.operation_id, body, result_conflict=True, max_body=262144)
    except OcrResultConflict as error:
        raise Conflict('OCR修订变化') from error
    tables = adapter.result_tables(result, receipt['revision'])
    ref = args.reference.model_copy(update={'revision': receipt['revision'], 'sha256': tables['content_sha256']})
    return AgentToolResult(status='success', summary='已保存逐项OCR修订与Agent作者；未自动确认人工复核', data={'receipt': receipt}, result_refs=[ref]).model_dump()


def edit_lookup(args, ctx):
    _, adapter, result, _ = bound(ctx, args.reference)
    value = adapter._request('GET', f'/api/platform/edits/{ctx.operation_id}?project_id={adapter.context.module_project_id}')
    if not value['committed']:
        return None
    receipt = value['receipt']; tables = adapter.result_tables(result, receipt['revision'])
    ref = args.reference.model_copy(update={'revision': receipt['revision'], 'sha256': tables['content_sha256']})
    return AgentToolResult(status='success', summary='已核实OCR逐项编辑原回执和保存版本，未重复编辑', data={'receipt': receipt}, result_refs=[ref]).model_dump()


def adopt(args, ctx):
    _, adapter, result, _ = bound(ctx, args.reference)
    try:
        from workbench_platform.application_services.native_operations import origin
        origin(ctx, 'ocr', adapter.context.link_id, adapter.context.instance_id)
        receipt = adapter.adopt(result, args.reference.revision, operation_id=ctx.operation_id,
            actor={'id': 'agent_'+ctx.session_id[:50], 'display_name': 'AI审计Agent', 'source': 'local'}, note='Agent采用：'+args.note)
    except OcrResultConflict as error:
        raise Conflict('OCR采用版本变化') from error
    return AgentToolResult(status='success', summary='已采用明确OCR修订，保留AI操作人；人工复核确认独立', data={'receipt': receipt}, result_refs=[args.reference]).model_dump()


def adopt_lookup(args, ctx):
    from workbench_platform.application_services.native_operations import origin
    _, adapter, _, _ = bound(ctx, args.reference)
    instance = origin(ctx, 'ocr', adapter.context.link_id, adapter.context.instance_id)
    receipt = adapter.operation(ctx.operation_id, expected_instance_id=instance)
    if receipt is None:
        return None
    return AgentToolResult(status='success', summary='已核实原OCR采用回执，未重新采用', data={'receipt': receipt}, result_refs=[args.reference]).model_dump()


def history(args, ctx):
    _, adapter, result, _ = bound(ctx, args.reference)
    value = adapter.result_history(result, args.offset, args.limit)
    return AgentToolResult(status='success', summary='已读取OCR历史修订', data=value,
        next_offset=args.offset+args.limit if args.offset+args.limit < value['total'] else None).model_dump()


def models(args, ctx):
    adapter = ctx.services.job_service._adapter(ctx.services.job_service._context(ctx.workspace_id, 'ocr', args.link_id))
    value = adapter._request('GET', '/api/multimodal/models')
    return AgentToolResult(status='success', summary='已读取现有本地/外部专业视觉模型与实际可用状态', data=value).model_dump()


def visual(args, ctx, allow_new=True):
    value, adapter, result, row = bound(ctx, args.reference)
    body = {'model_id': args.model_id, 'revision': args.reference.revision, 'version_id': result.version_id,
            'scope': args.scope, 'request_id': ctx.operation_id}
    if args.target is not None:
        body['target'] = args.target
    if args.target_ids is not None:
        body['target_ids'] = args.target_ids
    review = adapter._request('GET', f'/api/results/{result.native_result_id}/multimodal')
    prior = next((item for item in review['requests'] if item['request_id'] == ctx.operation_id), None)
    if prior:
        task_id = prior['task_id']
    else:
        if not allow_new:
            return None
        catalog = adapter._request('GET', '/api/multimodal/models')
        if not any(item['id'] == args.model_id and item.get('available') for item in catalog['models']):
            raise ValueError('专业视觉模型不可用')
        task_id = adapter._request('POST', f'/api/results/{result.native_result_id}/multimodal', body, result_conflict=True)['task_id']
    job = ctx.services.job_service.register(ctx.workspace_id, row['link_id'], 'ocr', [task_id])
    return AgentToolResult(status='pending', summary='已调度独立专业视觉模型，等待原生文字建议；建议不自动采用',
        data={'model_id': args.model_id, 'result_id': result.native_result_id},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, object_id=job['platform_job_id'], revision=job['generation'],
            application='ocr', native_id=result.native_result_id, parent_id=args.reference.parent_id, sources=[args.reference], coverage={'kind': 'visual_review'})]).model_dump()


def visual_lookup(args, ctx):
    return visual(args, ctx, False)


def read_visual(args, ctx):
    _, adapter, result, _ = bound(ctx, args.reference)
    value = adapter._request('GET', f'/api/results/{result.native_result_id}/multimodal')
    proposals = value.pop('proposals')
    return AgentToolResult(status='success', summary='已读取专业视觉模型真实建议、依据与过期状态；建议不自动写入',
        data={**value, 'proposals': proposals[args.offset:args.offset+args.limit], 'total': len(proposals)}, result_refs=[args.reference],
        next_offset=args.offset+args.limit if args.offset+args.limit < len(proposals) else None).model_dump()


def settle(ref, ctx):
    job = read_job(ctx, ref['object_id'])
    if job['status'] not in {'succeeded', 'partial', 'failed', 'cancelled'}:
        return None
    value = read_visual(Page(reference=AgentRef.model_validate(ref['sources'][0])), ctx)
    if job['status'] in {'failed', 'cancelled'}:
        value.update(status='error', error={'code': job['status'], 'message': '专业视觉任务未成功，请读取原生请求错误'})
    return value


def register(registry):
    for name, schema, handler, description, effect in [
        ('ocr_edit_result', Edit, edit, '按固定OCR引用的版本逐项修改文字/单元格，before必须吻合，不重录整表；返回新引用。', 'versioned'),
        ('ocr_adopt_result', Adopt, adopt, '采用明确OCR结果修订，记录AI操作人；不冒签人工复核。', 'versioned'),
        ('ocr_read_history', Page, history, '分页读取原生OCR编辑历史与修订目录。', 'read'),
        ('ocr_list_visual_models', Models, models, '查询独立专业视觉服务的本地/外部模型目录，凭据沿用该服务已配置连接。', 'read'),
        ('ocr_visual_review', Visual, visual, '调用已查询且可用的专业视觉模型阅读现有采用OCR结果，返回真实建议；外部模型会通过原生连接发送图像。', 'derive'),
        ('ocr_read_visual_review', Page, read_visual, '分页读取专业视觉建议、证据及过期状态，可选择显式文字编辑。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            lookup_operation={'ocr_visual_review': visual_lookup, 'ocr_edit_result': edit_lookup, 'ocr_adopt_result': adopt_lookup}.get(name),
            availability=lambda services: availability(services, 'ocr')))

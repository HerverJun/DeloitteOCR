"""Existing native OCR preprocessing catalog and CPU result fusion."""
from typing import Literal
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import Conflict
from workbench_platform.application_services.native import availability, read_job
from workbench_platform.jobs.native_ocr import OcrResultConflict
from .ocr_management import Models, Page, bound


class Fusion(AgentModel):
    references: list[AgentRef] = Field(min_length=2, max_length=16)
    content_type: Literal['table', 'print', 'handwriting'] = 'table'
    mode: Literal['conservative', 'aggressive'] = 'conservative'


class Issues(Page):
    state: Literal['pending', 'resolved', 'question', 'stale'] | None = None


def options(args, ctx):
    from workbench_platform.application_services.native_mapping import adapter_for
    adapter = adapter_for(ctx, 'ocr', args.link_id)
    return AgentToolResult(status='success', summary='已查询既有OCR处理选项和实际融合策略', data={
        'preprocess': [{'kind': 'rotate', 'degrees': [90, 180, 270, -90]}, {'kind': 'contrast', 'factor_min': .2, 'factor_max': 3}],
        'fusion_policies': adapter._request('GET', '/api/fusion/policies')}).model_dump()


def submit(args, ctx, allow_new=True):
    contexts = [bound(ctx, reference) for reference in args.references]
    adapter, row = contexts[0][1], contexts[0][3]
    if (adapter.context.role == 'viewer' or any(reference.revision != 0 for reference in args.references)
            or any(value[1].context.link_id != adapter.context.link_id for value in contexts)
            or len({reference.object_id for reference in args.references}) != len(args.references)):
        raise ValueError('融合需要同一关联项目的不同原始OCR结果；编辑内容使用显式编辑工具')
    path = f'/api/projects/{adapter.context.module_project_id}/fusion-requests/{ctx.operation_id}'
    previous = adapter._request('GET', path)
    task_ids = previous['task_ids'] if previous['submitted'] else None
    if task_ids is None:
        if not allow_new:
            return None
        try:
            task_ids = adapter._request('POST', f'/api/projects/{adapter.context.module_project_id}/fusion', {
                'result_ids': [reference.object_id for reference in args.references], 'request_id': ctx.operation_id,
                'content_type': args.content_type, 'mode': args.mode}, result_conflict=True)['task_ids']
        except OcrResultConflict as error:
            raise Conflict('融合输入或原请求发生变化') from error
    job = ctx.services.job_service.register(ctx.workspace_id, row['link_id'], 'ocr', task_ids)
    return AgentToolResult(status='pending', summary='已提交既有CPU融合器，保留各引擎原值和冲突依据',
        data={'link_id': row['link_id'], 'content_type': args.content_type, 'mode': args.mode},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, object_id=job['platform_job_id'],
            revision=job['generation'], application='ocr', parent_id=args.references[0].parent_id,
            sources=args.references, coverage={'kind': 'fusion'})]).model_dump()


def lookup(args, ctx):
    return submit(args, ctx, False)


def settle(ref, ctx):
    job = read_job(ctx, ref['object_id'])
    if job['status'] not in {'succeeded', 'partial', 'failed', 'cancelled'}:
        return None
    if job['status'] != 'succeeded':
        return AgentToolResult(status='error', summary='融合未完整完成，请读取原生错误', data={'job': job},
            error={'code': job['status'], 'message': '保留原始引擎结果，可读取后调整输入重新提交'}).model_dump()
    from workbench_platform.application_services.native_mapping import adapter_for, attach, read
    adapter = adapter_for(ctx, 'ocr', job['link_id'])
    results = adapter.results(job['native_job_ids'])
    if not results:
        raise ValueError('融合任务没有可核实的实际结果')
    refs = [read(ctx, attach(ctx, 'ocr', job['link_id'], result.native_result_id)) for result in results]
    return AgentToolResult(status='success', summary='已读取实际融合结果，可继续查看冲突、编辑或转为数据输入',
        data={'job': job}, result_refs=refs).model_dump()


def issues(args, ctx):
    _, adapter, result, _ = bound(ctx, args.reference)
    suffix = f'?offset={args.offset}&limit={args.limit}' + ('&state='+args.state if args.state else '')
    value = adapter._request('GET', f'/api/results/{result.native_result_id}/issues'+suffix)
    return AgentToolResult(status='success', summary='已读取原生结果冲突与定位依据', data=value,
        next_offset=args.offset+args.limit if args.offset+args.limit < value['total'] else None).model_dump()


def register(registry):
    for name, model, handler, description, effect in [
        ('ocr_list_processing_options', Models, options, '查询既有旋转/对比度预处理参数及融合策略。预处理传给ocr_run。', 'read'),
        ('ocr_fuse_results', Fusion, submit, '将同一图片、版本和批次的原始多引擎结果交给既有CPU融合器，保存来源与冲突；不融合后续编辑值。', 'derive'),
        ('ocr_read_issues', Issues, issues, '分页读取既有OCR/融合结果的冲突及来源依据，可继续显式OCR编辑。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, model, handler, effect=effect, idempotent=True,
            lookup_operation=lookup if name == 'ocr_fuse_results' else None,
            availability=lambda services: availability(services, 'ocr')))

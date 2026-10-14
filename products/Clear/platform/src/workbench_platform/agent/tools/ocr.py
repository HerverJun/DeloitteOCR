"""Actual image OCR handlers, bounded result reads and immutable attempts."""
from dataclasses import asdict
import json
from typing import Literal

from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.application_services.native import availability, prepare, scoped_set, attempt_set, read_job, scoped_handoff
from workbench_platform.agent.registry import ToolSpec


class OcrPrepare(AgentModel):
    link_id: Identity | None = None


class Rotate(AgentModel):
    kind: Literal['rotate']
    degrees: Literal[90, 180, 270, -90]


class Contrast(AgentModel):
    kind: Literal['contrast']
    factor: float = Field(ge=.2, le=3)


class OcrRun(AgentModel):
    link_id: Identity
    asset_set_id: Identity
    asset_set_revision: int = Field(ge=1)
    engines: list[Identity] = Field(min_length=1, max_length=16)
    preprocess: list[Rotate | Contrast] = Field(default_factory=list, max_length=2)
    parent_operation_id: Identity | None = None


class OcrRead(AgentModel):
    handoff_id: Identity
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class OcrExport(AgentModel):
    handoff_id: Identity


def prepare_tool(args, context):
    value = prepare(context, 'ocr', args.link_id)
    return AgentToolResult(status='success', summary='已核实 OCR 服务、当前项目和引擎目录', data=value).model_dump()


def pending(value, context):
    job = value['job']
    handoff = value['handoff']
    return AgentToolResult(status='pending', summary='已提交真实图片 OCR 任务',
        data={'handoff_id': handoff['handoff_id'], 'engines': handoff['processing']['engines']},
        job_refs=[AgentRef(kind='job', workspace_id=context.workspace_id, object_id=job['platform_job_id'],
                          revision=job['generation'], application='ocr', parent_id=handoff['handoff_id'])]).model_dump()


def run(args, context):
    original = scoped_set(context, args.asset_set_id, args.asset_set_revision)
    attempt = attempt_set(context, original)
    handoff = context.services.ocr_handoff
    imported = handoff.begin(context.workspace_id, args.link_id, attempt.asset_set_id, attempt.revision)
    catalog = handoff.capabilities(context.workspace_id, imported['handoff_id'])
    if not catalog['recognition'] or not set(args.engines) <= {item['id'] for item in catalog['engines']}:
        raise ValueError('所选引擎不可用，或输入需要 PDF 页处理')
    return pending(handoff.submit(context.workspace_id, imported['handoff_id'], args.engines,
        **({'preprocess': [item.model_dump() for item in args.preprocess]} if args.preprocess else {})), context)


def lookup(args, context):
    original = scoped_set(context, args.asset_set_id, args.asset_set_revision)
    attempt = attempt_set(context, original)
    handoff = context.services.ocr_handoff
    imported = handoff.begin(context.workspace_id, args.link_id, attempt.asset_set_id, attempt.revision)
    value = handoff.submit(context.workspace_id, imported['handoff_id'], args.engines, allow_new=False,
        **({'preprocess': [item.model_dump() for item in args.preprocess]} if args.preprocess else {}))
    return None if value.get('not_submitted') else pending(value, context)


def read(args, context):
    handoff = context.services.ocr_handoff
    row = scoped_handoff(context, handoff, args.handoff_id)
    adapter = handoff._adapter(handoff._context(context.workspace_id, row['link_id']))
    imported = handoff.status(context.workspace_id, args.handoff_id)
    processing = imported.get('processing')
    if not processing or not processing['platform_job_id']:
        raise ValueError('图片 OCR 尚未提交')
    job = read_job(context, processing['platform_job_id'])
    task_ids = job['native_job_ids']
    results = adapter.results(task_ids)
    items, refs = [], []
    for result in results[args.offset:args.offset + args.limit]:
        revision = adapter.result_revision(result)
        snapshot = adapter.result_snapshot(result, revision)
        tables = adapter.result_tables(result, revision)
        edited = snapshot.get('edited') or {}
        item = {'result_id': result.native_result_id, 'revision': revision, 'selected': result.selected,
                'task_id': result.native_task_id, 'available': snapshot['available'],
                'text': str(edited.get('text', ''))[:8000],
                'table_count': len(tables['tables']), 'table_source': tables['source'], 'warnings': tables['warnings'],
                'source_locator': result.locator}
        items.append(item)
        refs.append(AgentRef(kind='result', workspace_id=context.workspace_id, object_id=result.native_result_id,
            native_id=result.native_result_id, revision=revision, application='ocr', parent_id=args.handoff_id,
            sha256=tables['content_sha256']))
    batch = adapter.batch(task_ids)
    return AgentToolResult(status='success', summary='已读取真实 OCR 结果及失败覆盖', data={
        'handoff_id': args.handoff_id, 'items': items, 'tasks': [asdict(task) for task in batch.tasks][:50],
        'total': len(results), 'inference_and_adoption_are_distinct': True}, result_refs=refs,
        coverage={'succeeded': batch.succeeded, 'failed': batch.failed, 'cancelled': batch.cancelled, 'pending': batch.pending},
        next_offset=args.offset + args.limit if args.offset + args.limit < len(results) else None).model_dump()


def export(args, context):
    scoped_handoff(context, context.services.ocr_handoff, args.handoff_id)
    bundle = context.services.artifact_center.publish_ocr_result(context.workspace_id, args.handoff_id)
    return AgentToolResult(status='success', summary='已归集真实 OCR 导出成果', data={'bundle': bundle},
        artifact_refs=[AgentRef(kind='artifact', workspace_id=context.workspace_id, object_id=bundle['bundle_id'],
                               revision=1, application='ocr', parent_id=args.handoff_id)]).model_dump()


def settle(reference, context):
    context.current()
    job = read_job(context, reference['object_id'])
    context.current()
    if job['status'] == 'paused':
        return None
    if job['status'] in {'queued', 'running', 'unknown'}:
        return None
    if job['status'] in {'failed', 'cancelled'}:
        return AgentToolResult(status='error', summary='OCR 任务未成功，旧失败保留', data={'handoff_id': reference['parent_id'], 'job': job},
            error={'code': job['status'], 'message': '请根据真实错误和可用引擎创建新的失败子集 attempt'},
            coverage=job['counts']).model_dump()
    value = read(OcrRead(handoff_id=reference['parent_id']), context)
    if job['status'] == 'partial':
        value['warnings'].append('部分输入失败，不能报告完整覆盖')
    return value


def register(registry):
    for name, schema, handler, description, effect in [
        ('ocr_prepare', OcrPrepare, prepare_tool, '自动准备已安装OCR服务，复用当前范围项目，查询真实引擎目录。', 'derive'),
        ('ocr_run', OcrRun, run, '对固定图片集运行已查询引擎，可显式旋转/调整对比度；原图和处理版本保留。PDF需按页工具。', 'derive'),
        ('ocr_read_results', OcrRead, read, '分页读取真实OCR内容、结构表数量、结果版本与失败覆盖。推理和人工采用分别引用。', 'read'),
        ('ocr_export', OcrExport, export, '从已完成原生OCR结果生成可下载托管成果，不能伪造未识别的表格。', 'derive'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=lambda services: availability(services, 'ocr'),
            lookup_operation=lookup if name == 'ocr_run' else None,
            input_kinds=('asset_set',), output_kinds=('result', 'artifact', 'job'),
            resource_keys=lambda args, ctx: ('ocr:' + getattr(args, 'link_id', ctx.workspace_id),)))

"""Actual image OCR handlers, bounded result reads and immutable attempts."""
from dataclasses import asdict, replace
import json
import uuid
from typing import Literal

from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.application_services.native import availability, prepare, scoped_set, attempt_set, read_job, scoped_handoff


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
    context.current()
    imported = handoff.begin(context.workspace_id, args.link_id, attempt.asset_set_id, attempt.revision)
    catalog = handoff.capabilities(context.workspace_id, imported['handoff_id'])
    if not catalog['recognition'] or not set(args.engines) <= {item['id'] for item in catalog['engines']}:
        raise ValueError('所选引擎不可用，或输入需要 PDF 页处理')
    context.current()
    return pending(handoff.submit(context.workspace_id, imported['handoff_id'], args.engines,
        **({'preprocess': [item.model_dump() for item in args.preprocess]} if args.preprocess else {})), context)


def lookup(args, context, *, include_not_submitted=False):
    if include_not_submitted and availability(context.services, 'ocr')['state'] == 'preparable':
        # An interrupted intent has no job_ref yet, so the job observer cannot
        # restore its service. Reopen the original linked project only; the
        # launch identity changes, business operation and native key do not.
        prepare(replace(context, operation_id='l' + uuid.uuid4().hex), 'ocr', args.link_id)
        context.current()
    original = scoped_set(context, args.asset_set_id, args.asset_set_revision)
    attempt = attempt_set(context, original)
    handoff = context.services.ocr_handoff
    context.current()
    imported = handoff.begin(context.workspace_id, args.link_id, attempt.asset_set_id, attempt.revision)
    context.current()
    value = handoff.submit(context.workspace_id, imported['handoff_id'], args.engines, allow_new=False,
        **({'preprocess': [item.model_dump() for item in args.preprocess]} if args.preprocess else {}))
    if value.get('not_submitted'):
        if include_not_submitted and value.get('authoritative_negative') is True:
            from workbench_platform.application_services.errors import NativeNotSubmitted
            return NativeNotSubmitted(context.operation_id, 'ocr_run', value['native_request_id'],
                value['native_project_id'], value['input_fingerprint'])
        return None
    return pending(value, context)


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
                'text': str(edited.get('text', '')), 'text_total_chars': len(str(edited.get('text', ''))),
                'text_truncated': False,
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
            job_refs=[AgentRef.model_validate(reference)], coverage=job['counts']).model_dump()
    value = read(OcrRead(handoff_id=reference['parent_id']), context)
    value['job_refs'] = [reference]
    if job['status'] == 'partial':
        value['warnings'].append('部分输入失败，不能报告完整覆盖')
    return value

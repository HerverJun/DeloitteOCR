"""Agent operations over the same saved document service as the UI."""
from contextlib import closing
import json
from typing import Literal
from pydantic import Field
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.application_services.references import verify, asset_allowed


class Parse(AgentModel):
    reference: AgentRef
    ocr_policy: Literal['auto', 'never'] = 'auto'
    ocr_link_id: Identity | None = None
    fallback_engine: Literal['ppocr', 'paddlevl', 'glm', 'hunyuan'] | None = None


class Saved(AgentModel):
    document_id: Identity


class Search(Saved):
    query: str = Field(min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class Control(Saved):
    action: Literal['cancel', 'resume']


def availability(services):
    service = getattr(services, 'document_service', None)
    return {'state': 'ready' if service and service.python.is_file() else 'unavailable',
            'reason': '' if service and service.python.is_file() else '固定 DocVortex CPU 运行时不可用', 'hard_cancel': True}


def scoped(ctx, identifier):
    value = ctx.services.document_service.get(ctx.workspace_id, identifier)
    if not asset_allowed(ctx, value['asset_id']):
        raise ValueError('文档来源超出任务资料范围')
    return value


def reference(ctx, value):
    return AgentRef(kind='result', workspace_id=ctx.workspace_id, object_id=value['document_id'], revision=value['document']['revision'],
        sha256=value['result_hash'], application='platform', parent_id=value['document_id'],
        coverage={'kind': 'documents'}, sources=[AgentRef(kind='asset', workspace_id=ctx.workspace_id,
            object_id=value['asset_id'], revision=value['asset_revision'], sha256=value['blob_hash'])])


def pending(ctx, value):
    return AgentToolResult(status='pending', summary='已提交文档解析；原生内容由 CPU 读取，扫描内容交给现有 OCR 队列',
        data={'document_id': value['document_id'], 'source': value['source']},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, object_id=value['document_id'],
            revision=1, application='platform', parent_id=value['document_id'], coverage={'kind': 'documents'})]).model_dump()


def parse(args, ctx):
    verify(args.reference.model_dump(), ctx)
    if args.reference.kind != 'asset':
        raise ValueError('解析输入需要原资料引用')
    ctx.current()
    value = ctx.services.document_service.create(ctx.workspace_id, {'schema_version': '1.0',
        'request_id': ctx.operation_id, 'asset_id': args.reference.object_id, 'asset_revision': args.reference.revision,
        'blob_hash': args.reference.sha256, 'ocr_policy': args.ocr_policy, 'ocr_link_id': args.ocr_link_id,
        **({'fallback_engine': args.fallback_engine} if args.fallback_engine is not None else {})})
    return pending(ctx, value)


def lookup(args, ctx):
    service = ctx.services.document_service
    with closing(service.db()) as db:
        row = db.execute('SELECT body FROM documents WHERE workspace_id=? AND request_id=?', (ctx.workspace_id, ctx.operation_id)).fetchone()
    return pending(ctx, json.loads(row[0])) if row else None


def read_saved(args, ctx):
    value = scoped(ctx, args.document_id)
    if value['status'] not in {'succeeded', 'partial'}:
        return AgentToolResult(status='needs_verification' if value['status'] == 'needs_verification' else 'error',
            summary=value['error'] or '文档尚无可读取结果', data={'document_id': args.document_id, 'status': value['status']}).model_dump()
    document = ctx.services.document_service.parsed(ctx.workspace_id, args.document_id)
    return AgentToolResult(status='success', summary='已核实保存的文档结构；请用 documents_read 按页或块读取',
        data={'document_id': args.document_id, 'source': value['source'], 'page_count': len(document['pages']),
            'table_count': len(document['tables']), 'pages': [{'page_number': page['page_number'], 'needs_ocr': page['needs_ocr'],
            'block_count': len(page['blocks']), 'engine': page['engine']} for page in document['pages']], 'files': value['files']},
        result_refs=[reference(ctx, value)], warnings=document['warnings'],
        coverage={'total': len(document['pages']), 'readable': document['coverage']['readable_pages']}).model_dump()


def search(args, ctx):
    value = scoped(ctx, args.document_id)
    result = ctx.services.document_service.search(ctx.workspace_id, args.document_id, args.query, args.offset, args.limit)
    return AgentToolResult(status='success', summary='已检索固定保存的文档块与页级来源', data=result,
        result_refs=[reference(ctx, value)], next_offset=result['next_offset']).model_dump()


def control(args, ctx):
    scoped(ctx, args.document_id)
    ctx.current()
    value = ctx.services.document_service.control(ctx.workspace_id, args.document_id, args.action)
    return pending(ctx, value) if args.action == 'resume' else AgentToolResult(status='success', summary='已取消文档解析及所属 OCR 等待', data={'document_id': args.document_id, 'status': value['status']}).model_dump()


def settle(ref, ctx):
    value = scoped(ctx, ref['object_id'])
    if value['status'] in {'queued', 'running'}:
        return None
    return read_saved(Saved(document_id=ref['object_id']), ctx)

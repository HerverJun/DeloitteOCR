"""Explicit PDF page requests over native CPU/GPU document queues."""
from contextlib import closing
import json
from typing import Literal
from pydantic import Field, model_validator
from packages.contracts.agent import AgentModel, AgentRef, AgentToolResult, Identity
from workbench_platform.application_services.native import availability, scoped_set, attempt_set, scoped_handoff
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import canonical, digest, now
from .ocr import OcrRead


class InspectDocument(AgentModel):
    link_id: Identity
    asset_set_id: Identity
    asset_set_revision: int = Field(ge=1)


class ProcessPages(AgentModel):
    handoff_id: Identity
    document_id: Identity
    page_numbers: list[int] = Field(min_length=1, max_length=1000)
    mode: Literal['native', 'ocr', 'auto'] = 'auto'
    engine: Literal['ppocr', 'paddlevl', 'glm', 'hunyuan'] = 'ppocr'
    force: bool = False
    parent_operation_id: Identity | None = None

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.page_numbers)) != len(self.page_numbers) or any(number < 1 for number in self.page_numbers):
            raise ValueError('页集无效')
        self.page_numbers = sorted(self.page_numbers)
        return self


class ReadPage(AgentModel):
    handoff_id: Identity
    document_id: Identity
    page_number: int = Field(ge=1, le=1000)


def bound(context, handoff_id, document_id=None):
    service = context.services.ocr_handoff
    row = scoped_handoff(context, service, handoff_id)
    adapter = service._adapter(service._context(context.workspace_id, row['link_id']))
    imported = adapter.import_status(row['request_id'])
    documents = {item.get('target_document_id'): item for item in imported['items'] if item.get('target_document_id')}
    if imported['state'] != 'completed' or (document_id and document_id not in documents):
        raise ValueError('PDF不属于固定输入或未导入完成')
    return service, row, adapter, documents


def inspect(args, ctx):
    original = scoped_set(ctx, args.asset_set_id, args.asset_set_revision)
    if any(not ctx.services.asset_library[0].get(ctx.workspace_id, item.asset_id).original_name.lower().endswith('.pdf') for item in original.items):
        raise ValueError('PDF探查只接受PDF固定资料集')
    attempt = attempt_set(ctx, original)
    imported = ctx.services.ocr_handoff.begin(ctx.workspace_id, args.link_id, attempt.asset_set_id, attempt.revision)
    _, _, adapter, documents = bound(ctx, imported['handoff_id'])
    rows = []
    for document_id, source in documents.items():
        value = adapter.document_pages(document_id)
        member = next(member for member in attempt.items if member.item_id == source['item_id'])
        rows.append({'document_id': document_id, 'asset_id': member.asset_id, 'page_count': value['total'],
                     'pages': [{'page_number': page['page_number'], 'status': page['status']} for page in value['pages']]})
    return AgentToolResult(status='success', summary='已读取真实PDF页数；处理需要显式页集',
        data={'handoff_id': imported['handoff_id'], 'documents': rows,
              'modes': ['native', 'ocr', 'auto'], 'new_attempt_required_for_other_page_set': True}).model_dump()


def submission(args, ctx, allow_new):
    service, row, adapter, documents = bound(ctx, args.handoff_id, args.document_id)
    if any(number > documents[args.document_id]['page_count'] for number in args.page_numbers):
        raise ValueError('所选页超出真实文档页数')
    parameters = args.model_dump(include={'page_numbers', 'mode', 'engine', 'force'})
    key = 'r' + digest([args.handoff_id, args.document_id, parameters])[:40]
    with closing(service._connect()) as db:
        old = db.execute('SELECT * FROM ocr_page_submission WHERE handoff_id=?', (args.handoff_id,)).fetchone()
        if old and (old['document_id'], old['parameters_json']) != (args.document_id, canonical(parameters)):
            raise ValueError('此attempt已固定其他页集，请重新探查创建新的attempt')
        db.execute('INSERT OR IGNORE INTO ocr_page_submission VALUES(?,?,?,?,?)',
            (args.handoff_id, args.document_id, key, canonical(parameters), now()))
    value = adapter.page_request(args.document_id, key)
    if not value['submitted']:
        if not allow_new:
            return None
        value = adapter.page_request(args.document_id, key, body=parameters)
    return AgentToolResult(status='pending', summary='已按显式页集提交真实PDF处理',
        data={'handoff_id': args.handoff_id, 'document_id': args.document_id, 'requested_pages': args.page_numbers},
        job_refs=[AgentRef(kind='job', workspace_id=ctx.workspace_id, object_id=key, native_id=args.document_id,
            revision=1, application='ocr', parent_id=args.handoff_id, coverage={'kind': 'pdf_pages'})]).model_dump()


def process(args, ctx):
    return submission(args, ctx, True)


def lookup(args, ctx):
    return submission(args, ctx, False)


def status(ref, ctx):
    _, _, adapter, _ = bound(ctx, ref['parent_id'], ref['native_id'])
    return adapter.page_request(ref['native_id'], ref['object_id'])


def cancel(ref, ctx):
    _, _, adapter, _ = bound(ctx, ref['parent_id'], ref['native_id'])
    return adapter.page_request(ref['native_id'], ref['object_id'], action='cancel')


def read(args, ctx):
    service, _, adapter, _ = bound(ctx, args.handoff_id, args.document_id)
    value = adapter.document_pages(args.document_id, offset=args.page_number - 1, limit=1)
    if len(value['pages']) != 1 or value['pages'][0]['page_number'] != args.page_number:
        raise ValueError('页面不存在')
    page = value['pages'][0]
    native = adapter.snapshot()
    image = next((image for image in native['images'] if image['id'] == page['image_id']), None)
    result_id = image.get('selected_result') if image else None
    with closing(service._connect()) as db:
        submission = db.execute('SELECT * FROM ocr_page_submission WHERE handoff_id=?', (args.handoff_id,)).fetchone()
    if submission:
        receipt = adapter.page_request(args.document_id, submission['request_id'])
        stage = next((stage for stage in receipt.get('stages', []) if stage['page_number'] == args.page_number), None)
        if stage is None:
            raise ValueError('此页不属于本次显式页集')
        result_id = (stage.get('output') or {}).get('result_id') if stage['status'] == 'succeeded' else None
    if not image or not result_id:
        return AgentToolResult(status='success', summary='此页没有实际识别结果；未生成表格',
            data={'page_number': args.page_number, 'has_result': False}).model_dump()
    tasks = [task for task in native['tasks'] if task['result_id'] == result_id and task['status'] == 'succeeded']
    if len(tasks) != 1:
        raise ValueError('页结果来源无法核实')
    result = adapter.results([tasks[0]['id']])[0]
    revision = adapter.result_revision(result)
    snapshot = adapter.result_snapshot(result, revision)
    table_snapshot = adapter.result_tables(result, revision)
    edited = snapshot.get('edited') or {}
    return AgentToolResult(status='success', summary='已读取真实页结果，表格存在才提供结构引用',
        data={'page_number': args.page_number, 'has_result': True, 'text': str(edited.get('text', ''))[:8000],
              'table_count': len(table_snapshot['tables']), 'table_source': table_snapshot['source'],
              'document': snapshot.get('original', {}).get('document', {})},
        warnings=table_snapshot['warnings'],
        result_refs=[AgentRef(kind='result', workspace_id=ctx.workspace_id, object_id=result.native_result_id,
            revision=revision, sha256=table_snapshot['content_sha256'], native_id=result.native_result_id,
            application='ocr', parent_id=args.handoff_id, coverage={'page_number': args.page_number})]).model_dump()


def settle(ref, ctx):
    value = status(ref, ctx)
    if not value['submitted']:
        return AgentToolResult(status='needs_verification', summary='PDF原生请求回执不存在，未自动重发').model_dump()
    states = {stage['status'] for stage in value['stages']}
    if states & {'paused', 'interrupted'}:
        if any(stage['status'] in {'paused', 'interrupted'} and not stage['owned'] for stage in value['stages']):
            return AgentToolResult(status='needs_verification', summary='复用的PDF阶段已暂停，本请求无权替其它任务恢复').model_dump()
        from workbench_platform.application_services.job_lifecycle import cold_interruption
        # The native resume endpoint acts on all paused owned stages. Avoid
        # overriding human/unknown intent when it is mixed with cold recovery.
        if any(stage['status'] == 'paused' and not cold_interruption(stage) for stage in value['stages']):
            return None
        _, _, adapter, _ = bound(ctx, ref['parent_id'], ref['native_id'])
        ctx.current()
        adapter.page_request(ref['native_id'], ref['object_id'], action='resume')
        return None
    if states & {'queued', 'running', 'waiting_gpu'}:
        return None
    if 'waiting_unlock' in states:
        return AgentToolResult(status='needs_verification', summary='PDF需要原生临时解锁，密码不进入模型参数').model_dump()
    coverage = {state: sum(stage['status'] == state for stage in value['stages']) for state in ('succeeded', 'failed', 'cancelled')}
    coverage['total'] = len(value['stages'])
    refs, pages, warnings = [], [], []
    for stage in value['stages'][:50]:
        pages.append({key: stage[key] for key in ('page_number', 'status', 'error')})
        if stage['status'] == 'succeeded':
            result = read(ReadPage(handoff_id=ref['parent_id'], document_id=ref['native_id'], page_number=stage['page_number']), ctx)
            refs.extend(result['result_refs'])
            warnings.extend(result['warnings'])
    return AgentToolResult(status='success' if coverage['succeeded'] else 'error',
        summary='PDF显式页集已处理，覆盖按真实页状态记录', data={'handoff_id': ref['parent_id'],
              'document_id': ref['native_id'], 'requested_pages': value['parameters']['page_numbers'], 'pages': pages},
        result_refs=refs, coverage=coverage,
        warnings=list(dict.fromkeys(warnings + (['部分指定页未成功，不可声明完整覆盖']
                  if coverage['failed'] or coverage['cancelled'] else [])))).model_dump()


def register(registry):
    for name, schema, handler, description, effect in [
        ('ocr_inspect_document', InspectDocument, inspect, '读取PDF固定输入的页数并创建新attempt。其它页集或重试需要新attempt。', 'derive'),
        ('ocr_process_pages', ProcessPages, process, '按真实PDF文档ID和显式页集调用native/ocr/auto原生流程；同请求去重，等待由运行时完成。', 'derive'),
        ('ocr_read_page', ReadPage, read, '读取指定PDF页的真实内容、表数和页来源；无表不能编造Excel。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True,
            availability=lambda services: availability(services, 'ocr'),
            lookup_operation=lookup if name == 'ocr_process_pages' else None,
            input_kinds=('asset_set',), output_kinds=('job', 'result')))

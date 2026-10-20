"""Paged manual result discovery, including explicit new-source scope choices."""
from contextlib import closing
from dataclasses import replace

from packages.contracts.agent import AgentRef
from workbench_platform.application_services.references import data_reference, reference_assets, verify


def paged(fetch, key='items', limit=50):
    offset = 0
    while True:
        page = fetch(offset, limit)
        items = page[key]
        yield from items
        offset += len(items)
        if not items or offset >= page.get('total', offset if len(items) < limit else offset + 1):
            break


def result_references(ctx, task, unavailable, *, only_ocr=False):
    from .native_mapping import binding, read as read_native
    if getattr(ctx, 'store', None) and ctx.store.rows("SELECT name FROM sqlite_master WHERE name='native_bindings'"):
        for row in ctx.store.rows('SELECT id FROM native_bindings WHERE workspace_id=? ORDER BY id', (ctx.workspace_id,)):
            try:
                imported = binding(ctx, row['id'])
                if not only_ocr or imported['application'] == 'ocr':
                    yield read_native(ctx, imported), '原生工具独立导入 · 当前保存结果'
            except ValueError:
                unavailable.append({'id': row['id'], 'reason': '原生结果或原件暂不可核实'})
    service = ctx.services.data_workbench
    sessions = () if only_ocr else paged(lambda offset, limit: service.list_page(ctx.workspace_id, offset, limit))
    for row in sessions:
        try:
            session = service.get(row['id'])
            register = getattr(service, 'register_upload_source', None)
            if register:
                for table in session['state']['tables'].values():
                    register(session['id'], table['source']['asset_id'])
        except ValueError:
            unavailable.append({'application': 'data', 'id': row['id'], 'reason': '原始上传来源尚不可核实'})
            continue
        targets = [(side, 'table', table['revision']) for side, table in session['state']['tables'].items()]
        if session['state']['result_id']:
            targets.append(('', 'result', session['state']['result_id']))
        for side, kind, identifier in targets:
            try:
                value = data_reference(ctx, session['id'], kind, identifier)
                ref = AgentRef(kind=kind, workspace_id=ctx.workspace_id, object_id=identifier, revision=identifier,
                               sha256=value['content_sha256'], application='data', parent_id=session['id'])
                title = session['name'] + ' · ' + ('核对结果' if kind == 'result' else '左表' if side == 'left' else '右表')
                yield ref, title
            except ValueError:
                unavailable.append({'application': 'data', 'id': session['id'], 'reason': '数据结果或来源尚不可读取'})
    refs = task.get('result_refs', []) + task.get('initial_refs', []) + task.get('job_refs', [])
    known = list(dict.fromkeys((ref.get('application'), ref.get('parent_id')) for ref in refs
                              if ref.get('application') in {'ocr', 'classification'} and ref.get('parent_id')))
    seen = set()

    def parents():
        yield from known
        for app in ('ocr', 'classification'):
            native = getattr(ctx.services, app + '_handoff', None)
            if native:
                offset = 0
                while True:
                    with closing(native._connect()) as db:
                        rows = db.execute(f'SELECT handoff_id FROM {app}_handoff WHERE workspace_id=? '
                            'ORDER BY updated_at DESC,handoff_id LIMIT 50 OFFSET ?', (ctx.workspace_id, offset)).fetchall()
                    yield from ((app, row['handoff_id']) for row in rows)
                    if len(rows) < 50:
                        break
                    offset += len(rows)

    for app, parent in parents():
        if only_ocr and app != 'ocr':
            continue
        if (app, parent) in seen:
            continue
        seen.add((app, parent))
        if getattr(ctx.services, app+'_handoff', None) is None:
            unavailable.append({'application': app, 'id': parent, 'reason': '原生交接服务未配置'})
            continue
        try:
            if app == 'ocr':
                from workbench_platform.application_services.operations.ocr import read, OcrRead
                try:
                    first = read(OcrRead(handoff_id=parent), ctx)
                except ValueError:
                    from workbench_platform.application_services.operations.pdf import bound, read as read_page, ReadPage
                    _, _, adapter, documents = bound(ctx, parent)
                    for document in documents:
                        pages = paged(lambda offset, limit: adapter.document_pages(document, offset=offset, limit=limit), 'pages')
                        for page in pages:
                            try:
                                value = read_page(ReadPage(handoff_id=parent, document_id=document, page_number=page['page_number']), ctx)
                            except ValueError:
                                continue
                            for ref in value['result_refs']:
                                yield ref, f'OCR · 第 {page["page_number"]} 页当前保存版本'
                else:
                    value = first
                    while True:
                        for ref in value['result_refs']:
                            yield ref, 'OCR · 当前识别/人工修改版本'
                        if value.get('next_offset') is None:
                            break
                        value = read(OcrRead(handoff_id=parent, offset=value['next_offset']), ctx)
            else:
                from workbench_platform.application_services.operations.classification import snapshot, reference
                yield reference(snapshot(ctx, parent), ctx), '分类 · 当前预测/人工标注版本'
        except ValueError:
            unavailable.append({'application': app, 'id': parent, 'reason': '原生结果未完成或服务未启动，可从工具页面继续'})
    forensic = None if only_ocr else getattr(ctx.services, 'forensic_service', None)
    if forensic:
        for value in paged(lambda offset, limit: forensic.list(ctx.workspace_id, offset, limit)):
            if value['status'] in {'succeeded', 'partial'}:
                yield AgentRef(kind='result', workspace_id=ctx.workspace_id, object_id=value['inspection_id'],
                    revision=value['revision'], sha256=value['report_hash'], application='forensics'), '取证 · 当前报告'
    bundles = () if only_ocr else paged(lambda offset, limit: ctx.services.artifact_center.list(ctx.workspace_id, offset, limit))
    for bundle in bundles:
        yield AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'],
                       revision=1, application='platform'), bundle['title']


def candidates(ctx, offset=0, limit=50, keyword='', related_only=False):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50 or len(keyword) > 100:
        raise ValueError('候选分页无效')
    task = ctx.services.agent.tasks.get(ctx.session_id)
    # Same-project discovery can show a new upload; accepting it requires an
    # explicit include_asset_ids choice and a separately verified return POST.
    selected = set(getattr(ctx, 'selected_assets', ()))
    task_refs = [reference for field in ('result_refs','initial_refs','job_refs','artifact_refs')
                 for reference in task.get(field,[])]
    objects = {(ref.get('application'),ref['object_id']) for ref in task_refs}
    parents = {(ref.get('application'),ref['parent_id']) for ref in task_refs if ref.get('parent_id')}
    discovery = replace(ctx, selected_assets=()) if selected else ctx
    items, unavailable, seen = [], [], set()
    matched = 0
    for ref, title in result_references(discovery, task, unavailable):
        value = ref.model_dump(mode='json') if isinstance(ref, AgentRef) else ref
        key = (value['kind'], value.get('application'), value.get('parent_id'), value['object_id'], str(value['revision']))
        if key in seen:
            continue
        try:
            checked = verify(value, discovery)
            sources = reference_assets(value, checked, discovery)
            assets = [ctx.services.asset_library[0].get(ctx.workspace_id, identifier) for identifier in sources]
        except ValueError:
            unavailable.append({'application': value.get('application'), 'id': value['object_id'], 'reason': '结果或原始资料暂不可核实'})
            continue
        seen.add(key)
        related = (bool(selected.intersection(sources)) or
                   (value.get('application'),value['object_id']) in objects or
                   bool(value.get('parent_id') and (value.get('application'),value['parent_id']) in parents))
        if related_only and not related:
            continue
        if keyword.casefold() not in (title + ' ' + ' '.join(asset.original_name for asset in assets)).casefold():
            continue
        required = [{'asset_id': asset.asset_id, 'name': asset.original_name} for asset in assets
                    if selected and asset.asset_id not in selected]
        if required:
            try:
                verify(value, ctx)
                required = []
            except ValueError:
                pass
        if matched >= offset:
            items.append({'reference': value, 'title': title, 'required_assets': required,
                          'related_to_task': related})
            if len(items) > limit:
                break
        matched += 1
    return {'items': items[:limit], 'offset': offset, 'limit': limit,
            'next_offset': offset + limit if len(items) > limit else None,
            'unavailable': unavailable[:20], 'native_jobs': task['job_refs']}

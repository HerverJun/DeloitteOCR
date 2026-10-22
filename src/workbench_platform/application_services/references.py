"""Verify typed results and server-created input lineage before composition."""
from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
from packages.contracts.agent import AgentRef
from workbench_platform.application_services.identity import digest
from .native import scoped_handoff


def reference_location(reference, task_id=None, evidence_id=None, offset=0):
    """Resolve object kind before its originating application's result route."""
    from urllib.parse import urlencode
    ref = AgentRef.model_validate(reference)
    query = {'workspace': ref.workspace_id}
    if task_id:
        query['task_return'] = task_id
    if evidence_id:
        query['evidence'] = evidence_id
    if offset:
        query['offset'] = offset
    if ref.kind == 'evidence':
        query.update(evidence=ref.object_id, session=task_id or ref.parent_id)
        path = '/findings'
    elif ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'audit_draft':
        query.update(session=ref.parent_id, draft=ref.object_id, revision=str(ref.revision), sha256=ref.sha256)
        path = '/findings'
    elif ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'documents':
        query['document'] = ref.object_id
        path = '/apps/ocr/documents'
    elif ref.kind == 'artifact':
        query['bundle'] = ref.object_id; path = '/artifacts'
    elif ref.kind in {'asset', 'asset_set'}:
        query['asset' if ref.kind == 'asset' else 'set'] = ref.object_id; path = '/assets'
    elif ref.kind == 'job':
        if ref.application == 'multimodal':
            query['analysis'] = ref.object_id
            path = {'face': '/apps/face', 'audio': '/apps/audio', 'seal': '/apps/ocr/seals',
                    'catnet': '/apps/forensics/deep', 'document_forensics':'/apps/forensics/document', 'aigc_image':'/apps/forensics/aigc'}[ref.coverage['kind']]
        elif ref.coverage.get('kind') == 'batch':
            query['batch'] = ref.object_id; path = '/batches'
        else:
            query['job'] = ref.object_id; path = '/jobs'
    elif ref.application == 'data':
        query.update(session=ref.parent_id)
        query[ref.kind] = ref.object_id; path = '/apps/data'
    elif ref.application == 'forensics':
        query['inspection'] = ref.object_id; path = '/apps/forensics'
    elif ref.application == 'multimodal':
        query.update(analysis=ref.parent_id, snapshot=ref.object_id, revision=str(ref.revision), sha256=ref.sha256)
        path = {'face': '/apps/face', 'audio': '/apps/audio', 'seal': '/apps/ocr/seals',
                'catnet': '/apps/forensics/deep', 'document_forensics':'/apps/forensics/document', 'aigc_image':'/apps/forensics/aigc'}[ref.coverage['kind']]
    elif ref.application in {'ocr', 'classification'}:
        if ref.coverage.get('origin') == 'native_binding':
            query['native_binding'] = ref.parent_id
        elif ref.parent_id:
            query['handoff'] = ref.parent_id
        query.update(native_result=ref.object_id, revision=str(ref.revision))
        path = '/apps/' + ref.application + '/workflow'
    else:
        raise ValueError('引用类型没有可定位的业务入口')
    return path + '?' + urlencode(query)


def leaf_assets(ctx, asset_id, visited=None):
    visited = set() if visited is None else set(visited)
    if asset_id in visited:
        raise ValueError('派生资料来源存在循环')
    visited.add(asset_id)
    rows = ctx.store.rows('SELECT * FROM derived_inputs WHERE workspace_id=? AND asset_id=?', (ctx.workspace_id, asset_id))
    if not rows:
        ctx.services.asset_library[0].get(ctx.workspace_id, asset_id)
        return [asset_id]
    from workbench_platform.supervisor.processes import require_f_path
    row = rows[0]
    recorded = Path(row['provenance_path'])
    path = require_f_path(recorded if recorded.is_absolute() else ctx.store.root / recorded)
    if not path.is_relative_to(ctx.store.root / 'bridges') or not path.is_file():
        raise ValueError('派生资料来源文件不可核实')
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != row['provenance_hash']:
        raise ValueError('派生资料来源哈希变化')
    provenance = json.loads(payload)
    asset = ctx.services.asset_library[0].get(ctx.workspace_id, asset_id)
    if (provenance.get('asset_id') != asset_id or provenance.get('input_sha256') != asset.blob_hash
            or provenance.get('sources') != json.loads(row['source_refs'])):
        raise ValueError('派生资料来源身份变化')
    leaves = json.loads(rows[0]['leaf_assets'])
    if not leaves:
        raise ValueError('派生资料缺少原始输入来源')
    return sorted({leaf for identifier in leaves for leaf in leaf_assets(ctx, identifier, visited)})


def asset_allowed(ctx, asset_id):
    leaves = leaf_assets(ctx, asset_id)
    return not ctx.selected_assets or asset_id in ctx.selected_assets or all(identifier in ctx.selected_assets for identifier in leaves)


def reference_asset_ids(reference, value):
    """Immediate source identities, preserving explicit derived-asset scope."""
    ref = AgentRef.model_validate(reference)
    if ref.kind == 'evidence':
        identifiers = [item['asset_id'] for item in value.get('source_assets') or value['sources']]
    elif ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'audit_draft':
        identifiers = value.get('source_assets', [])
    elif ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'documents':
        identifiers = [value['source']['asset_id']]
    elif ref.kind == 'asset':
        identifiers = [ref.object_id]
    elif ref.kind == 'asset_set':
        identifiers = [item['asset_id'] for item in value['asset_set']['items']]
    elif ref.kind == 'artifact':
        identifiers = [item['asset_id'] for item in value.get('inputs', []) if item.get('asset_id')]
        identifiers.extend(value.get('source_assets', []))
    elif ref.application == 'data':
        identifiers = value.get('source_assets', [])
    elif ref.application == 'ocr':
        identifiers = [value['source_asset']['asset_id']]
    elif ref.application == 'classification':
        identifiers = [item['asset_id'] for item in value.get('items', []) if item.get('asset_id')]
    elif ref.application == 'forensics':
        identifiers = [value['asset_id']]
    elif ref.application == 'multimodal':
        identifiers = [item['asset_id'] for item in value['sources']]
    else:
        identifiers = []
    return sorted(set(identifiers))


def reference_assets(reference, value, ctx):
    """Original leaf sources used for explicit manual scope additions."""
    return sorted({leaf for identifier in reference_asset_ids(reference, value) for leaf in leaf_assets(ctx, identifier)})


def data_sources(ctx, session_id, tables):
    service = ctx.services.data_workbench
    sources = []
    for table in tables:
        source = table['source']
        upload = service.upload_source(session_id, source['asset_id'])
        if upload['sha256'] != source['sha256']:
            raise ValueError('数据表原始来源哈希变化')
        if upload['source_asset_id']:
            if upload['source_workspace_id'] != ctx.workspace_id or not asset_allowed(ctx, upload['source_asset_id']):
                raise ValueError('数据作业输入超出会话范围')
            asset = ctx.services.asset_library[0].get(ctx.workspace_id, upload['source_asset_id'])
            if (asset.revision, asset.blob_hash) != (upload['source_asset_revision'], upload['source_blob_hash']):
                raise ValueError('数据来源版本变化')
            sources.append(asset.asset_id)
        elif ctx.selected_assets:
            raise ValueError('此数据表缺少选中资料的来源映射')
    return sources


def data_scope(ctx, session):
    if session['workspace_id'] != ctx.workspace_id:
        raise ValueError('跨项目数据作业')
    return data_sources(ctx, session['id'], list(session['state']['tables'].values()))


def data_reference(ctx, session_id, kind, revision):
    service = ctx.services.data_workbench
    session = service.get(session_id)
    if session['workspace_id'] != ctx.workspace_id:
        raise ValueError('跨项目数据引用')
    value = service.page(session_id, kind, revision, 0, 50)
    tables = ([value['metadata']['left'], value['metadata']['right']] if kind == 'result' else
        [table for table in session['state']['tables'].values()
         if revision in {item['revision'] for item in table.get('versions', table['history'])}])
    sources = data_sources(ctx, session_id, tables)
    from modules.data_workbench.recipes import RecipeStore
    recipes = RecipeStore(service.session_dir(session_id) / 'recipes.sqlite3')
    snapshots = [recipes.get(table['revision'] if kind == 'result' else revision)['event']['snapshot_sha256'] for table in tables]
    content = hashlib.sha256()
    content.update(json.dumps({'kind': kind, 'revision': revision, 'metadata': value['metadata'], 'snapshots': snapshots},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'))
    path = service.session_dir(session_id) / 'views.sqlite3'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        query, key = ('table_rows', 'revision') if kind == 'table' else ('result_rows', 'id')
        for ordinal, payload in db.execute(f'SELECT ordinal,payload FROM {query} WHERE {key}=? ORDER BY ordinal', (revision,)):
            content.update(str(ordinal).encode('ascii') + b'\0' + payload.encode('utf-8') + b'\n')
    return {**value, 'content_sha256': content.hexdigest(), 'source_assets': sources}


def verify(reference, ctx):
    ref = AgentRef.model_validate(reference)
    if ref.workspace_id != ctx.workspace_id:
        raise ValueError('跨项目引用')
    if ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'documents':
        from workbench_platform.application_services.operations.documents import scoped
        value = scoped(ctx, ref.object_id)
        if type(ref.revision) is not int or ref.parent_id != ref.object_id:
            raise ValueError('文档结果版本、来源或哈希变化')
        value = ctx.services.document_service.result_snapshot(ctx.workspace_id, ref.object_id, ref.revision, ref.sha256)
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, value['asset_id'])
        if (asset.revision, asset.blob_hash) != (value['asset_revision'], value['blob_hash']):
            raise ValueError('文档原件版本变化')
        return {**value, 'content_sha256': value['result_hash']}
    if ref.application == 'multimodal' and ref.kind == 'result':
        from workbench_platform.application_services.operations.multimodal import checked_snapshot, Result
        if not ref.parent_id:
            raise ValueError('多模态结果缺少原作业身份')
        value = checked_snapshot(ctx, Result(job_id=ref.parent_id, snapshot_id=ref.object_id))
        if (ref.sha256 != value['snapshot_hash'] or ref.revision != value['revision']
                or ref.coverage.get('kind', value['kind']) != value['kind']
                or ref.coverage.get('generation', value['generation']) != value['generation']):
            raise ValueError('不可变结果快照身份不一致')
        # Bytes and sources are checked on read, not inferred from final prose.
        for source in value['sources']:
            asset = ctx.services.asset_library[0].get(ctx.workspace_id, source['asset_id'])
            if (asset.revision, asset.blob_hash) != (source['asset_revision'], source['blob_hash']):
                raise ValueError('多模态原件身份变化')
        return {**value, 'content_sha256': value['snapshot_hash']}
    if ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'audit_draft':
        drafts = ctx.services.audit_service.drafts
        with closing(drafts.store.db()) as db:
            row = db.execute('SELECT h.body,d.workspace_id,d.task_id FROM audit_draft_history h '
                'JOIN audit_finding_drafts d ON d.draft_id=h.draft_id WHERE h.draft_id=? AND h.revision=?',
                (ref.object_id, ref.revision)).fetchone()
        if row is None or (row['workspace_id'], row['task_id']) != (ctx.workspace_id, ref.parent_id):
            raise ValueError('草稿引用的项目、所属任务或版本无效')
        value = json.loads(row['body'])
        if (type(ref.revision) is not int or value['revision'] != ref.revision
                or value['draft_id'] != ref.object_id or value['task_id'] != ref.parent_id
                or value['workspace_id'] != ref.workspace_id or ref.sha256 != digest(value)):
            raise ValueError('草稿引用的版本或内容哈希变化')
        sources = set()
        for identifier in value['evidence_ids']:
            # Verify source bytes against the reader's actual current scope,
            # rather than silently assuming the original author's scope.
            evidence = drafts.evidence(identifier, ctx)
            if value['evidence_hashes'].get(identifier) != evidence['snapshot_hash']:
                raise ValueError('草稿固定证据哈希变化')
            sources.update(item['asset_id'] for item in evidence.get('source_assets', evidence.get('sources', [])))
        return {**value, 'content_sha256': ref.sha256, 'source_assets': sorted(sources)}
    if ref.kind == 'evidence':
        value = ctx.services.audit_service.drafts.evidence(ref.object_id, ctx)
        if ref.revision != 1 or ref.sha256 != value['snapshot_hash']:
            raise ValueError('固定证据引用哈希或版本变化')
        return value
    if ref.kind == 'asset':
        if not asset_allowed(ctx, ref.object_id):
            raise ValueError('资料超出会话范围')
        registry = ctx.services.asset_library[0]
        asset = registry.get(ctx.workspace_id, ref.object_id)
        if (asset.revision, asset.blob_hash) != (ref.revision, ref.sha256):
            raise ValueError('资料引用失效')
        with registry.stage_verified(ctx.workspace_id, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash):
            pass
        return {'asset': asset.model_dump(mode='json')}
    if ref.kind == 'asset_set':
        from .native import scoped_set
        value = scoped_set(ctx, ref.object_id, ref.revision)
        if ref.sha256 != value.manifest_hash:
            raise ValueError('集合哈希变化')
        return {'asset_set': value.model_dump(mode='json')}
    if ref.kind == 'result' and ref.application in {'ocr', 'classification'} and ref.parent_id:
        from .native_mapping import binding, verify_reference
        imported = binding(ctx, ref.parent_id)
        if imported:
            return verify_reference(ctx, ref, imported)
    if ref.application == 'ocr' and ref.kind == 'result' and ref.parent_id:
        service = ctx.services.ocr_handoff
        row = scoped_handoff(ctx, service, ref.parent_id)
        adapter = service._adapter(service._context(ctx.workspace_id, row['link_id']))
        snapshot = adapter.snapshot()
        native = adapter.import_status(row['request_id'])
        allowed_images = {item.get('target_asset_id') for item in native['items'] if item.get('target_asset_id')}
        allowed_documents = {item.get('target_document_id') for item in native['items'] if item.get('target_document_id')}
        allowed_images.update(image['id'] for image in snapshot['images'] if image.get('document_id') in allowed_documents)
        tasks = [task for task in snapshot['tasks'] if task.get('result_id') == ref.object_id
                 and task.get('image_id') in allowed_images and task['status'] == 'succeeded']
        if len(tasks) != 1:
            raise ValueError('OCR结果不属于固定输入')
        task = tasks[0]
        with closing(service._connect()) as db:
            page_submission = db.execute('SELECT * FROM ocr_page_submission WHERE handoff_id=?', (ref.parent_id,)).fetchone()
        if page_submission:
            receipt = adapter.page_request(page_submission['document_id'], page_submission['request_id'])
            if ref.object_id not in {(stage.get('output') or {}).get('result_id') for stage in receipt.get('stages', []) if stage['status'] == 'succeeded'}:
                raise ValueError('页结果不属于本次请求')
        result = adapter.results([task['id']])[0]
        value = adapter.result_snapshot(result, ref.revision)
        tables = adapter.result_tables(result, ref.revision)
        if not value['available'] or tables['content_sha256'] != ref.sha256:
            raise ValueError('OCR快照不可核实')
        member_set = ctx.services.asset_library[1].get(ctx.workspace_id, row['asset_set_id'], row['asset_set_revision'])
        matched = next((item for item in native['items'] if item.get('target_asset_id') == task['image_id']
                        or item.get('target_document_id') == result.locator.get('document_id')), None)
        if matched is None:
            raise ValueError('OCR原资料映射不可核实')
        member = next(item for item in member_set.items if item.item_id == matched['item_id'])
        return {'snapshot': value, 'table_snapshot': tables, 'locator': result.locator, 'source_asset': member.model_dump(mode='json'),
                'selected': result.selected, 'current_revision': adapter.result_revision(result)}
    if ref.application == 'classification' and ref.kind == 'result' and ref.parent_id:
        from workbench_platform.application_services.operations.classification import snapshot
        value = snapshot(ctx, ref.parent_id)
        if ref.object_id != value['native_job_id'] or ref.sha256 != digest(value['items']) or ref.revision != digest(value['items']):
            raise ValueError('分类结果版本变化')
        return value
    if ref.application == 'forensics' and ref.kind == 'result':
        service = ctx.services.forensic_service
        value = service.get(ctx.workspace_id, ref.object_id)
        if not asset_allowed(ctx, value['asset_id']) or (value['revision'], value['report_hash']) != (ref.revision, ref.sha256):
            raise ValueError('报告来源或版本变化')
        service.file(ctx.workspace_id, ref.object_id, 'report.json')
        return value
    if ref.application == 'data' and ref.kind in {'table', 'result'} and ref.parent_id:
        if ref.object_id != ref.revision:
            raise ValueError('数据引用不属于当前作业版本')
        value = data_reference(ctx, ref.parent_id, ref.kind, ref.object_id)
        if ref.sha256 != value['content_sha256']:
            raise ValueError('数据结果引用哈希变化')
        return value
    if ref.kind == 'artifact':
        center = ctx.services.artifact_center
        value = center.get(ctx.workspace_id, ref.object_id)
        if ref.revision != 1:
            raise ValueError('成果引用版本无效')
        for source in value.get('inputs', []):
            if source.get('asset_id') and not asset_allowed(ctx, source['asset_id']):
                raise ValueError('成果来源超出会话范围')
        identities = []
        source_assets = set()
        for file in value['files']:
            artifact = center.file(ctx.workspace_id, ref.object_id, file['name'])
            for source in artifact.source_refs:
                if not asset_allowed(ctx, source.asset_id):
                    raise ValueError('成果文件来源超出会话范围')
                source_assets.add(source.asset_id)
            with center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash):
                pass
            identities.append({'name': file['name'], 'id': artifact.artifact_id,
                               'revision': artifact.revision, 'sha256': artifact.blob_hash})
        content_hash = digest({'bundle_id': ref.object_id, 'inputs': value.get('inputs', []),
                               'files': sorted(identities, key=lambda item: item['name'])})
        if ref.sha256 is not None and ref.sha256 != content_hash:
            raise ValueError('成果引用内容变化')
        return {**value, 'content_sha256': content_hash, 'source_assets': sorted(source_assets)}
    raise ValueError('引用类型不支持核实')

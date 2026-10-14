"""Bring native imports back by verified source bytes, never by host paths."""
from contextlib import closing
from dataclasses import replace, asdict
import hashlib
import http.client
import json
import mimetypes
import uuid
from pathlib import Path
from packages.contracts.agent import AgentRef
from packages.contracts.models import AssetSetItem, SourceLocator
from packages.contracts.uploads import UploadBinding
from workbench_platform.agent.store import digest, canonical
from workbench_platform.application_services.references import asset_allowed
from workbench_platform.supervisor.processes import require_f_path


def ensure_table(ctx):
    with ctx.store.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS native_bindings (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, application TEXT NOT NULL, link_id TEXT NOT NULL, object_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(workspace_id,application,link_id,object_id))')


def binding(ctx, parent_id):
    if not getattr(ctx, 'store', None) or not ctx.store.rows("SELECT name FROM sqlite_master WHERE name='native_bindings'"):
        return None
    rows = ctx.store.rows('SELECT * FROM native_bindings WHERE id=? AND workspace_id=?', (parent_id, ctx.workspace_id))
    if not rows:
        return None
    return dict(rows[0]) | json.loads(rows[0]['payload'])


def adapter_for(ctx, app, link_id):
    jobs = ctx.services.job_service
    return jobs._adapter(jobs._context(ctx.workspace_id, app, link_id))


def registered_source(ctx, adapter, app, kind, source):
    registry = ctx.services.asset_library[0]
    # Match content, then still download and verify the native source bytes.
    matches = []
    offset = 0
    while True:
        items, total = registry.list(ctx.workspace_id, offset=offset, limit=200)
        matches.extend(item for item in items if item['blob_hash'] == source['sha256'])
        offset += len(items)
        if not items or offset >= total:
            break
    folder = require_f_path(registry.roots['uploads'])
    request_id = 'n'+digest([ctx.workspace_id, app, adapter.context.link_id, kind, source['id'], source['sha256']])[:40]
    target = folder / ('.native-'+request_id+'-'+uuid.uuid4().hex+'.tmp')
    connection = http.client.HTTPConnection('127.0.0.1', adapter.port, timeout=10)
    path = (f'/api/v1/assets/{source["id"]}/image' if app == 'classification' else
            f'/api/projects/{adapter.context.module_project_id}/sources/{kind}/{source["id"]}')
    headers = {'Authorization': 'Bearer '+adapter._credential()} if app == 'ocr' else {'X-Platform-Request': '1'}
    try:
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('原生原资料暂不可读取')
        content = hashlib.sha256(); count = 0
        with target.open('wb') as stream:
            while chunk := response.read(1024*1024):
                count += len(chunk)
                if count > 128*1024*1024:
                    raise ValueError('原资料超过单次回传范围')
                content.update(chunk); stream.write(chunk)
        if not count or content.hexdigest() != source['sha256']:
            raise ValueError('原生原资料哈希不一致')
        if matches:
            chosen = next((item for item in matches if item['asset_id'] in ctx.selected_assets), matches[0])
            asset = registry.get(ctx.workspace_id, chosen['asset_id'])
            with registry.stage_verified(ctx.workspace_id, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash):
                pass
            return asset
        name = Path(source['name'].replace('\\', '/')).name
        command = UploadBinding(schema_version='1.0', workspace_id=ctx.workspace_id, request_id=request_id,
            name=name, sha256=source['sha256'], size_bytes=count)
        registry.reserve_upload(command)
        status, _ = registry.commit_upload(command, target, media_type=mimetypes.guess_type(name)[0] or 'application/octet-stream')
        return status.receipt.asset
    finally:
        connection.close(); target.unlink(missing_ok=True)


def attach(ctx, app, link_id, object_id):
    ensure_table(ctx)
    adapter = adapter_for(ctx, app, link_id)
    identity = 'n'+digest([ctx.workspace_id, app, link_id, object_id])[:40]
    prior = binding(ctx, identity)
    if prior:
        verify_sources(ctx, prior)
        return prior
    sources, native_items = {}, []
    if app == 'ocr':
        snapshot = adapter.snapshot()
        tasks = [item for item in snapshot['tasks'] if item.get('result_id') == object_id and item['status'] == 'succeeded']
        if len(tasks) != 1:
            raise ValueError('OCR结果未完成或不属于当前关联项目')
        image = next(item for item in snapshot['images'] if item['id'] == tasks[0]['image_id'])
        if image.get('document_id'):
            source = next(item for item in snapshot['documents'] if item['id'] == image['document_id'])
            kind = 'document'
        else:
            source, kind = image, 'image'
        asset = registered_source(ctx, adapter, app, kind, source)
        sources[source['id']] = {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash,
                                'native_kind': kind, 'native_id': source['id']}
        native_items = [tasks[0]['id']]
    else:
        job = adapter.job(object_id)
        if job.kind != 'inference' or job.state not in {'succeeded', 'completed_with_errors', 'failed', 'cancelled'}:
            raise ValueError('仅回传已结束的实际分类推理')
        for offset in range(0, job.total, 200):
            native_items.extend(asdict(item) for item in adapter.results(object_id, limit=min(200, job.total-offset), offset=offset))
        if len(native_items) != job.total or job.total > 5000:
            raise ValueError('分类结果范围不完整')
        for native_id in dict.fromkeys(item['asset_id'] for item in native_items if item['asset_id']):
            source = adapter._request('GET', f'/api/v1/projects/{adapter.project_id}/assets/{native_id}')
            asset = registered_source(ctx, adapter, app, 'image', source)
            sources[native_id] = {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash,
                                  'native_kind': 'image', 'native_id': native_id}
    if not sources:
        raise ValueError('结果缺少可核实的原资料')
    members = []
    for index, source in enumerate(sources.values()):
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, source['asset_id'])
        members.append(AssetSetItem(schema_version='1.0', item_id='i'+str(index), asset_id=asset.asset_id, asset_revision=asset.revision,
            blob_hash=asset.blob_hash, source_locator=SourceLocator(schema_version='1.0', kind='file',
                asset_id=asset.asset_id, asset_revision=asset.revision, blob_hash=asset.blob_hash, precision='exact')))
    fixed = ctx.services.asset_library[1].create(ctx.workspace_id, members, asset_set_id='s'+identity[1:])
    payload = {'sources': sources, 'asset_set_id': fixed.asset_set_id, 'asset_set_revision': fixed.revision, 'native_items': native_items}
    with ctx.store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO native_bindings VALUES(?,?,?,?,?,?)', (identity, ctx.workspace_id, app, link_id, object_id, canonical(payload)))
    return binding(ctx, identity)


def verify_sources(ctx, value):
    for source in value['sources'].values():
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, source['asset_id'])
        if not asset_allowed(ctx, asset.asset_id) or (asset.revision, asset.blob_hash) != (source['asset_revision'], source['blob_hash']):
            raise ValueError('原生回传资料未纳入当前任务范围或版本变化')
        with ctx.services.asset_library[0].stage_verified(ctx.workspace_id, asset.asset_id, revision=asset.revision, blob_hash=asset.blob_hash):
            pass


def read(ctx, value):
    verify_sources(ctx, value)
    adapter = adapter_for(ctx, value['application'], value['link_id'])
    if value['application'] == 'ocr':
        result = next(item for item in adapter.results(value['native_items']) if item.native_result_id == value['object_id'])
        revision = adapter.result_revision(result); tables = adapter.result_tables(result, revision)
        return AgentRef(kind='result', workspace_id=ctx.workspace_id, application='ocr', object_id=value['object_id'],
            native_id=value['object_id'], parent_id=value['id'], revision=revision, sha256=tables['content_sha256'], coverage={'origin': 'native_binding'})
    snapshot = classification_snapshot(ctx, value)
    return AgentRef(kind='result', workspace_id=ctx.workspace_id, application='classification', object_id=value['object_id'],
        native_id=value['object_id'], parent_id=value['id'], revision=digest(snapshot['items']), sha256=digest(snapshot['items']), coverage={'origin': 'native_binding'})


def classification_snapshot(ctx, value):
    verify_sources(ctx, value)
    adapter = adapter_for(ctx, 'classification', value['link_id'])
    raw = adapter._request('GET', '/api/v1/jobs/'+value['object_id'])
    items = []
    job = adapter.job(value['object_id'])
    for offset in range(0, job.total, 200):
        for item in adapter.results(value['object_id'], limit=min(200, job.total-offset), offset=offset):
            source = value['sources'].get(item.asset_id)
            if not source:
                raise ValueError('分类结果原资料范围变化')
            items.append(asdict(item) | {'native_item_id': item.native_item_id, 'native_asset_id': item.asset_id,
                'asset_id': source['asset_id'], 'source_name': ctx.services.asset_library[0].get(ctx.workspace_id, source['asset_id']).original_name})
    if len(items) != len(value['native_items']) or {item['native_item_id'] for item in items} != {item['native_item_id'] for item in value['native_items']}:
        raise ValueError('分类回传结果集合变化')
    for native_id, source in value['sources'].items():
        current = adapter._request('GET', f'/api/v1/projects/{adapter.project_id}/assets/{native_id}')
        if current['sha256'] != source['blob_hash']:
            raise ValueError('分类原件映射内容变化')
    projection = ctx.services.job_service.register(ctx.workspace_id, value['link_id'], 'classification', [value['object_id']])
    return {'handoff_id': value['id'], 'model_id': raw['params']['model_id'], 'project_id': adapter.project_id,
            'native_job_id': value['object_id'], 'input_set_id': value['asset_set_id'], 'input_set_revision': value['asset_set_revision'],
            'items': items, 'platform_job': projection, 'inputs': list(value['sources'].values()), 'native_binding': True}


def verify_reference(ctx, ref, value):
    verify_sources(ctx, value)
    if value['application'] != ref.application or value['object_id'] != ref.object_id:
        raise ValueError('原生回传身份变化')
    if ref.application == 'classification':
        snapshot = classification_snapshot(ctx, value)
        if ref.sha256 != digest(snapshot['items']) or ref.revision != digest(snapshot['items']):
            raise ValueError('分类回传版本变化')
        return snapshot
    adapter = adapter_for(ctx, 'ocr', value['link_id'])
    result = next(item for item in adapter.results(value['native_items']) if item.native_result_id == ref.object_id)
    snapshot = adapter.result_snapshot(result, ref.revision); tables = adapter.result_tables(result, ref.revision)
    if not snapshot['available'] or ref.sha256 != tables['content_sha256']:
        raise ValueError('OCR回传版本变化')
    source = value['sources'].get(result.locator.get('document_id') or result.image_id)
    if source is None or snapshot['original_sha256'] != next(image['sha256'] for image in adapter.snapshot()['images'] if image['id'] == result.image_id):
        raise ValueError('OCR原资料身份变化')
    state = adapter.snapshot()
    native_sources = state['documents'] if source['native_kind'] == 'document' else state['images']
    if not any(item['id'] == source['native_id'] and item['sha256'] == source['blob_hash'] for item in native_sources):
        raise ValueError('OCR原件映射内容变化')
    return {'snapshot': snapshot, 'table_snapshot': tables, 'locator': result.locator, 'source_asset': source,
            'selected': result.selected, 'current_revision': adapter.result_revision(result)}

"""Fix direct native results as evidence without creating a synthetic batch."""
from __future__ import annotations

from packages.contracts.agent import AgentRef
from packages.contracts.audit import DirectEvidenceSnapshot
from packages.contracts.models import SourceLocator
from workbench_platform.application_services.references import verify, reference_location, reference_asset_ids
from workbench_platform.agent.store import canonical, digest, now


def source_locator(asset, *, row=None, sheet=None, page=None):
    value = {'schema_version': '1.0', 'kind': 'file', 'asset_id': asset.asset_id,
             'asset_revision': asset.revision, 'blob_hash': asset.blob_hash, 'precision': 'exact'}
    if page:
        value.update(kind='page', page_number=page, precision='page_only')
    if row and (sheet or page):
        value.update(kind='row', row_number=row, table_number=1, sheet_name=sheet, page_number=page, precision='exact')
    return SourceLocator.model_validate(value).model_dump(mode='json')


def locate(reference, task_id, evidence_id=None, offset=0):
    return reference_location(reference, task_id, evidence_id, offset)


def snapshot(args, ctx, *, legacy=False):
    reference = args.reference.model_dump(mode='json')
    value = verify(reference, ctx)
    registry = ctx.services.asset_library[0]
    sources = []
    def add(source):
        if source not in sources:
            sources.append(source)
    ref = args.reference
    if ref.kind == 'artifact':
        observation = value
        for member in value.get('inputs', []):
            if member.get('asset_id'):
                add(source_locator(registry.get(ctx.workspace_id, member['asset_id'])))
        for member in value.get('files', []):
            artifact=ctx.services.artifact_center.file(ctx.workspace_id,ref.object_id,member['name'])
            for source in artifact.source_refs:add(source.model_dump(mode='json'))
        summary = '固定已生成的成果及原始输入。'
    elif ref.kind == 'asset':
        asset = registry.get(ctx.workspace_id, ref.object_id)
        add(source_locator(asset)); observation = value; summary = '固定原资料的内容哈希与版本。'
    elif ref.kind == 'asset_set':
        observation = value
        for member in value['asset_set']['items']:
            add(source_locator(registry.get(ctx.workspace_id, member['asset_id'])))
        summary = '固定资料集合的版本、内容哈希与原始输入。'
    elif ref.application == 'data' and ref.kind in {'table', 'result'}:
        service = ctx.services.data_workbench
        observation = service.page(ref.parent_id, ref.kind, ref.object_id, args.offset, args.limit)
        metadata = observation['metadata']
        tables = [metadata['left'], metadata['right']] if ref.kind == 'result' else [next(table for table in service.get(ref.parent_id)['state']['tables'].values() if ref.object_id in {v['revision'] for v in table.get('versions', table['history'])})]
        for i, table in enumerate(tables):
            upload = service.upload_source(ref.parent_id, table['source']['asset_id'])
            if not upload['source_asset_id']:
                continue
            asset = registry.get(ctx.workspace_id, upload['source_asset_id'])
            sheet = table['source'].get('sheet_name') or table.get('options', {}).get('sheet_name') or ('CSV' if table.get('format') == 'csv' else None)
            for row in observation['rows']:
                row_number = row.get(('left' if i == 0 else 'right') + '_source_row') if ref.kind == 'result' else row.get('source_row')
                if row_number:
                    add(source_locator(asset, row=int(row_number), sheet=sheet))
                for original in row.get('source_rows', []):
                    add(source_locator(asset, row=original['source_row'], sheet=sheet))
        observation['window'] = {'offset': args.offset, 'limit': args.limit}
        summary = '固定数据结果与规则口径；本证据保存所选分页，完整结果仍可按版本读取。'
    elif ref.kind == 'result' and ref.application == 'ocr':
        member = value['source_asset']
        asset = registry.get(ctx.workspace_id, member['asset_id'])
        page = value['locator'].get('page_number')
        add(source_locator(asset, page=page))
        observation = {'snapshot': value['snapshot'], 'tables': value['table_snapshot'], 'locator': value['locator']}
        summary = '固定OCR识别版本，保留原文、人工修改及页来源。'
    elif ref.kind == 'result' and ref.application == 'classification':
        items = value['items'][args.offset:args.offset + args.limit]
        for item in items:
            if item.get('asset_id'):
                add(source_locator(registry.get(ctx.workspace_id, item['asset_id'])))
        observation = {'items': items, 'total': len(value['items']), 'model_id': value['model_id'], 'window': {'offset': args.offset, 'limit': args.limit}}
        summary = '固定分类结果版本，模型预测与人工类别分开保存。'
    elif ref.kind == 'result' and ref.application == 'forensics':
        import json
        content, _ = ctx.services.forensic_service.file(ctx.workspace_id, ref.object_id, 'report.json')
        observation = json.loads(content)
        add(source_locator(registry.get(ctx.workspace_id, value['asset_id'])))
        summary = '固定取证报告与算法线索；不能据此自动裁定真伪。'
    else:
        raise ValueError('当前结果类型不能作为审计证据')
    if len(canonical(observation).encode('utf-8')) > 192 * 1024:
        raise ValueError('证据窗口过大，请缩小分页')
    source_assets = [source_locator(registry.get(ctx.workspace_id, identifier))
                     for identifier in reference_asset_ids(reference, value)]
    content = {'reference': reference, 'observation': observation, 'sources': sources}
    if not legacy:
        content.update(sources=sources[:200], source_count=len(sources), source_assets=source_assets)
    key = digest(content)
    evidence_id = 'e' + digest([ctx.session_id, key])[:40]
    return DirectEvidenceSnapshot(snapshot_version=1 if legacy else 2, source_assets=[] if legacy else source_assets,
        evidence_id=evidence_id, workspace_id=ctx.workspace_id,
        task_id=ctx.session_id, reference=AgentRef.model_validate(reference), snapshot_hash=key, summary=summary,
        sources=sources[:200], source_count=len(sources), observation=observation, created_at=now(), location_url=locate(reference, ctx.session_id, evidence_id, args.offset)).model_dump(mode='json')

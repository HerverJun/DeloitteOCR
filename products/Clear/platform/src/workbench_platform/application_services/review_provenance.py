"""Trace review obligations through immutable business result provenance.

Wrapping an unreviewed result in a file, data table, evidence or finding does
not turn it into a human-confirmed result. This reader never changes review
state, starts a worker, or replaces a pinned result with the current result.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path

from packages.contracts.agent import AgentRef
from workbench_platform.application_services.identity import canonical, digest
from .references import verify, asset_allowed, leaf_assets, reference_asset_ids


def inspect_review_provenance(reference, ctx, *, verified=None, require_review=False):
    """Return checks, draft state and fixed native identities for a verified ref.

    ``verified`` is an optional result already returned by references.verify.
    Bad identities, changed source bytes and cyclic provenance raise ValueError.
    Missing human confirmation produces a failed check only when required;
    ordinary draft analysis remains allowed and retains its draft marker.
    """
    active, finished, snapshots, unknown, obligations = set(), set(), {}, {}, set()
    visits = 0

    def key(ref):
        return canonical({field: ref.get(field) for field in
            ('kind', 'application', 'workspace_id', 'object_id', 'parent_id', 'revision', 'sha256')})

    def source_asset(identifier):
        if not asset_allowed(ctx, identifier):
            raise ValueError('复核来源超出当前任务资料范围')
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, identifier)
        walk(AgentRef(kind='asset', workspace_id=ctx.workspace_id, object_id=asset.asset_id,
            revision=asset.revision, sha256=asset.blob_hash))

    def fixed_snapshot(job_id, snapshot_id, expected_hash=None):
        if not job_id or not snapshot_id:
            raise ValueError('复核来源缺少固定作业或结果身份')
        from workbench_platform.application_services.operations.multimodal import checked_snapshot, Result
        value = checked_snapshot(ctx, Result(job_id=job_id, snapshot_id=snapshot_id))
        if expected_hash is not None and value['snapshot_hash'] != expected_hash:
            raise ValueError('派生成果的原始结果快照哈希变化')
        identity = (value['job_id'], value['snapshot_id'])
        previous = snapshots.get(identity)
        if previous is not None and previous['snapshot_hash'] != value['snapshot_hash']:
            raise ValueError('复核来源的固定身份不一致')
        snapshots[identity] = value

    def evidence(value):
        content = {field: value[field] for field in ('reference', 'observation', 'sources')}
        if value.get('snapshot_version', 1) == 2:
            content.update(source_count=value['source_count'], source_assets=value['source_assets'])
        elif value.get('source_count', 0) > len(value['sources']):
            raise ValueError('旧固定证据缺少完整复核来源')
        if value.get('snapshot_hash') != digest(content):
            raise ValueError('固定证据复核来源哈希变化')
        source = value['reference']
        if source.get('kind') == 'result' and source.get('application') == 'classification':
            # The outer evidence hash fixes the observed labels/predictions.
            # Current editable labels may differ; retain the captured facts
            # and stale warning instead of substituting the current result.
            observation = value.get('observation') or {}
            if not observation.get('model_id') or not isinstance(observation.get('items'), list):
                raise ValueError('固定分类证据缺少模型身份或原观察记录')
        else:
            walk(source)
        for source in value.get('source_assets') or value['sources']:
            source_asset(source['asset_id'])

    def derived(identifier):
        rows = ctx.store.rows('SELECT * FROM derived_inputs WHERE workspace_id=? AND asset_id=?',
                              (ctx.workspace_id, identifier))
        if not rows:
            return
        # leaf_assets verifies input hash, ledger source identities, F-drive
        # boundaries and the provenance file hash before reading its contents.
        leaf_assets(ctx, identifier)
        row = rows[0]
        from workbench_platform.supervisor.processes import require_f_path
        recorded = Path(row['provenance_path'])
        path = require_f_path(recorded if recorded.is_absolute() else ctx.store.root / recorded)
        if not path.is_relative_to(ctx.store.root / 'bridges'):
            raise ValueError('派生复核来源路径无效')
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row['provenance_hash']:
            raise ValueError('派生复核来源哈希变化')
        value = json.loads(payload)
        for source in json.loads(row['source_refs']):
            walk(source)
        provenance = value.get('provenance') or {}
        if provenance.get('kind') in {'manual_seal_draft', 'manual_seal_fields'}:
            fixed_snapshot(provenance.get('analysis_id'), provenance.get('snapshot_id'), provenance.get('snapshot_hash'))
            if provenance.get('draft') or provenance.get('review_state') == 'unreviewed':
                obligations.add((provenance['analysis_id'], provenance['snapshot_id']))

    def audit_bundle(ref, value):
        # These catalogs deliberately return compact summaries. Their durable
        # publication snapshots retain the complete fixed evidence graph.
        tables = {'audit_draft': 'audit_draft_bundle', 'audit_workpaper': 'audit_workpaper_bundle'}
        table = tables.get(value.get('kind'))
        if not table:
            return
        center = ctx.services.artifact_center
        with closing(center._connect()) as db:
            row = db.execute(f'SELECT snapshot,snapshot_hash FROM {table} '
                'WHERE workspace_id=? AND bundle_id=? AND status=?',
                (ctx.workspace_id, ref.object_id, 'ready')).fetchone()
        if row is None:
            raise ValueError('底稿固定复核来源不存在')
        content = json.loads(row['snapshot'])
        if digest(content) != row['snapshot_hash'] or value.get('snapshot_hash') != row['snapshot_hash']:
            raise ValueError('底稿固定复核来源哈希变化')
        for item in content.get('evidence', []):
            captured = item.get('snapshot', item)
            if captured.get('scenario') == 'direct_tool':
                evidence(captured)
        for bundle in content.get('review_refs', []):
            walk(AgentRef(kind='artifact', workspace_id=ctx.workspace_id,
                object_id=bundle['bundle_id'], revision=1))

    def legacy_manual_bundle(ref, value):
        """Check a legacy immutable export against its original review receipt."""
        identity = {'bundle_id': ref.object_id, 'job_id': value.get('platform_job_id')}
        exported = next((file for file in value.get('files', []) if file['name'] == 'result.json'), None)
        if not exported:
            unknown[ref.object_id] = identity
            return
        center = ctx.services.artifact_center
        artifact = center.file(ctx.workspace_id, ref.object_id, 'result.json')
        if artifact.size_bytes > 64 * 1024 * 1024:
            raise ValueError('旧多模态复核导出过大，不能在当前边界内核实')
        with center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            with path.open('r', encoding='utf-8') as stream:
                analysis = json.load(stream)
        if (analysis.get('workspace_id') != ctx.workspace_id or analysis.get('job_id') != identity['job_id']
                or analysis.get('revision') != value.get('review_revision')):
            raise ValueError('旧多模态固定导出身份与成果账本不一致')
        result_hash = analysis.get('result_hash')
        if not isinstance(result_hash, str) or len(result_hash) != 64 or not analysis.get('inputs'):
            raise ValueError('旧多模态固定导出缺少可核实的算法结果哈希或原件')
        review = analysis.get('review') or {}
        if review.get('assessment') != 'confirmed' or not review.get('request_id') or not review.get('actor'):
            unknown[ref.object_id] = identity
            return
        native = ctx.services.multimodal_service
        saved = native.lookup_operation(ctx.workspace_id, 'review', review['request_id'])
        if not saved['found']:
            unknown[ref.object_id] = identity
            return
        receipt = saved['receipt']
        if any(receipt.get(field) != analysis.get(field) for field in
                ('workspace_id', 'job_id', 'generation', 'revision', 'result_hash', 'inputs', 'review')):
            raise ValueError('旧多模态固定导出与原用户复核回执不一致')
        from workbench_platform.data_api.service import DataError
        try:
            frozen = native.snapshot_at_revision(ctx.workspace_id, analysis['job_id'], analysis['revision'])
        except DataError:
            unknown[ref.object_id] = identity
            return
        fixed_snapshot(frozen['job_id'], frozen['snapshot_id'], frozen['snapshot_hash'])

    def walk(reference, supplied=None):
        nonlocal visits
        ref = AgentRef.model_validate(reference)
        identity = key(ref.model_dump())
        if identity in active:
            raise ValueError('复核来源形成循环，不能核准')
        if identity in finished:
            return
        if len(active) >= 64:
            raise ValueError('复核来源衔接层数过多，请缩小交付范围')
        visits += 1
        if visits > 10000:
            raise ValueError('复核来源过多，请缩小交付范围')
        active.add(identity)
        value = supplied if supplied is not None else verify(ref.model_dump(), ctx)
        if ref.workspace_id != ctx.workspace_id:
            raise ValueError('复核来源不属于当前项目')
        if ref.application == 'multimodal' and ref.kind == 'result':
            fixed_snapshot(ref.parent_id, ref.object_id, ref.sha256)
        elif ref.kind == 'evidence':
            evidence(value)
        elif ref.kind == 'result' and ref.application == 'platform' and ref.coverage.get('kind') == 'audit_draft':
            for identifier in value.get('evidence_ids', []):
                captured = ctx.services.audit_service.drafts.evidence(identifier, ctx)
                if value['evidence_hashes'].get(identifier) != captured['snapshot_hash']:
                    raise ValueError('发现草稿的固定证据哈希变化')
                evidence(captured)
        elif ref.kind == 'artifact':
            if value.get('kind') == 'manual_analysis':
                if value.get('snapshot_id'):
                    fixed_snapshot(value.get('platform_job_id'), value['snapshot_id'], value.get('snapshot_hash'))
                    if value.get('draft') or value.get('review_state') == 'unreviewed':
                        obligations.add((value['platform_job_id'], value['snapshot_id']))
                else:
                    legacy_manual_bundle(ref, value)
            audit_bundle(ref, value)
        if ref.kind == 'asset':
            derived(ref.object_id)
        else:
            for identifier in reference_asset_ids(ref.model_dump(), value):
                source_asset(identifier)
        for source in ref.sources:
            walk(source)
        active.remove(identity)
        finished.add(identity)

    walk(reference, verified)
    from workbench_platform.application_services.operations.multimodal import delivery_checks
    checks, identities, draft = [], [], bool(unknown or obligations)
    for value in snapshots.values():
        confirmed = (value['analysis'].get('review') or {}).get('assessment') == 'confirmed' and value['review_revision'] > 0
        draft |= not confirmed
        checks.extend(delivery_checks(value, require_review=require_review))
        identities.append({field: value[field] for field in
            ('job_id', 'snapshot_id', 'snapshot_hash', 'generation', 'revision', 'review_revision', 'kind')})
    for identity in unknown.values():
        checks.append({'name': 'human_review', 'status': 'failed' if require_review else 'measured',
            'message': '旧成果没有可核实的固定复核快照，保留为草稿；请核对原版本后重新归集', 'evidence': identity})
    if require_review and obligations:
        # An explicitly generated unreviewed derivative remains draft even if
        # its source happened to have a saved review. Formal conversion creates
        # a separate result with its own version and confirmed provenance.
        checks.append({'name': 'human_review', 'status': 'failed',
            'message': '交付包含明确标记为未复核的草稿派生字段，请沿核准通路重新生成正式结果',
            'evidence': {'snapshot_ids': sorted({snapshot for _, snapshot in obligations})}})
    return {'checks': checks, 'draft': draft, 'snapshots': identities}

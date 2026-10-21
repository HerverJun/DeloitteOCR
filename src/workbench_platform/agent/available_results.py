"""Collect fixed tool output independently of the model and final delivery."""
from dataclasses import replace
import json
from urllib.parse import quote

from .registry import invoke
from .store import digest, RunSuperseded


def downloadable(bundle, workspace_id):
    base = f'/api/platform/v1/workspaces/{workspace_id}/artifacts/{bundle["bundle_id"]}/files/'
    return {**bundle, 'files': [{**file, 'download_url': base + quote(file['name'])} for file in bundle.get('files', [])]}


async def publish_reference(ctx, ref):
    from workbench_platform.application_services.references import verify, reference_asset_ids
    from workbench_platform.artifacts.native_exports import publish
    if ref['kind'] == 'result' and ref.get('application') == 'ocr':
        from workbench_platform.artifacts.text_outputs import publish_text_result
        return await invoke(publish_text_result, ctx, ref)
    value = await invoke(verify, ref, ctx)
    if ref['kind'] == 'artifact':
        return {'artifact_ref': ref, 'delivery': downloadable(value, ctx.workspace_id)}
    # The exact verified snapshot is useful even when later comparison or
    # model prose has failed. Do not re-query a newer native result to export.
    if ref.get('application') not in {'data', 'classification'} or ref['kind'] not in {'result', 'table'}:
        return {'result_ref': ref}
    from .store import canonical
    name = 'predictions.json' if ref.get('application') == 'classification' else 'table-result.json'
    source_ids = reference_asset_ids(ref, value)
    from packages.contracts.models import SourceLocator
    inputs, locators = [], []
    for identifier in source_ids:
        asset = ctx.services.asset_library[0].get(ctx.workspace_id, identifier)
        locator = SourceLocator(schema_version='1.0', kind='file', asset_id=identifier,
            asset_revision=asset.revision, blob_hash=asset.blob_hash, precision='exact').model_dump(mode='json')
        locators.append(locator)
        inputs.append({'asset_id': identifier, 'revision': asset.revision, 'blob_hash': asset.blob_hash,
                       'source_locator': locator})
    output_ctx = replace(ctx, operation_id='o' + digest(['available-fixed-result-v1', ref])[:48])
    bundle = await invoke(publish, output_ctx, '分类预测结果' if name == 'predictions.json' else '数据表格结果',
        {name: (canonical({'reference': ref, 'snapshot': value}).encode('utf-8'), 'application/json')},
        inputs, locators, 'available-fixed-result-v1')
    return {'artifact_ref': {'kind': 'artifact', 'workspace_id': ctx.workspace_id,
                            'object_id': bundle['bundle_id'], 'revision': 1},
            'delivery': downloadable(bundle, ctx.workspace_id)}


async def collect(runtime, run_id):
    run = runtime.store.run(run_id)
    if run['status'] == 'cancelled':
        return
    state = {'workspace_id': run['workspace_id'], 'session_id': run['session_id'],
             'run_id': run_id, 'generation': run['generation']}
    delivered = set()
    for event in runtime.store.rows("SELECT payload FROM events WHERE run_id=? AND kind='delivery'", (run_id,)):
        final = json.loads(event['payload']).get('delivery') or {}
        delivered.update(digest(ref) for ref in [*final.get('result_refs', []), *final.get('artifact_refs', [])])
    for record in runtime.store.rows('SELECT * FROM operations WHERE run_id=? AND result IS NOT NULL ORDER BY created', (run_id,)):
        runtime.store.assert_current(run_id, run['generation'])
        ctx = replace(runtime.context(state), operation_id=record['id'])
        result = json.loads(record['result'])
        if result.get('status') == 'pending' and result.get('job_refs'):
            try:
                result = await observe_accepted(runtime, ctx, record, result)
            except RunSuperseded:
                raise
            except Exception as error:
                runtime.store.event(run['session_id'], run_id,
                    record['id'] + ':result-observe-error:' + digest(str(error))[:16], 'result_collection_error',
                    {'operation_id': record['id'], 'message': str(error)[:500], 'generation': run['generation']})
        refs = {digest(ref): ref for ref in [*result.get('result_refs', []), *result.get('artifact_refs', [])]
                if ref.get('kind') in {'result', 'table', 'artifact'}}
        for key, ref in refs.items():
            if key in delivered:
                continue
            identity = run_id + ':result-available:' + key
            if runtime.store.rows('SELECT 1 FROM events WHERE identity=?', (identity,)):
                continue
            try:
                output = await publish_reference(ctx, ref)
                ctx.current()
                available = {'status': 'success', 'summary': '已完成的工具成果可读取和下载；其余目标继续保留',
                    'result_refs': [ref] if ref['kind'] != 'artifact' else [],
                    'artifact_refs': [output['artifact_ref']] if output.get('artifact_ref') else [],
                    'data': {'delivery': [output['delivery']] if output.get('delivery') else [],
                        'readable_text': [output['readable_text']] if output.get('readable_text') else [],
                        'draft': ref.get('application') in {'ocr', 'classification'},
                        'review_state': 'unreviewed' if ref.get('application') in {'ocr', 'classification'} else 'raw_output',
                        'finalized': False, 'analysis_pending': True}}
                from workbench_platform.resources.reader_features import require_features
                require_features(runtime.store.root.parent, 'independent-results-v1')
                runtime.store.public_event(run_id, run['generation'], identity, 'result_available',
                    {'operation_id': record['id'], 'goal_complete': False, 'result': available})
            except RunSuperseded:
                raise
            except Exception as error:
                # One unavailable item cannot hide the other successful ones.
                runtime.store.event(run['session_id'], run_id, identity + ':unavailable:' + digest(str(error))[:16],
                    'result_collection_error', {'operation_id': record['id'], 'reference': ref,
                        'message': str(error)[:500], 'generation': run['generation']})


async def observe_accepted(runtime, ctx, record, result):
    from workbench_platform.application_services.job_lifecycle import adapter
    outputs = []
    for reference in result['job_refs']:
        lifecycle = adapter(reference)
        # Observation never resumes/cancels/submits. In particular do not let
        # settle() revive a native interrupted data operation.
        observed = await invoke(lifecycle.read, reference, ctx)
        native_status = observed.get('status') or observed.get('platform_job', {}).get('status')
        if native_status not in {'succeeded', 'partial', 'failed', 'cancelled'}:
            break
        output = await invoke(lifecycle.settle, reference, ctx)
        if output is None:
            break
        outputs.append(output)
    if len(outputs) == len(result['job_refs']) == 1:
        ctx.current()
        result = {**outputs[0], 'job_refs': result['job_refs']}
        runtime.store.finish_operation(record['id'], result)
    return result

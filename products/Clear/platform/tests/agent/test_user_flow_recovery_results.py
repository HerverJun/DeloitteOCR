"""J04/J05: one durable goal survives controller faults and delivers fixed output."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, Conflict, canonical, digest
from workbench_platform.agent.providers import ProviderFault


def runtime_for(tmp_path):
    runtime = object.__new__(AgentRuntime)
    runtime.store = AgentStore(tmp_path / 'agent')
    runtime.services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
    runtime.running, runtime.reschedules, runtime.maintenance = {}, {}, {}
    runtime.closing, runtime.agent_enabled, runtime.model_provider = False, True, object()
    runtime.tasks = SimpleNamespace(get=lambda _: {'manual': False, 'scope_revision': 1})
    def save_state(value):
        snapshot = dict(value)
        state.clear()
        state.update(snapshot)
    runtime.graph = SimpleNamespace(load=lambda _: dict(state), save=Mock(side_effect=save_state),
        ainvoke=AsyncMock(side_effect=ProviderFault('provider_unavailable', '503 synthetic')))
    runtime.registry = SimpleNamespace(tools={})
    runtime.schedule = Mock()
    session = runtime.store.create_session('workspace', 'User goal', ['image'], 'session')
    run, _ = runtime.store.start_run(session['id'], 'Read then compare amounts', 'original-request', 1)
    state = {'run_id': run['id'], 'generation': 1, 'session_id': session['id'],
             'workspace_id': 'workspace', 'step': 0, 'messages': []}
    return runtime, run, state


@pytest.mark.parametrize('code', ['provider_unavailable', 'authentication', 'harness_exited',
                                  'truncated_response', 'rate_limited'])
def test_temporary_fault_keeps_same_goal_resumable(tmp_path, code):
    runtime, run, state = runtime_for(tmp_path)
    runtime.graph.ainvoke.side_effect = ProviderFault(code, 'Synthetic controller interruption')
    asyncio.run(runtime.drive(run['id'], None))
    saved = runtime.store.run(run['id'])
    assert saved['status'] == 'paused'
    assert saved['request_id'] == 'original-request'
    assert '继续' in saved['error']
    asyncio.run(runtime.reconcile(run['id']))
    runtime.schedule.assert_called_once_with(run['id'], None)
    assert runtime.store.run(run['id'])['status'] == 'running'


def test_explicit_continue_abandons_unknown_model_attempt_without_reissuing_it(tmp_path):
    runtime, run, state = runtime_for(tmp_path)
    request, _ = runtime.store.begin_model(run['id'], 1, 0, digest('unknown model generation'))
    state['pending_model_request'] = request['id']
    runtime.store.status(run['id'], 1, 'needs_verification', 'Unknown model response')
    asyncio.run(runtime.reconcile(run['id']))
    row = runtime.store.rows('SELECT * FROM model_requests')[0]
    assert row['id'] == request['id'] and row['state'] == 'abandoned'
    assert runtime.store.run(run['id'])['request_id'] == 'original-request'
    runtime.schedule.assert_called_once_with(run['id'], None)
    assert runtime.store.rows('SELECT * FROM operations') == []


def test_unknown_business_write_blocks_continue_and_preserves_model_record(tmp_path):
    runtime, run, state = runtime_for(tmp_path)
    request, _ = runtime.store.begin_model(run['id'], 1, 0, digest('unknown model generation'))
    runtime.registry.tools['unsafe_write'] = SimpleNamespace(effect='versioned', idempotent=False,
                                                           lookup_operation=None)
    runtime.store.begin_operation(run['id'], 1,
        {'call_id': 'accepted-unknown', 'tool': 'unsafe_write', 'arguments': {}})
    runtime.store.status(run['id'], 1, 'needs_verification', 'Unknown native receipt')
    with pytest.raises(Conflict, match='回执'):
        asyncio.run(runtime.reconcile(run['id']))
    runtime.schedule.assert_not_called()
    assert runtime.store.rows('SELECT state FROM model_requests')[0]['state'] == 'sent'
    assert len(runtime.store.rows('SELECT * FROM operations')) == 1


def test_cancelled_goal_is_never_resumed(tmp_path):
    runtime, run, state = runtime_for(tmp_path)
    with runtime.store.transaction() as db:
        runtime.store.cancel_in_transaction(db, run_id=run['id'])
    with pytest.raises(Conflict):
        asyncio.run(runtime.reconcile(run['id']))
    runtime.schedule.assert_not_called()


@pytest.mark.parametrize('fault_code', ['authentication', 'provider_unavailable'])
def test_repaired_connection_binds_tested_revision_to_original_goal(tmp_path, fault_code):
    runtime, run, state = runtime_for(tmp_path)
    runtime.model_provider = None
    runtime.connection = SimpleNamespace(view=lambda: {'available': True, 'revision': 2},
        resolve=lambda _: ({'revision': 2, 'model': 'repaired-original-model'}, 'synthetic-key'))
    runtime.finish_error(run['id'], 1, 'paused', fault_code, 'Repair tested connection then continue')
    asyncio.run(runtime.reconcile(run['id']))
    resumed = runtime.store.run(run['id'])
    assert resumed['id'] == run['id'] and resumed['request_id'] == 'original-request'
    assert resumed['connection_revision'] == 2
    assert runtime.graph.save.call_args.args[0]['frozen_config']['revision'] == 2
    assert len(runtime.store.rows("SELECT * FROM events WHERE kind='connection_repaired'")) == 1
    runtime.schedule.assert_called_once_with(run['id'], None)


def test_completed_text_is_collected_when_analysis_failed_without_model_or_native_submit(tmp_path):
    runtime, run, state = runtime_for(tmp_path)
    original = {'kind': 'result', 'workspace_id': 'workspace', 'application': 'ocr',
        'parent_id': 'original-handoff', 'object_id': 'original-result', 'revision': 2, 'sha256': 'a' * 64}
    operation, _ = runtime.store.begin_operation(run['id'], 1,
        {'call_id': 'extract-original', 'tool': 'ocr_run', 'arguments': {}})
    runtime.store.finish_operation(operation['id'], {'status': 'success', 'summary': 'Text complete',
        'result_refs': [original], 'artifact_refs': [], 'job_refs': []})
    runtime.store.status(run['id'], 1, 'failed', 'Comparison interrupted')
    output = {'artifact_ref': {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': 'bundle', 'revision': 1},
        'readable_text': {'text': '0001 金额10.00', 'reference': original},
        'delivery': {'bundle_id': 'bundle', 'files': [{'name': 'ocr-text.txt', 'download_url': '/download/original'}]}}
    with patch('workbench_platform.artifacts.text_outputs.publish_text_result', return_value=output) as publish:
        asyncio.run(runtime.collect_available_results(run['id']))
        asyncio.run(runtime.collect_available_results(run['id']))
    assert publish.call_count == 1
    assert publish.call_args.args[1] == original
    assert runtime.store.run(run['id'])['status'] == 'failed'
    events = runtime.store.rows("SELECT payload FROM events WHERE kind='result_available'")
    assert len(events) == 1
    payload = json.loads(events[0]['payload'])
    assert payload['goal_complete'] is False
    assert payload['result']['data']['delivery'][0]['files'][0]['name'] == 'ocr-text.txt'
    runtime.graph.ainvoke.assert_not_awaited()


def test_one_unreadable_item_does_not_hide_two_completed_results_and_only_missing_item_retries(tmp_path):
    runtime, run, state = runtime_for(tmp_path)
    references = [{'kind': 'result', 'application': 'ocr', 'workspace_id': 'workspace',
                   'object_id': name, 'parent_id': 'original-handoff', 'revision': 1} for name in ['unavailable', 'good-one', 'good-two']]
    operation, _ = runtime.store.begin_operation(run['id'], 1, {'call_id': 'three-results', 'tool': 'ocr_run', 'arguments': {}})
    runtime.store.finish_operation(operation['id'], {'status': 'error', 'summary': 'Two complete; one unavailable', 'result_refs': references})
    runtime.store.status(run['id'], 1, 'failed', 'Comparison remains incomplete')
    ready, attempted = False, []
    def publish(ctx, ref):
        attempted.append(ref['object_id'])
        if ref['object_id'] == 'unavailable' and not ready:
            raise ValueError('Fixed native result temporarily unreadable')
        return {'artifact_ref': {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': 'bundle-' + ref['object_id'], 'revision': 1},
                'delivery': {'bundle_id': 'bundle-' + ref['object_id'], 'files': []}, 'readable_text': {'text': ref['object_id']}}
    with patch('workbench_platform.artifacts.text_outputs.publish_text_result', side_effect=publish):
        asyncio.run(runtime.collect_available_results(run['id']))
        assert len(runtime.store.rows("SELECT * FROM events WHERE kind='result_available'")) == 2
        ready = True
        asyncio.run(runtime.collect_available_results(run['id']))
    assert len(runtime.store.rows("SELECT * FROM events WHERE kind='result_available'")) == 3
    assert attempted == ['unavailable', 'good-one', 'good-two', 'unavailable']
    assert runtime.store.run(run['id'])['status'] == 'failed'
    assert len(runtime.store.rows('SELECT * FROM operations')) == 1
    runtime.graph.ainvoke.assert_not_awaited()


def test_accepted_job_completed_after_controller_failure_exports_actual_original_txt(tmp_path):
    from packages.contracts.models import SourceLocator, AssetSetItem
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.artifacts.center import ArtifactCenter
    from workbench_platform.application_services.job_lifecycle import Lifecycle
    runtime, run, state = runtime_for(tmp_path)
    fixtures, blobs = tmp_path / 'fixtures', tmp_path / 'blobs'
    fixtures.mkdir(); blobs.mkdir()
    (fixtures / 'image.png').write_bytes(b'original-fixed-image')
    assets = AssetRegistry(tmp_path / 'assets.sqlite3', blobs, {'fixtures': fixtures})
    sets = AssetSetStore(assets)
    asset = assets.register('workspace', 'fixtures', 'image.png', media_type='image/png', storage_mode='managed')
    member = AssetSetItem(schema_version='1.0', item_id='original', asset_id=asset.asset_id,
        asset_revision=1, blob_hash=asset.blob_hash, source_locator=SourceLocator(schema_version='1.0', kind='file',
            asset_id=asset.asset_id, asset_revision=1, blob_hash=asset.blob_hash, precision='exact'))
    runtime.services.asset_library = (assets, sets, None)
    center = runtime.services.artifact_center = ArtifactCenter(tmp_path / 'artifacts', assets, sets, None)
    job = {'kind': 'job', 'workspace_id': 'workspace', 'application': 'ocr', 'object_id': 'accepted-job', 'parent_id': 'handoff', 'revision': 1}
    ref = {'kind': 'result', 'workspace_id': 'workspace', 'application': 'ocr', 'object_id': 'original',
           'parent_id': 'handoff', 'revision': 2, 'sha256': 'b' * 64}
    operation, _ = runtime.store.begin_operation(run['id'], 1,
        {'call_id': 'accepted-original', 'tool': 'ocr_run', 'arguments': {}})
    runtime.store.finish_operation(operation['id'], {'status': 'pending', 'summary': 'Accepted once', 'job_refs': [job]})
    runtime.store.status(run['id'], 1, 'failed', 'Analysis interrupted after submission')
    original = '编号 00001；金额 10.00\r\n' * 800
    fixed = {'snapshot': {'available': True, 'edited': {'text': original}},
             'source_asset': member.model_dump(mode='json'), 'locator': {'page_number': 5}}
    read = Mock(return_value={'status': 'succeeded'})
    settle = Mock(return_value={'status': 'success', 'summary': 'Finished original job', 'result_refs': [ref]})
    with patch('workbench_platform.application_services.job_lifecycle.adapter', return_value=Lifecycle(read, settle, None, None)), \
         patch('workbench_platform.application_services.references.verify', return_value=fixed):
        asyncio.run(runtime.collect_available_results(run['id']))
        asyncio.run(runtime.collect_available_results(run['id']))
    assert settle.call_count == 1 and read.call_count == 1
    result = json.loads(runtime.store.rows("SELECT payload FROM events WHERE kind='result_available'")[0]['payload'])['result']
    bundle = result['data']['delivery'][0]
    artifact = center.file('workspace', bundle['bundle_id'], 'ocr-text.txt')
    with center.store.stage_download('workspace', artifact.artifact_id,
        expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
        assert path.read_bytes() == original.encode('utf-8')
    assert result['result_refs'] == [ref] and result['data']['finalized'] is False
    assert runtime.store.run(run['id'])['status'] == 'failed'
    assert len(runtime.store.rows('SELECT * FROM operations')) == 1
    runtime.graph.ainvoke.assert_not_awaited()


@pytest.mark.parametrize('application', ['data', 'classification'])
def test_fixed_table_and_prediction_exports_have_downloadable_bytes_and_typed_source_locations(tmp_path, application):
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.artifacts.center import ArtifactCenter
    runtime, run, state = runtime_for(tmp_path)
    fixtures, blobs = tmp_path / 'fixtures', tmp_path / 'blobs'
    fixtures.mkdir(); blobs.mkdir()
    (fixtures / 'source.bin').write_bytes(b'fixed-original-input')
    assets = AssetRegistry(tmp_path / 'assets.sqlite3', blobs, {'fixtures': fixtures})
    sets = AssetSetStore(assets)
    asset = assets.register('workspace', 'fixtures', 'source.bin', media_type='application/octet-stream', storage_mode='managed')
    runtime.services.asset_library = (assets, sets, None)
    center = runtime.services.artifact_center = ArtifactCenter(tmp_path / 'artifacts', assets, sets, None)
    ref = {'kind': 'table' if application == 'data' else 'result', 'application': application,
           'workspace_id': 'workspace', 'parent_id': 'original-native-parent', 'object_id': 'fixed-result', 'revision': 3}
    fixed = {'source_assets': [asset.asset_id], 'items': [{'asset_id': asset.asset_id,
             'original_id': '00000000000001', 'amount': '10.00', 'prediction_label_id': 'original-label'}]}
    operation, _ = runtime.store.begin_operation(run['id'], 1,
        {'call_id': 'fixed-' + application, 'tool': 'fixed_' + application, 'arguments': {}})
    runtime.store.finish_operation(operation['id'], {'status': 'success', 'summary': 'Fixed original', 'result_refs': [ref]})
    runtime.store.status(run['id'], 1, 'failed', 'Later analysis interrupted')
    with patch('workbench_platform.application_services.references.verify', return_value=fixed):
        asyncio.run(runtime.collect_available_results(run['id']))
    result = json.loads(runtime.store.rows("SELECT payload FROM events WHERE kind='result_available'")[0]['payload'])['result']
    bundle = result['data']['delivery'][0]
    file_name = 'predictions.json' if application == 'classification' else 'table-result.json'
    artifact = center.file('workspace', bundle['bundle_id'], file_name)
    with center.store.stage_download('workspace', artifact.artifact_id,
        expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
        assert json.loads(path.read_bytes()) == {'reference': ref, 'snapshot': fixed}
    assert artifact.source_refs[0].asset_id == asset.asset_id
    assert runtime.store.run(run['id'])['status'] == 'failed'


@pytest.mark.parametrize('fault_code', ['provider_unavailable', 'response_unknown'])
def test_actual_harness_continues_original_goal_after_rejected_or_unknown_generation(tmp_path, fault_code):
    from workbench_platform.store import WorkspaceStore
    async def scenario():
        workspaces = WorkspaceStore(tmp_path / 'workspaces.sqlite3')
        workspaces.create_workspace('Synthetic recovery', workspace_id='workspace')
        services = SimpleNamespace(workspace_store=workspaces, asset_library=(SimpleNamespace(), None, None))
        requests = []
        async def provider(state, schemas):
            requests.append(state['run_id'])
            if len(requests) == 1:
                raise ProviderFault(fault_code, 'Injected model interruption')
            return {'id': 'resumed-response', 'role': 'assistant', 'content': '原目标已继续',
                'calls': [],
                'protocol': 'openai_chat_completions', 'private': {}, 'blocks': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        runtime = AgentRuntime(tmp_path / 'actual-harness', services, model_provider=provider)
        services.agent = runtime
        await runtime.start()
        errors = []
        original_invoke = runtime.graph.ainvoke
        async def traced_invoke(*args, **kwargs):
            try:
                return await original_invoke(*args, **kwargs)
            except Exception:
                import traceback
                errors.append(traceback.format_exc())
                raise
        runtime.graph.ainvoke = traced_invoke
        try:
            session = runtime.store.create_session('workspace', 'Original goal', [], 'actual-session')
            run = await runtime.submit(session['id'], '请说明下一步如何核对金额', 'actual-original-request')
            async def wait_for(statuses):
                for _ in range(600):
                    saved = runtime.store.run(run['id'])
                    if saved['status'] in statuses:
                        return saved
                    await asyncio.sleep(.05)
                raise AssertionError(runtime.store.events(session['id'], limit=10000))
            assert (await wait_for({'answered', 'failed', 'paused'}))['status'] == 'answered', errors
            assert requests == [run['id'], run['id']]
            assert len(runtime.store.rows('SELECT id FROM runs')) == 1
            assert runtime.store.run(run['id'])['request_id'] == 'actual-original-request'
            ledger = runtime.store.rows('SELECT state FROM model_requests ORDER BY step')
            assert [row['state'] for row in ledger] == [
                'abandoned' if fault_code == 'response_unknown' else 'rejected', 'received']
        finally:
            await runtime.close()
    asyncio.run(scenario())


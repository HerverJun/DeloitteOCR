"""Restore fixed results across the direct driver's three commit windows."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, digest
from workbench_platform.application_services import job_lifecycle
from workbench_platform.application_services.image_text import ImageTextContext, drive


@pytest.mark.parametrize('window', ['operation_committed', 'driver_committed', 'delivery_committed'])
def test_resume_keeps_fixed_version_and_original_operation_without_native_replay(tmp_path, window):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspace', 'Fixed result', ['image'], 'session')
    arguments = {'link_id': 'link', 'asset_set_id': 'set', 'asset_set_revision': 1, 'engines': ['ppocr']}
    ref = AgentRef(kind='job', application='ocr', workspace_id='workspace', object_id='job',
                   parent_id='handoff', revision=1).model_dump(mode='json')
    original = AgentRef(kind='result', application='ocr', workspace_id='workspace', object_id='result',
                        parent_id='handoff', revision=1, sha256='b' * 64).model_dump(mode='json')
    pending = AgentToolResult(status='pending', summary='Accepted', job_refs=[AgentRef.model_validate(ref)]).model_dump(mode='json')
    fixed = AgentToolResult(status='success', summary='Fixed revision', job_refs=[AgentRef.model_validate(ref)],
        result_refs=[AgentRef.model_validate(original)], coverage={'succeeded': 1}).model_dump(mode='json')
    saved = {'version': 1, 'request': {'engines': [], 'link_id': None, 'source_text': None},
        'inputs': [{'asset_id': 'image', 'revision': 1, 'blob_hash': 'a' * 64}], 'scope_revision': 1,
        'stage': 'waiting', 'timings': {}, 'receipts': {'prepared': {'link_id': 'link', 'engines': [{'id': 'ppocr'}]},
        'input_set': {'asset_set_id': 'set', 'revision': 1}, 'submitted': pending, 'submit_arguments': arguments}}
    run, _ = store.start_run(session['id'], 'Text', 'run', 0, driver='image-text-v1', driver_state=saved)
    operation, _ = store.begin_operation(run['id'], 1,
        {'call_id': 'direct-ocr_run-' + digest(arguments)[:24], 'tool': 'ocr_run', 'arguments': arguments})
    store.finish_operation(operation['id'], fixed)
    saved['receipts']['submitted_operation_id'] = operation['id']
    if window != 'operation_committed':
        saved['receipts']['result'] = fixed
        saved['stage'] = 'delivery'
    output = {'artifact_ref': {'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': 'textBundle', 'revision': 1},
              'readable_text': {'text': 'Original saved text', 'reference': original}, 'delivery': {'files': []}}
    body = {'verified': True, 'finalize': True, 'summary': '图片文字已提取，可复制原文或下载 TXT。',
        'result_refs': fixed['result_refs'], 'artifact_refs': [output['artifact_ref']],
        'readable_text': [output['readable_text']], 'deliveries': [output['delivery']],
        'coverage': fixed['coverage'], 'driver': 'image-text-v1', 'rule_version': 'input-requirements-v1'}
    if window == 'delivery_committed':
        store.public_event(run['id'], 1, run['id'] + ':direct-delivery', 'delivery', body)
        saved['stage'] = 'complete'
    store.save_driver_state(run['id'], 1, saved)
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()),
        asset_library=(SimpleNamespace(get=lambda *_: SimpleNamespace(revision=1, blob_hash='a' * 64)), None, None))
    runtime = SimpleNamespace(store=AgentStore(tmp_path / 'agent'), services=services,
        tasks=SimpleNamespace(get=lambda _: {'scope_revision': 1, 'manual': False}),
        settle=AsyncMock(side_effect=AssertionError('Committed result must not be read again as revision 2')),
        activity=Mock(), finish_error=Mock(side_effect=AssertionError('Fixed success must remain successful')))
    services.agent = runtime
    with patch('workbench_platform.artifacts.text_outputs.publish_text_result', return_value=output) as publish, \
         patch('workbench_platform.application_services.job_observations.publish_settled_progress') as progress:
        asyncio.run(drive(runtime, run['id']))
    runtime.settle.assert_not_awaited()
    publish.assert_called_once()
    assert publish.call_args.args[1] == original
    assert progress.call_args.args[0].operation_id == operation['id']
    assert runtime.store.run(run['id'])['status'] == 'succeeded'
    assert json.loads(runtime.store.rows('SELECT result FROM operations WHERE id=?', (operation['id'],))[0]['result']) == fixed
    assert len(runtime.store.rows("SELECT seq FROM events WHERE kind='delivery'")) == 1


def test_runtime_settle_preserves_direct_scope_fence_across_native_read():
    task = {'scope_revision': 1, 'manual': False}
    store = SimpleNamespace(assert_current=lambda *_: None)
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()),
        asset_library=(SimpleNamespace(get=lambda *_: SimpleNamespace(revision=1, blob_hash='a' * 64)), None, None),
        agent=SimpleNamespace(tasks=SimpleNamespace(get=lambda _: task)))
    context = ImageTextContext('workspace', 'session', 'run', 1, 0, 'operation', ('image',), services, store,
        expected_scope=1, input_versions=(('image', 1, 'a' * 64),))
    runtime = object.__new__(AgentRuntime)
    runtime.current = lambda _: None
    runtime.context = Mock(side_effect=AssertionError('Direct context must not be replaced'))
    def read(*_):
        task['pending_scope'] = {'selected_assets': []}
        return None
    lifecycle = job_lifecycle.Lifecycle(None, read, None, None)
    from workbench_platform.application_services.identity import Conflict
    with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), pytest.raises(Conflict):
        asyncio.run(runtime.settle({'job_refs': [{'application': 'ocr'}]}, {}, context=context))
    runtime.context.assert_not_called()


def pending_direct_action(tmp_path, application='classification', *, known=True):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspace', 'Accepted direct action', ['image'], 'session')
    run, _ = store.start_run(session['id'], 'Predict', 'request', 0,
        driver='classification-predict-v1' if application == 'classification' else 'data-action-v1', driver_state={})
    arguments = {'native_request_id': 'original-request'}
    reference = AgentRef(kind='job', workspace_id='workspace', application=application,
        object_id='original-native-job', parent_id='original-handoff', revision=1).model_dump(mode='json')
    pending = AgentToolResult(status='pending', summary='Accepted once', job_refs=[reference]).model_dump(mode='json')
    fixed = AgentToolResult(status='success', summary='Original accepted result', coverage={'succeeded': 1}).model_dump(mode='json')
    operation, _ = store.begin_operation(run['id'], 1, {'call_id': 'direct-fixed_action-' + digest(arguments)[:24],
        'tool': 'fixed_action', 'arguments': arguments})
    if known:
        store.finish_operation(operation['id'], pending)
    task = {'scope_revision': 1, 'manual': False}
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()),
        asset_library=(SimpleNamespace(get=lambda *_: SimpleNamespace(revision=1, blob_hash='a' * 64)), None, None))
    runtime = object.__new__(AgentRuntime)
    runtime.store, runtime.services = store, services
    runtime.tasks = SimpleNamespace(get=lambda _: task)
    runtime.current = lambda state: store.assert_current(state['run_id'], state['generation'])
    runtime.context = Mock(side_effect=AssertionError('Preserve the specialized direct context'))
    services.agent = runtime
    ctx = ImageTextContext('workspace', session['id'], run['id'], 1, 0, '', ('image',), services, store,
        expected_scope=1, input_versions=(('image', 1, 'a' * 64),))
    return runtime, ctx, task, arguments, reference, pending, fixed, operation


def test_classification_unavailable_observation_restores_service_for_original_receipt_only(tmp_path):
    from workbench_platform.application_services import direct_actions
    runtime, ctx, task, arguments, reference, pending, fixed, original = pending_direct_action(tmp_path)
    state = {'ready': False, 'starts': 0}
    def prepare(ref, current):
        assert ref == reference and isinstance(current, ImageTextContext)
        if not state['ready']:
            state.update(ready=True, starts=state['starts'] + 1)
    def progress(ref, current):
        assert ref == reference
        return {'status': 'succeeded' if state['ready'] else 'unknown',
                'observation': 'native' if state['ready'] else 'unavailable'}
    lifecycle = job_lifecycle.Lifecycle(None, Mock(return_value=fixed), None, None, prepare)
    submit = Mock(side_effect=AssertionError('Never resubmit an accepted inference'))
    def final_progress(current, references):
        assert references == [reference]
        assert current.store.rows('SELECT state FROM operations WHERE id=?', (original['id'],))[0]['state'] == 'finished'
    with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), \
         patch.object(direct_actions, 'progress_for', side_effect=progress), \
         patch.object(direct_actions, 'publish_settled_progress', side_effect=final_progress), \
         patch.object(direct_actions.asyncio, 'sleep', new=AsyncMock()):
        result = asyncio.run(direct_actions.operation(runtime, ctx, 'fixed_action', arguments, submit))
    assert state['starts'] == 1 and result['job_refs'] == [reference]
    assert lifecycle.settle.call_args.args[0] == reference
    submit.assert_not_called()
    runtime.context.assert_not_called()
    rows = runtime.store.rows('SELECT id,arguments,state FROM operations')
    assert len(rows) == 1 and rows[0]['id'] == original['id'] and rows[0]['state'] == 'finished'
    assert json.loads(rows[0]['arguments']) == arguments


def test_data_unavailable_observation_keeps_backoff_without_native_service_dependency(tmp_path):
    from workbench_platform.application_services import direct_actions
    runtime, ctx, task, arguments, reference, pending, fixed, original = pending_direct_action(tmp_path, 'data')
    lifecycle = job_lifecycle.Lifecycle(None, Mock(return_value=fixed), None, None, None)
    submit = Mock(side_effect=AssertionError('Never repeat the accepted data operator'))
    observations = [{'status': 'unknown', 'observation': 'unavailable'}, {'status': 'succeeded', 'observation': 'native'}]
    with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), \
         patch.object(direct_actions, 'progress_for', side_effect=observations), \
         patch.object(direct_actions, 'publish_settled_progress'), \
         patch.object(direct_actions.asyncio, 'sleep', new=AsyncMock()) as sleep:
        result = asyncio.run(direct_actions.operation(runtime, ctx, 'fixed_action', arguments, submit))
    assert result['job_refs'] == [reference]
    assert lifecycle.settle.call_count == 1 and sleep.await_count == 1
    submit.assert_not_called()


@pytest.mark.parametrize('change', ['pause', 'scope'])
def test_direct_service_prepare_cannot_cross_pause_or_scope_change(tmp_path, change):
    from workbench_platform.application_services import direct_actions
    from workbench_platform.application_services.identity import Conflict, RunSuperseded
    runtime, ctx, task, arguments, reference, pending, fixed, original = pending_direct_action(tmp_path)
    def prepare(ref, current):
        if change == 'pause':
            # The real pause API revokes the executing generation.
            with current.store.transaction() as db:
                db.execute("UPDATE runs SET status='paused',generation=generation+1 WHERE id=?", (current.run_id,))
        else:
            task['pending_scope'] = {'selected_assets': []}
    lifecycle = job_lifecycle.Lifecycle(None, Mock(side_effect=AssertionError('No late settlement')), None, None, prepare)
    submit = Mock(side_effect=AssertionError('No new inference'))
    with patch.object(job_lifecycle, 'adapter', return_value=lifecycle), \
         patch.object(direct_actions, 'progress_for', return_value={'status': 'unknown', 'observation': 'unavailable'}), \
         pytest.raises((Conflict, RunSuperseded)):
        asyncio.run(direct_actions.operation(runtime, ctx, 'fixed_action', arguments, submit))
    assert runtime.store.rows('SELECT state FROM operations WHERE id=?', (original['id'],))[0]['state'] == 'pending'
    lifecycle.settle.assert_not_called()
    submit.assert_not_called()


def test_unknown_direct_submission_never_uses_service_recovery_to_resubmit(tmp_path):
    from workbench_platform.application_services import direct_actions
    from workbench_platform.application_services.image_text import ReceiptUnknown
    runtime, ctx, task, arguments, reference, pending, fixed, original = pending_direct_action(tmp_path, known=False)
    submit = Mock(side_effect=AssertionError('Unknown accepted receipt cannot be repeated'))
    with patch.object(job_lifecycle, 'prepare_observation') as prepare, pytest.raises(ReceiptUnknown):
        asyncio.run(direct_actions.operation(runtime, ctx, 'fixed_action', arguments, submit))
    prepare.assert_not_called()
    submit.assert_not_called()
    assert runtime.store.rows('SELECT state FROM operations WHERE id=?', (original['id'],))[0]['state'] == 'intent'

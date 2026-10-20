"""Only known engine failures may create a fixed failed-input fallback."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from packages.contracts.agent import AgentRef, AgentToolResult
from workbench_platform.agent.store import AgentStore, digest
from workbench_platform.application_services import image_text, requirement_coverage


def prepare(tmp_path, *, required=(), code='engine_load_timeout'):
    store = AgentStore(tmp_path / 'agent')
    inputs = [{'asset_id': 'image' + str(index), 'revision': 1, 'blob_hash': letter * 64}
              for index, letter in enumerate('abc', 1)]
    session = store.create_session('workspace', 'Fallback', [item['asset_id'] for item in inputs], 'session')
    arguments = {'link_id': 'link', 'asset_set_id': 'fixedSet', 'asset_set_revision': 1, 'engines': ['ppocr']}
    job = AgentRef(kind='job', application='ocr', workspace_id='workspace', object_id='primaryJob',
                   parent_id='primaryHandoff', revision=1).model_dump(mode='json')
    retry_job = {**job, 'object_id': 'retryJob', 'parent_id': 'retryHandoff'}
    refs = [AgentRef(kind='result', application='ocr', workspace_id='workspace', object_id='result' + str(index),
                    parent_id='primaryHandoff' if index < 3 else 'retryHandoff', revision=1,
                    sha256=letter * 64).model_dump(mode='json') for index, letter in enumerate('def', 1)]
    pending = AgentToolResult(status='pending', summary='Accepted', job_refs=[AgentRef.model_validate(job)]).model_dump(mode='json')
    original = AgentToolResult(status='success', summary='Two completed, one failed', job_refs=[AgentRef.model_validate(job)],
        result_refs=[AgentRef.model_validate(ref) for ref in refs[:2]], coverage={'succeeded': 2, 'failed': 1}).model_dump(mode='json')
    retried = AgentToolResult(status='success', summary='Failed input repaired', job_refs=[AgentRef.model_validate(retry_job)],
        result_refs=[AgentRef.model_validate(refs[2])], coverage={'succeeded': 1, 'failed': 0}).model_dump(mode='json')
    saved = {'version': 1, 'request': {'engines': list(required), 'link_id': 'link', 'source_text': None},
        'inputs': inputs, 'scope_revision': 1, 'stage': 'waiting', 'timings': {}, 'receipts': {
            'prepared': {'link_id': 'link', 'engines': [{'id': 'ppocr'}, {'id': 'paddlevl'}]},
            'input_set': {'asset_set_id': 'fixedSet', 'revision': 1}, 'submitted': pending,
            'submit_arguments': arguments, 'result': original}}
    run, _ = store.start_run(session['id'], 'Text', 'run', 0, driver='image-text-v1', driver_state=saved)
    operation, _ = store.begin_operation(run['id'], 1,
        {'call_id': 'direct-ocr_run-' + digest(arguments)[:24], 'tool': 'ocr_run', 'arguments': arguments})
    store.finish_operation(operation['id'], original)
    saved['receipts']['submitted_operation_id'] = operation['id']
    store.save_driver_state(run['id'], 1, saved)
    assets = {item['asset_id']: SimpleNamespace(revision=item['revision'], blob_hash=item['blob_hash']) for item in inputs}
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()),
        asset_library=(SimpleNamespace(get=lambda _, identifier: assets[identifier]), None, None))
    runtime = SimpleNamespace(store=store, services=services,
        tasks=SimpleNamespace(get=lambda _: {'scope_revision': 1, 'manual': False}), activity=Mock(),
        settle=AsyncMock(return_value=retried), finish_error=Mock(side_effect=AssertionError('Partial results remain deliverable')))
    services.agent = runtime
    units = [{**item, 'asset_revision': item['revision'], 'operation': 'extract_text', 'scope': {'kind': 'image'},
              'engine': 'ppocr', 'state': 'succeeded' if index < 2 else 'failed',
              'failure_code': None if index < 2 else code, 'result_id': 'result' + str(index + 1) if index < 2 else None,
              'native_task_id': 'task' + str(index + 1)} for index, item in enumerate(inputs)]
    fallback_unit = {**units[2], 'engine': 'paddlevl', 'state': 'succeeded', 'failure_code': None,
                     'result_id': 'result3', 'native_task_id': 'retryTask'}
    class Reader:
        def __init__(self, _):
            pass
        def attempt(self, ref):
            return [fallback_unit] if ref['object_id'] == 'retryJob' else units
        def result(self, parent, result_id):
            return fallback_unit if result_id == 'result3' else units[int(result_id[-1]) - 1]
    return runtime, run, operation, original, retry_job, Reader, inputs


def output(ctx, ref):
    return {'artifact_ref': {'kind': 'artifact', 'workspace_id': ctx.workspace_id,
                            'object_id': 'bundle' + ref['object_id'], 'revision': 1},
            'readable_text': {'reference': ref, 'text': ref['object_id']},
            'delivery': {'bundle_id': 'bundle' + ref['object_id'], 'files': []}}


@pytest.mark.parametrize('required,code,unknown', [(['ppocr'], 'engine_load_timeout', False),
    ([], 'permission_denied', False), ([], 'unclassified_failure', False), ([], 'response_unknown', True)])
def test_explicit_engine_permission_and_unknown_failures_never_submit_fallback(tmp_path, required, code, unknown):
    runtime, run, _, _, _, reader, _ = prepare(tmp_path, required=required, code=code)
    with patch.object(requirement_coverage, 'OcrCoverageReader', reader), \
         patch('workbench_platform.application_services.operations.ocr.run') as submit, \
         patch('workbench_platform.application_services.job_observations.publish_settled_progress'), \
         patch('workbench_platform.artifacts.text_outputs.publish_text_result', side_effect=output):
        if unknown:
            with pytest.raises(image_text.ReceiptUnknown):
                asyncio.run(image_text.drive(runtime, run['id']))
        else:
            asyncio.run(image_text.drive(runtime, run['id']))
            assert runtime.store.run(run['id'])['status'] == 'partial'
    submit.assert_not_called()
    runtime.settle.assert_not_awaited()


def test_three_inputs_retry_only_failed_original_and_recover_acceptance_gap_once(tmp_path):
    runtime, run, original_operation, original, retry_job, reader, inputs = prepare(tmp_path)
    retry_pending = AgentToolResult(status='pending', summary='Fallback accepted',
                                   job_refs=[AgentRef.model_validate(retry_job)]).model_dump(mode='json')
    fixed = Mock(return_value=AgentToolResult(status='success', summary='Fixed failed input',
        data={'asset_set_id': 'retrySet', 'revision': 1}).model_dump(mode='json'))
    save = runtime.store.save_driver_state
    failed_once = []
    def interrupted_checkpoint(run_id, generation, state):
        if state['stage'] == 'fallback_wait' and not failed_once:
            failed_once.append(True)
            raise OSError('Simulated stop after native acceptance, before driver checkpoint')
        return save(run_id, generation, state)
    with patch.object(requirement_coverage, 'OcrCoverageReader', reader), \
         patch.object(image_text, '_fixed_inputs', fixed), \
         patch('workbench_platform.application_services.operations.ocr.run', return_value=retry_pending) as submit, \
         patch('workbench_platform.application_services.job_observations.progress_for',
               return_value={'status': 'succeeded', 'observation': 'native'}), \
         patch('workbench_platform.application_services.job_observations.publish_settled_progress'), \
         patch('workbench_platform.artifacts.text_outputs.publish_text_result', side_effect=output):
        with patch.object(runtime.store, 'save_driver_state', side_effect=interrupted_checkpoint), pytest.raises(OSError):
            asyncio.run(image_text.drive(runtime, run['id']))
        runtime.store = AgentStore(tmp_path / 'agent')
        asyncio.run(image_text.drive(runtime, run['id']))
        state = json.loads(runtime.store.run(run['id'])['driver_state'])
        assert runtime.store.run(run['id'])['status'] == 'succeeded'
        assert state['receipts']['result']['coverage'] == {'total': 3, 'succeeded': 3, 'failed': 0, 'cancelled': 0, 'pending': 0}
        assert state['requirement_coverage']['repaired_job_ids'] == ['primaryJob']
        assert fixed.call_count == 1 and fixed.call_args.args[0]['inputs'] == [inputs[2]]
        assert submit.call_count == 1 and submit.call_args.args[0].engines == ['paddlevl']
        retry_operation = runtime.store.rows('SELECT * FROM operations WHERE parent_id=?', (original_operation['id'],))
        assert len(retry_operation) == 1
        assert json.loads(runtime.store.rows('SELECT result FROM operations WHERE id=?', (original_operation['id'],))[0]['result']) == original
        runtime.store.status(run['id'], 1, 'running')
        asyncio.run(image_text.drive(runtime, run['id']))
        assert submit.call_count == 1 and runtime.settle.await_count == 1
        assert len(runtime.store.rows("SELECT seq FROM events WHERE kind='delivery'")) == 1

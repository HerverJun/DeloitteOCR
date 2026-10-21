"""Data/classification recover only from authoritative original-key absence."""
import asyncio
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from workbench_platform.agent.store import AgentStore, digest
from workbench_platform.application_services.context import ToolContext
from workbench_platform.application_services.errors import NativeNotSubmitted
from workbench_platform.application_services.image_text import _operation, ReceiptUnknown
from workbench_platform.application_services.operations import classification, data
from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.data_api.service import DataWorkbenchService, DataError
from workbench_platform.jobs.classification_handoff import ClassificationHandoff
from workbench_platform.jobs.native_classification import ClassificationAdapter, ClassificationUnavailable


def intent(tmp_path, tool, arguments):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspace', 'Original fixed goal', [], 'session')
    run, _ = store.start_run(session['id'], 'Original fixed goal', 'original-goal', 0,
        driver='data-action-v1', driver_state={'resource_object': 'project:original', 'scope_workspace_id': 'workspace'})
    operation, _ = store.begin_operation(run['id'], 1,
        {'call_id': 'direct-' + tool + '-' + digest(arguments)[:24], 'tool': tool, 'arguments': arguments})
    with store.transaction() as db:
        db.execute("UPDATE runs SET status='paused',generation=2 WHERE id=?", (run['id'],))
    store.status(run['id'], 2, 'running')
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
    ctx = ToolContext('workspace', session['id'], run['id'], 2, 0, '', (), services, store)
    return SimpleNamespace(store=store), ctx, operation


@pytest.fixture
def native_data(tmp_path):
    service = DataWorkbenchService(tmp_path / 'native-data', Path(__file__))
    service.pool.shutdown()
    service.pool = Mock()  # Accepted SQLite jobs, without launching a worker.
    yield service


def test_data_paused_unsent_intent_reuses_original_native_key_and_never_duplicates(tmp_path, native_data):
    session = native_data.create('Original table', workspace_id='workspace')
    arguments = {'data_session_id': session['id'], 'expected_version': 1, 'side': 'left',
                 'step': {'operator': 'trim', 'column': 'c1'}}
    runtime, ctx, original = intent(tmp_path, 'data_apply_step', arguments)
    ctx.services.data_workbench = native_data
    parsed = data.DataStep.model_validate(arguments)
    current = replace(ctx, operation_id=original['id'])
    assert data.lookup(parsed, current) is None
    proof = data.lookup(parsed, current, include_not_submitted=True)
    assert isinstance(proof, NativeNotSubmitted) and proof.request_id == original['id']
    handler = Mock(side_effect=lambda args, current: data.apply_step(data.DataStep(**args), current))
    lookup = lambda args, current: data.lookup(data.DataStep(**args), current, include_not_submitted=True)
    first, _ = asyncio.run(_operation(runtime, ctx, 'data_apply_step', arguments, handler, lookup=lookup))
    second, _ = asyncio.run(_operation(runtime, ctx, 'data_apply_step', arguments, handler, lookup=lookup))
    assert first == second and first['status'] == 'pending' and handler.call_count == 1
    job = native_data.operation(original['id'])
    replay = native_data.submit(session['id'], 'step', {'side': 'left', 'operator': 'trim',
        'parameters': {'column': 'c1'}}, 1, request_key=original['id'])
    assert replay['id'] == job['id'] and native_data.pool.submit.call_count == 1
    with closing(native_data.db()) as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        saved = db.execute('SELECT * FROM operation_requests').fetchall()
        assert len(saved) == 1 and saved[0]['request_key'] == original['id'] and saved[0]['version'] == 1
    assert len(runtime.store.rows('SELECT * FROM operations')) == 1
    assert len(runtime.store.rows("SELECT * FROM events WHERE kind='native_not_submitted'")) == 1


@pytest.mark.parametrize('condition', ['new_version', 'wrong_workspace', 'orphan_request', 'unavailable'])
def test_data_native_absence_does_not_hide_changed_scope_or_incomplete_authority(native_data, condition):
    session = native_data.create('Original', workspace_id='workspace')
    if condition == 'new_version':
        with closing(native_data.db()) as db, db:
            db.execute('UPDATE sessions SET version=2 WHERE id=?', (session['id'],))
    elif condition == 'orphan_request':
        with closing(native_data.db()) as db, db:
            db.execute('INSERT INTO operation_requests VALUES(?,?,?,?,?,?)',
                       ('original-operation', session['id'], 'missing-job', 'a' * 64, 1, '{}'))
    elif condition == 'unavailable':
        native_data.closing.set()
    assert native_data.operation_not_submitted('original-operation', session['id'], 1,
        'different-workspace' if condition == 'wrong_workspace' else 'workspace') is False
    if condition == 'orphan_request':
        with pytest.raises(DataError):
            native_data.operation('original-operation')


@pytest.mark.parametrize('authority', ['missing', 'none', 'error'])
def test_data_unknown_native_authority_keeps_original_intent_blocked(tmp_path, native_data, authority):
    session = native_data.create('Original', workspace_id='workspace')
    arguments = {'data_session_id': session['id'], 'expected_version': 1, 'side': 'left'}
    runtime, ctx, original = intent(tmp_path, 'data_export', arguments)
    ctx.services.data_workbench = native_data
    native_data.operation_not_submitted = None if authority == 'missing' else Mock(return_value=None)
    if authority == 'error':
        native_data.operation_not_submitted.side_effect = OSError('Authority unavailable')
    handler = Mock(side_effect=AssertionError('No blind export resubmission'))
    with pytest.raises(ReceiptUnknown):
        asyncio.run(_operation(runtime, ctx, 'data_export', arguments, handler,
            lookup=lambda args, current: data.lookup(data.DataExport(**args), current, include_not_submitted=True)))
    handler.assert_not_called()
    assert runtime.store.rows('SELECT state FROM operations')[0]['state'] == 'intent'


@pytest.mark.parametrize('tool,kind,fields', [
    ('data_import', data.DataImport, {'side': 'left', 'asset_id': 'original-asset',
        'asset_revision': 1, 'blob_hash': 'a' * 64}),
    ('data_apply_step', data.DataStep, {'side': 'left', 'step': {'operator': 'trim', 'column': 'c1'}}),
    ('data_compare', data.DataCompare, {'spec': {'key_pairs': [['c1', 'c1']]}}),
    ('data_export', data.DataExport, {'side': 'left'}),
])
def test_data_negative_receipt_is_bound_to_each_original_fixed_operator(native_data, tool, kind, fields):
    session = native_data.create('Original', workspace_id='workspace')
    ctx = SimpleNamespace(workspace_id='workspace', operation_id='original-operation', current=Mock(),
                          services=SimpleNamespace(data_workbench=native_data))
    args = kind(data_session_id=session['id'], expected_version=1, **fields)
    with patch.object(data, 'scoped_asset') as asset:
        assert data.lookup(args, ctx) is None
        proof = data.lookup(args, ctx, include_not_submitted=True)
        assert isinstance(proof, NativeNotSubmitted) and proof.tool == tool
        assert proof.request_id == proof.operation_id == ctx.operation_id and proof.project_id == session['id']
        assert asset.call_count == (1 if kind is data.DataImport else 0)


def classification_handoff(tmp_path):
    requests, posts = {}, []
    adapter = object.__new__(ClassificationAdapter)
    adapter.context = SimpleNamespace(module_project_id='original-project', role='owner')
    adapter.import_status = Mock(return_value={'state': 'completed', 'items': [{'target_asset_id': 'original-image'}]})
    adapter.models = Mock(return_value=[{'id': 'original-model'}])
    def request(method, path, body=None, **kwargs):
        assert '/projects/original-project/' in path
        if method == 'GET' and '/inference-requests/' in path:
            job = requests.get(path.rsplit('/', 1)[-1])
            return {'submitted': job is not None, 'job_id': job}
        assert method == 'POST' and path.endswith('/batch-jobs')
        assert body == {'model_id': 'original-model', 'asset_ids': ['original-image'],
                        'device': 'cuda', 'auto_accept': False}
        posts.append((kwargs['key'], body))
        return {'job_id': requests.setdefault(kwargs['key'], 'original-native-job')}
    adapter._request = request
    jobs = SimpleNamespace(register=Mock(return_value={'platform_job_id': 'original-platform-job', 'generation': 1}))
    handoff = ClassificationHandoff(tmp_path / 'classification.sqlite3', jobs, None, None)
    handoff._context = lambda *_: adapter.context
    handoff._adapter = lambda *_: adapter
    handoff.status = lambda *_: {'handoff_id': 'original-handoff', 'processing': {'model_id': 'original-model'}}
    with closing(handoff._connect()) as db:
        db.execute('INSERT INTO classification_handoff VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('original-handoff', 'workspace', 'original-link', 'original-set', 1, 'a' * 64,
             'original-import', '{"items":[{"name":"original.png"}]}', None, 'now', 'now'))
    return handoff, adapter, requests, posts


def test_classification_authoritative_negative_resumes_same_model_device_input_and_key(tmp_path):
    handoff, adapter, requests, posts = classification_handoff(tmp_path)
    negative = handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda', allow_new=False)
    assert negative['authoritative_negative'] is True and posts == []
    assert negative['native_project_id'] == 'original-project' and len(negative['input_fingerprint']) == 64
    first = handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda')
    again = handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda')
    assert first == again and len(posts) == 1 and posts[0][0] == negative['native_request_id']
    assert requests == {negative['native_request_id']: 'original-native-job'}
    with closing(handoff._connect()) as db:
        rows = db.execute('SELECT * FROM classification_submission').fetchall()
        assert len(rows) == 1 and rows[0]['model_id'] == 'original-model' and rows[0]['device'] == 'cuda'
        db.execute("UPDATE classification_submission SET request_id='wrong-original-key',native_job_id=NULL")
    with pytest.raises(AssetSetConflict):
        handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda', allow_new=False)
    assert len(posts) == 1


@pytest.mark.parametrize('bad', [None, {}, {'submitted': 0, 'job_id': None},
    {'submitted': False, 'job_id': 'unbound-job'}, {'submitted': True, 'job_id': None}, 'transport'])
def test_classification_malformed_or_unavailable_native_receipt_never_becomes_negative(tmp_path, bad):
    handoff, adapter, requests, posts = classification_handoff(tmp_path)
    adapter._request = Mock(side_effect=ClassificationUnavailable()) if bad == 'transport' else Mock(return_value=bad)
    with pytest.raises(ClassificationUnavailable):
        handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda', allow_new=False)
    assert posts == []


def test_classification_missing_native_lookup_is_not_authoritative(tmp_path):
    handoff, adapter, requests, posts = classification_handoff(tmp_path)
    adapter.lookup_inference = None
    value = handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda', allow_new=False)
    assert value['not_submitted'] is True and value['authoritative_negative'] is False and posts == []


def test_classification_accepted_native_request_is_adopted_without_another_inference(tmp_path):
    handoff, adapter, requests, posts = classification_handoff(tmp_path)
    negative = handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda', allow_new=False)
    requests[negative['native_request_id']] = 'original-native-job'
    result = handoff.submit('workspace', 'original-handoff', 'original-model', 'cuda', allow_new=False)
    assert result['job']['platform_job_id'] == 'original-platform-job' and posts == []
    assert handoff.jobs.register.call_args.args[-1] == ['original-native-job']


def test_classification_cold_intent_prepares_same_link_and_retains_business_identity(tmp_path):
    arguments = {'link_id': 'original-link', 'asset_set_id': 'original-set', 'asset_set_revision': 1,
                 'model_id': 'original-model', 'device': 'cuda'}
    runtime, ctx, original = intent(tmp_path, 'classification_infer', arguments)
    ctx = replace(ctx, operation_id=original['id'])
    handoff, adapter, requests, posts = classification_handoff(tmp_path)
    ctx.services.classification_handoff = handoff
    handoff.begin = Mock(return_value={'handoff_id': 'original-handoff'})
    attempt = SimpleNamespace(asset_set_id='s' + original['id'][1:], revision=1)
    def prepare(launch_context, family, link):
        assert family == 'classification' and link == arguments['link_id']
        assert launch_context.operation_id != original['id']
        assert launch_context.run_id == ctx.run_id and launch_context.generation == ctx.generation
    with patch.object(classification, 'availability', return_value={'state': 'preparable'}), \
         patch.object(classification, 'prepare', side_effect=prepare) as start, \
         patch.object(classification, 'scoped_set', return_value=object()), \
         patch.object(classification, 'attempt_set', return_value=attempt):
        parsed = classification.ClassificationInfer(**arguments)
        assert classification.lookup(parsed, ctx) is None
        proof = classification.lookup(parsed, ctx, include_not_submitted=True)
        assert isinstance(proof, NativeNotSubmitted) and proof.operation_id == original['id']
        assert start.call_count == 1 and posts == []
        result = classification.infer(parsed, ctx)
    assert result['status'] == 'pending' and len(posts) == 1 and posts[0][0] == proof.request_id
    assert all(call.args[1:] == ('original-link', attempt.asset_set_id, 1, {}) for call in handoff.begin.call_args_list)


def test_classification_cold_prepare_losing_generation_never_reaches_inference_lookup(tmp_path):
    arguments = {'link_id': 'original-link', 'asset_set_id': 'original-set', 'asset_set_revision': 1,
                 'model_id': 'original-model'}
    runtime, ctx, original = intent(tmp_path, 'classification_infer', arguments)
    ctx = replace(ctx, operation_id=original['id'])
    def pause(*_):
        with ctx.store.transaction() as db:
            db.execute("UPDATE runs SET status='paused',generation=3 WHERE id=?", (ctx.run_id,))
    with patch.object(classification, 'availability', return_value={'state': 'preparable'}), \
         patch.object(classification, 'prepare', side_effect=pause), \
         patch.object(classification, 'submission') as submit, pytest.raises(ValueError):
        classification.lookup(classification.ClassificationInfer(**arguments), ctx, include_not_submitted=True)
    submit.assert_not_called()

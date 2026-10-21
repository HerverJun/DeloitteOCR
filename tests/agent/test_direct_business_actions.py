"""Typed fixed actions and real Data export readback; no controller inference."""
import asyncio
import csv
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError

from packages.contracts.direct_classification import PredictRequest
from packages.contracts.direct_data import DataActionRequest
from workbench_platform.application_services import classification_action, data_action
from workbench_platform.application_services.operations import classification_management


def test_direct_requests_reject_implicit_training_and_unbounded_or_ambiguous_data_steps():
    with pytest.raises(ValidationError):
        PredictRequest(request_id='request', link_id='link')
    with pytest.raises(ValidationError):
        PredictRequest(request_id='request', link_id='link', model_id='model', train=True)
    base = {'request_id': 'request', 'left_asset_id': 'left', 'action': 'clean'}
    for bad in ({}, {'steps': [{'step': {'operator': 'python', 'code': 'print(1)'}}]},
                {'steps': [{'side': 'right', 'step': {'operator': 'trim', 'column': 'c1'}}]},
                {'right_asset_id': 'right', 'steps': [{'step': {'operator': 'trim', 'column': 'c1'}}]}):
        with pytest.raises(ValidationError):
            DataActionRequest(**(base | bad))
    with pytest.raises(ValidationError):
        DataActionRequest(request_id='request', left_asset_id='left', right_asset_id='right', action='compare',
            compare_spec={'key_pairs': [['c1', 'c1']], 'amount_pairs': [['c3', 'c3']]})
    value = DataActionRequest(**(base | {'steps': [{'step': {'operator': 'cast', 'column': 'c3', 'target': 'decimal'}}]}))
    assert value.steps[0].step.target == 'decimal'


def test_missing_existing_model_stops_before_input_import_and_never_trains():
    request = PredictRequest(request_id='request', link_id='link', model_id='missing')
    saved = {'request': request.model_dump(exclude={'request_id'}), 'receipts': {}}
    runtime = SimpleNamespace(store=SimpleNamespace(run=lambda _: {'driver_state': json.dumps(saved), 'request_id': 'request'}))
    prepared = AsyncMock(return_value={'status': 'success', 'data': {'models': [], 'requires_project_selection': False}})
    with patch.object(classification_action, 'action_context', return_value=object()), \
         patch.object(classification_action, 'operation', prepared), \
         patch.object(classification_management, 'submit') as train, \
         pytest.raises(ValueError, match='未自动启动训练'):
        asyncio.run(classification_action.drive(runtime, 'run'))
    train.assert_not_called()
    assert prepared.await_count == 1


def test_real_data_actions_keep_precision_lineage_full_download_and_replay(tmp_path, monkeypatch):
    import test_data_tools as data_fixtures
    from test_data_tools import DataToolTests
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.tasks import AuditTasks
    root = tmp_path / 'data-fixture'
    (root / 'fixtures').mkdir(parents=True)
    for name in ('suppliers-left.csv', 'suppliers-right.csv'):
        shutil.copyfile(data_fixtures.RUN / 'fixtures' / name, root / 'fixtures' / name)
    monkeypatch.setattr(data_fixtures, 'RUN', root)
    fixture = DataToolTests(methodName='test_real_clean_compare_export_independent_values_and_native_provenance')
    fixture.setUp()
    try:
        fixture.store.status(fixture.ctx.run_id, 1, 'succeeded')
        runtime = object.__new__(AgentRuntime)
        runtime.store, runtime.services = fixture.store, fixture.services
        runtime.tasks, runtime.schedule = AuditTasks(runtime), Mock()
        fixture.services.agent = runtime
        left, right = fixture.asset('suppliers-left.csv'), fixture.asset('suppliers-right.csv')
        session = fixture.store.create_session('wtest', 'Direct tables', [left.asset_id, right.asset_id], 'direct-tables')
        clean = DataActionRequest(request_id='clean-request', action='clean', left_asset_id=left.asset_id,
            steps=[{'step': {'operator': 'trim', 'column': 'c2'}},
                   {'step': {'operator': 'cast', 'column': 'c3', 'target': 'decimal'}}])
        async def execute(request):
            run = await data_action.submit(runtime, session['id'], request)
            await data_action.drive(runtime, run['id'])
            return runtime.store.run(run['id'])
        first = asyncio.run(execute(clean))
        assert first['status'] == 'succeeded'
        first_state = json.loads(first['driver_state'])
        native_session = first_state['receipts']['import_left']['data']['data_session_id']
        fixed = first_state['receipts']['preview']['data']
        assert fixed['rows'][0]['cells'][0]['value'] == '000001'
        assert fixed['rows'][1]['cells'][0]['value'] == '123456789012345678901234'
        assert fixed['rows'][0]['cells'][1]['value'] == '甲公司'
        assert fixed['rows'][0]['cells'][2]['value'] == '100.00'
        assert fixed['rows'][0]['cells'][4]['raw'] == '=SUM(1,2)'
        native_upload = fixture.native.get(native_session)['state']['tables']['left']['source']['asset_id']
        assert fixture.native.upload_source(native_session, native_upload)['source_asset_id'] == left.asset_id
        job_count = len(fixture.store.rows('SELECT id FROM operations'))
        replay = asyncio.run(data_action.submit(runtime, session['id'], clean))
        assert replay['id'] == first['id'] and len(fixture.store.rows('SELECT id FROM operations')) == job_count
        compare = DataActionRequest(request_id='compare-request', action='compare', left_asset_id=left.asset_id,
            right_asset_id=right.asset_id, steps=[{'side': side, 'step': step} for side in ('left', 'right') for step in
                ({'operator': 'trim', 'column': 'c2'}, {'operator': 'cast', 'column': 'c3', 'target': 'decimal'})],
            compare_spec={'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c2', 'c2'], ['c3', 'c3']],
                          'amount_pairs': [['c3', 'c3']], 'amount_tolerance': '1'})
        second = asyncio.run(execute(compare))
        assert second['status'] == 'succeeded'
        state = json.loads(second['driver_state'])
        bundle = state['receipts']['exported']['data']['bundle']
        artifact = fixture.center.file('wtest', bundle['bundle_id'], 'results.csv')
        with fixture.center.store.stage_download('wtest', artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            with path.open('r', encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
        differences = sorted(row['L_c1_value'] for row in rows if row['category'] == 'different')
        assert differences == ['000001', '000003']
        assert sorted(row['L_c3_value'] for row in rows if row['category'] == 'different') == ['100.00', '50.00']
        assert len(state['receipts']['delivery']['data']['delivery'][0]['files']) == 5
        assert not fixture.store.rows('SELECT id FROM model_requests')
    finally:
        fixture.tearDown()

"""Partial delivery retains one goal and never replays accepted/unknown work."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.product_compatibility import DATA_COMPATIBILITY, check
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, Conflict, canonical
from workbench_platform.agent.tasks import AuditTasks
from workbench_platform.agent.task_recovery import can_resume_run
from workbench_platform.agent import routes


def fixture(tmp_path, status='partial'):
    runtime = object.__new__(AgentRuntime)
    runtime.store = AgentStore(tmp_path / 'agent')
    session = runtime.store.create_session('workspace', 'Original', ['one', 'two', 'three'], 'session')
    run, _ = runtime.store.start_run(session['id'], 'Process all three; preserve original IDs', 'original', 1,
        driver_state={'scope_claims': {'version': 1, 'workspace_id': 'workspace', 'projects': {'ocr': ['shared']}}})
    partial = {'status': 'success', 'summary': 'Two of three completed', 'artifact_refs': [{'object_id': 'first'}],
        'result_refs': [], 'job_refs': [], 'data': {'verified': True, 'finalized': True, 'completeness': 'partial',
        'requirement_coverage': {'missing': 1, 'missing_requirements': ['third input'], 'unverified': []},
        'limitations': ['Third input not submitted']}}
    state = {'run_id': run['id'], 'session_id': session['id'], 'workspace_id': 'workspace', 'generation': 1,
        'messages': [{'id': 'original', 'role': 'user', 'content': 'Process all three; preserve original IDs'}],
        'delivery': partial, 'answer': {'old': 'only stops controller'}, 'wait': 'budget_stopped',
        'delivery_display_id': 'old-partial', 'business_started': True, 'step': 4, 'tool_count': 3,
        'entered': True, 'continuation_authorized': True, 'no_progress': 8, 'visible': []}
    snapshots = [deepcopy(state)]
    runtime.graph = SimpleNamespace(load=lambda _: deepcopy(snapshots[-1]),
        save=lambda value: snapshots.append(deepcopy(value)))
    runtime.services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: {}))
    runtime.registry = SimpleNamespace(tools={})
    runtime.model_provider = True
    runtime.agent_enabled = True
    runtime.maintenance = {}; runtime.running = {}
    runtime.tasks = AuditTasks(runtime)
    runtime.tasks.record_goal(session['id'], state['messages'][0]['content'], 'original')
    runtime.schedule = Mock()
    accepted, _ = runtime.store.begin_operation(run['id'], 1,
        {'call_id': 'accepted', 'tool': 'native_write', 'arguments': {'original_parameters': True}})
    runtime.store.finish_operation(accepted['id'], {'status': 'success', 'summary': 'Native already accepted',
        'data': {}, 'job_refs': [{'object_id': 'accepted-job'}], 'result_refs': [], 'artifact_refs': []})
    runtime.store.event(session['id'], run['id'], 'old-partial-event', 'delivery', {'status': 'partial', 'delivery': partial})
    runtime.store.status(run['id'], 1, status)
    return runtime, session, runtime.store.run(run['id']), state, snapshots


def test_old_partial_api_and_continue_keep_goal_receipts_generation_and_artifacts(tmp_path, monkeypatch):
    runtime, session, run, state, snapshots = fixture(tmp_path)
    monkeypatch.setattr(routes, 'runtime', lambda *_: runtime)
    monkeypatch.setattr(routes, 'session_scope', lambda *_: session)
    monkeypatch.setattr(routes, 'run_scope', lambda *_: run)
    assert routes.session(session['id'], object())['runs'][0]['can_resume']
    assert routes.run(run['id'], object())['can_resume']
    operations = runtime.store.rows('SELECT * FROM operations')
    events = runtime.store.rows("SELECT * FROM events WHERE kind IN ('message','delivery') ORDER BY seq")
    resumed = asyncio.run(runtime.reconcile(run['id']))
    assert resumed['id'] == run['id'] and resumed['generation'] == 1 and resumed['status'] == 'running'
    assert runtime.store.rows('SELECT * FROM operations') == operations
    assert runtime.store.rows("SELECT * FROM events WHERE kind IN ('message','delivery') ORDER BY seq") == events
    assert runtime.store.session(session['id'])['selected_assets'] == ['one', 'two', 'three']
    assert len(runtime.store.rows('SELECT id FROM runs')) == 1
    runtime.schedule.assert_called_once_with(run['id'], None)
    current = snapshots[-1]
    assert current['delivery'] is None and current['answer'] is None and 'wait' not in current
    assert current['partial_delivery'] == state['delivery']
    assert current['remaining_delivery_work']['missing_requirements'] == ['third input']
    assert current['remaining_delivery_work']['as_of_last_partial_delivery'] is True
    assert 'tools_describe' in current['remaining_delivery_work']['instruction']
    assert current['messages'] == state['messages'] and current['business_started'] is True
    marker = tmp_path / 'product-reader-requirements.json'
    assert 'partial-goal-continuation-v1' in json.loads(marker.read_text())['reader_features']
    old_reader = deepcopy(DATA_COMPATIBILITY)
    old_reader['reader_features'].remove('partial-goal-continuation-v1')
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='不能直接降级'):
        check(tmp_path, old_reader)
    assert marker.read_bytes() == before and check(tmp_path, DATA_COMPATIBILITY)['compatible']


def test_partial_unknown_native_write_still_blocks_before_activation_or_model(tmp_path):
    runtime, session, run, state, snapshots = fixture(tmp_path)
    runtime.registry.tools['unconfirmed'] = SimpleNamespace(effect='versioned', idempotent=False, lookup_operation=None)
    unknown, _ = runtime.store.begin_operation(run['id'], 1,
        {'call_id': 'unknown', 'tool': 'unconfirmed', 'arguments': {'unchanged': True}})
    before = runtime.store.rows('SELECT * FROM operations')
    with pytest.raises(Conflict, match='回执尚不可核实'):
        asyncio.run(runtime.reconcile(run['id']))
    assert runtime.store.run(run['id'])['status'] == 'partial'
    assert runtime.store.rows('SELECT * FROM operations') == before
    assert snapshots[-1]['delivery'] == state['delivery']
    runtime.schedule.assert_not_called()


def test_partial_continue_reacquires_real_scope_and_refuses_new_active_owner(tmp_path):
    runtime, session, run, state, snapshots = fixture(tmp_path)
    other = runtime.store.create_session('another-workspace', 'Another', [], 'other')
    owner, _ = runtime.store.start_run(other['id'], 'Same OCR project', 'new-owner', 1, driver='image-text-v1',
        driver_state={'resource_object': 'project:shared'})
    with pytest.raises(Conflict, match='同一业务对象'):
        asyncio.run(runtime.reconcile(run['id']))
    assert runtime.store.run(run['id'])['status'] == 'partial'
    assert runtime.store.run(owner['id'])['status'] == 'running'
    runtime.schedule.assert_not_called()
    with runtime.store.transaction() as db:
        runtime.store.cancel_in_transaction(db, run_id=owner['id'])
    assert asyncio.run(runtime.reconcile(run['id']))['status'] == 'running'


@pytest.mark.parametrize('status', ['partial', 'paused'])
def test_cancelled_partial_or_paused_task_can_never_resume_or_dispatch(tmp_path, status):
    runtime, session, run, state, snapshots = fixture(tmp_path, status)
    with runtime.store.transaction() as db:
        changed = runtime.store.cancel_in_transaction(db, run_id=run['id'])
    assert len(changed) == 1 and changed[0]['generation'] == 2
    cancelled = runtime.store.run(run['id'])
    assert can_resume_run(runtime, cancelled) is False
    before = runtime.store.rows('SELECT * FROM operations')
    with pytest.raises(Conflict, match='用户已停止'):
        asyncio.run(runtime.reconcile(run['id']))
    assert runtime.store.rows('SELECT * FROM operations') == before
    runtime.schedule.assert_not_called()


def test_final_partial_publishes_available_results_but_pauses_with_explicit_missing_work(tmp_path):
    runtime, session, run, state, snapshots = fixture(tmp_path, 'running')
    runtime.inbox = lambda _: ([], [])
    runtime.close_presentations = Mock()
    assert asyncio.run(runtime.harness_finalize(state)) is True
    paused = runtime.store.run(run['id'])
    assert paused['status'] == 'paused' and can_resume_run(runtime, paused)
    assert 'Third input' in paused['error']
    assert state['partial_delivery']['artifact_refs'] == [{'object_id': 'first'}]
    assert state['remaining_delivery_work']['missing'] == 1
    assert runtime.store.rows("SELECT payload FROM events WHERE kind='delivery'")
    runtime.close_presentations.assert_called_once_with(state, 'paused', '部分成果已保存，继续原任务可补齐剩余事项')


def test_supplement_to_latest_partial_uses_same_run_and_never_old_partial_after_new_success(tmp_path, monkeypatch):
    from workbench_platform.application_services import goal_routing
    from unittest.mock import AsyncMock
    runtime, session, run, state, snapshots = fixture(tmp_path)
    result = asyncio.run(runtime.submit(session['id'], '服务已恢复，继续未完成项', 'supplement'))
    assert result['id'] == run['id'] and result['status'] == 'running'
    assert len(runtime.store.rows('SELECT id FROM runs')) == 1
    runtime.store.status(run['id'], 1, 'partial')
    newer, _ = runtime.store.start_run(session['id'], 'Independent later goal', 'newer', 1)
    runtime.store.status(newer['id'], 1, 'succeeded')
    local = AsyncMock(return_value={'id': 'new-goal'})
    monkeypatch.setattr(goal_routing, 'submit_local_goal', local)
    assert asyncio.run(runtime.submit(session['id'], 'A separate goal', 'separate'))['id'] == 'new-goal'
    assert len(runtime.store.rows('SELECT * FROM inbox WHERE run_id=?', (run['id'],))) == 1


def test_legacy_partial_restores_original_staged_checks_once(tmp_path):
    runtime, session, run, state, snapshots = fixture(tmp_path)
    record = {'operation_id': 'old-publish', 'run_id': run['id'], 'created': 'old', 'summary': 'Two delivered',
        'artifact_refs': [{'kind': 'artifact', 'workspace_id': 'workspace', 'object_id': 'first', 'revision': 1}],
        'result_refs': [], 'checks': [{'name': 'row_count', 'bundle_id': 'first', 'expected_rows': 3}],
        'files': [{'bundle_id': 'first', 'filename': 'cleaned.csv', 'sha256': 'immutable'}],
        'completeness': 'partial', 'finalized': True, 'consumed_stages': []}
    with runtime.store.transaction() as db:
        body, revision = runtime.tasks._body(db, session['id'])
        body['delivery_requirements'] = {'version': 1, 'stages': [], 'history': [record]}
        runtime.tasks._save(db, session['id'], body, revision)
    asyncio.run(runtime.reconcile(run['id']))
    req = runtime.tasks.get(session['id'])['delivery_requirements']
    assert req['history'] == [record] and req['stages'][0]['checks'] == record['checks']
    assert req['stages'][0]['files'] == record['files']
    runtime.store.status(run['id'], 1, 'paused')
    asyncio.run(runtime.reconcile(run['id']))
    assert runtime.tasks.get(session['id'])['delivery_requirements'] == req


def test_complete_delivery_after_partial_emits_new_receipt_and_stops_as_success(tmp_path):
    runtime, session, run, state, snapshots = fixture(tmp_path)
    asyncio.run(runtime.reconcile(run['id']))
    current = snapshots[-1]
    current['delivery'] = deepcopy(state['delivery'])
    current['delivery']['summary'] = 'All three delivered'
    current['delivery']['data'].update(completeness='complete', limitations=[],
        requirement_coverage={'missing': 0, 'missing_requirements': [], 'unverified': []})
    runtime.inbox = lambda _: ([], [])
    runtime.close_presentations = Mock()
    assert asyncio.run(runtime.harness_finalize(current)) is True
    completed = runtime.store.run(run['id'])
    assert completed['status'] == 'succeeded' and not can_resume_run(runtime, completed)
    events = [json.loads(row['payload']) for row in runtime.store.rows("SELECT payload FROM events WHERE kind='delivery' ORDER BY seq")]
    assert [event['status'] for event in events] == ['partial', 'succeeded']
    assert events[0]['delivery'] == state['delivery'] and events[-1]['content'] == 'All three delivered'
    assert 'partial_delivery' not in current and 'remaining_delivery_work' not in current

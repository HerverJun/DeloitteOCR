"""Deterministic J02/J07/J09 regressions; no live model or browser claim."""
import asyncio
from types import SimpleNamespace

import pytest

from workbench_platform.agent.harness_service import HarnessService
from workbench_platform.agent.store import AgentStore, Conflict
from workbench_platform.agent.goal_progress import observe, reset_for_input
from workbench_platform.application_services.direct_scope import resolve_harness_scope


def result(status='success', **values):
    return {'status': status, 'summary': 'receipt', 'data': {}, 'job_refs': [],
            'result_refs': [], 'artifact_refs': [], **values}


def runner(tmp_path):
    store = AgentStore(tmp_path / 'agent')
    session = store.create_session('workspaceA', 'Original goal', [], 'session')
    run, _ = store.start_run(session['id'], 'Compare amounts', 'run', 1)
    state = {'run_id': run['id'], 'session_id': session['id'], 'workspace_id': 'workspaceA',
             'generation': 1, 'step': 0, 'tool_count': 0, 'visible': ['delivery_publish', 'read_result'],
             'results': [], 'calls': []}
    async def dispatch(call, context):
        return call['arguments']['receipt']
    runtime = HarnessService()
    runtime.store = store
    runtime.services = SimpleNamespace(workspace_store=SimpleNamespace(list_links=lambda _: []))
    runtime.registry = SimpleNamespace(tools={name: SimpleNamespace(effect='read') for name in state['visible']},
        validate_batch=lambda *args: None, dispatch=dispatch)
    runtime.current = lambda _: store.run(run['id'])
    runtime.inbox = lambda _: ([], [])
    runtime.context = lambda _: SimpleNamespace()
    runtime.tasks = SimpleNamespace(get=lambda _: {'pending_scope': None})
    runtime.graph = SimpleNamespace(save=lambda _: None)
    runtime.activity = lambda *args, **kwargs: None
    runtime.event = lambda *args, **kwargs: None
    async def collect(*args):
        return None
    runtime.collect_available_results = collect
    return runtime, state


def execute(runtime, state, name, receipt, index):
    return asyncio.run(runtime.harness_tool(state, {'call_id': 'call' + str(index), 'tool': name,
        'arguments': {'receipt': receipt, 'irrelevant_retry_number': index}}))


def test_alternating_delivery_failure_and_same_readback_is_bounded(tmp_path):
    runtime, state = runner(tmp_path)
    for index in range(12):
        receipt = (result('error', error={'code': 'delivery_checks_failed', 'message': 'amount missing'})
                   if index % 2 == 0 else result(data={'items': [{'text': 'unchanged 10.00'}]}))
        execute(runtime, state, 'delivery_publish' if index % 2 == 0 else 'read_result', receipt, index)
    assert state.get('no_progress', 0) >= 6
    assert state.get('progress_blocker', {}).get('code') == 'delivery_checks_failed'
    runtime.harness_wait = lambda _: asyncio.sleep(0)
    runtime.tasks.apply_scope = lambda _: None
    runtime.harness_config = lambda _: {}
    stopped = asyncio.run(runtime.harness_prepare(state, {'messages': [], 'incoming': []}))
    assert stopped == {'stop': True}
    assert runtime.store.run(state['run_id'])['status'] == 'paused'
    assert 'amount missing' in runtime.store.run(state['run_id'])['error']


def test_real_business_progress_can_exceed_twenty_steps(tmp_path):
    runtime, state = runner(tmp_path)
    for index in range(30):
        execute(runtime, state, 'read_result', result(data={'items': [{'text': 'page ' + str(index)}]}), index)
        assert state.get('no_progress', 0) == 0


def test_claimed_paused_harness_allows_unrelated_project_but_not_shared_object(tmp_path):
    store = AgentStore(tmp_path / 'agent')
    a = store.create_session('workspaceA', 'A', [], 'sessionA')
    original, _ = store.start_run(a['id'], 'Analyze A', 'runA', 1, driver_state={
        'scope_claims': {'version': 1, 'workspace_id': 'workspaceA', 'projects': {'ocr': ['nativeA']}}})
    store.status(original['id'], 1, 'paused')
    b = store.create_session('workspaceB', 'B', [], 'sessionB')
    independent, _ = store.start_run(b['id'], 'Extract B', 'runB', 0, driver='image-text-v1',
        driver_state={'resource_object': 'project:nativeB'})
    assert independent['status'] == 'running'
    c = store.create_session('workspaceC', 'C', [], 'sessionC')
    with pytest.raises(Conflict):
        store.start_run(c['id'], 'Same object', 'runC', 0, driver='pdf-text-v1',
                        driver_state={'resource_object': 'project:nativeA'})


def test_healthy_native_wait_and_user_information_do_not_trigger_stall():
    state = {}
    for index in range(40):
        observe(state, {}, result('pending', job_refs=[{'native_id': 'accepted'}]))
    assert state.get('no_progress', 0) == 0
    for index in range(6):
        observe(state, {}, result('error', error={'code': 'same_blocker'}))
    reset_for_input(state, [{'role': 'user', 'id': 'reply', 'content': 'Use gross amount'}])
    assert state['no_progress'] == 0
    observe(state, {}, result('error', error={'code': 'same_blocker'}))
    reset_for_input(state, [{'role': 'user', 'id': 'reply', 'content': 'Use gross amount'}])
    assert state['no_progress'] == 1


def test_parameter_tool_and_volatile_response_changes_are_not_progress():
    state = {}
    for index in range(12):
        observe(state, {'tool': 'tool' + str(index), 'arguments': {'retry': index}},
                result(data={'request_id': str(index), 'timestamp': index, 'tools': [str(index)]}))
    assert state['no_progress'] == 12


def test_equal_values_on_new_real_pages_still_advance_coverage():
    state = {}
    for index in range(30):
        observe(state, {}, result(data={'offset': index, 'items': [{'text': 'same repeated value'}]}))
        assert state['no_progress'] == 0
    for index in range(12):
        observe(state, {}, result(data={'offset': index + 30, 'items': []}))
    assert state['no_progress'] == 12


def test_legacy_harness_scope_is_resolved_and_new_links_rechecked_before_write(tmp_path):
    store = AgentStore(tmp_path / 'agent')
    links = {'workspaceA': ['nativeA'], 'workspaceB': ['nativeB']}
    store.scope_resolver = lambda run: {'version': 1, 'workspace_id': run['workspace_id'],
        'projects': {'ocr': links[run['workspace_id']]}}
    a = store.create_session('workspaceA', 'A', [], 'sessionA')
    original, _ = store.start_run(a['id'], 'Analyze A', 'runA', 1)
    store.status(original['id'], 1, 'paused')
    b = store.create_session('workspaceB', 'B', [], 'sessionB')
    direct, _ = store.start_run(b['id'], 'Extract B', 'runB', 0, driver='image-text-v1',
        driver_state={'resource_object': 'project:nativeB'})
    links['workspaceA'].append('nativeB')
    store.status(original['id'], 1, 'running')
    with pytest.raises(Conflict, match=direct['id']):
        store.claim_harness_scope(original['id'], 1)


def test_readonly_links_are_excluded_and_shared_native_ids_are_preserved():
    runtime = SimpleNamespace(services=SimpleNamespace(workspace_store=SimpleNamespace(list_links=lambda _: [
        SimpleNamespace(app_id='ocr', module_project_id='shared', role='editor'),
        SimpleNamespace(app_id='ocr', module_project_id='readonly', role='viewer')])))
    scope = resolve_harness_scope(runtime, {'workspace_id': 'workspaceA'})
    assert scope['projects'] == {'ocr': ['shared']}


def test_independent_unlinked_direct_projects_and_prepared_shared_object(tmp_path):
    store = AgentStore(tmp_path / 'agent')
    a = store.create_session('workspaceA', 'A', [], 'sessionA')
    original, _ = store.start_run(a['id'], 'Extract A', 'runA', 0, driver='image-text-v1',
        driver_state={'resource_object': '*'})
    store.status(original['id'], 1, 'paused')
    b = store.create_session('workspaceB', 'B', [], 'sessionB')
    independent, _ = store.start_run(b['id'], 'Extract B', 'runB', 0, driver='image-text-v1',
        driver_state={'resource_object': '*'})
    assert independent['status'] == 'running'
    store.status(independent['id'], 1, 'succeeded')
    import json
    saved = json.loads(original['driver_state'])
    saved['receipts'] = {'prepared': {'project_id': 'nowShared', 'link_id': 'linkA'}}
    store.status(original['id'], 1, 'running')
    store.save_driver_state(original['id'], 1, saved)
    store.status(original['id'], 1, 'paused')
    with pytest.raises(Conflict, match=original['id']):
        store.start_run(b['id'], 'Shared native', 'runC', 0, driver='pdf-text-v1',
            driver_state={'resource_object': 'project:nowShared'})


def test_live_link_added_after_unlinked_direct_start_still_fences_shared_native(tmp_path):
    store = AgentStore(tmp_path / 'agent')
    links = {'workspaceA': [], 'workspaceB': ['shared']}
    store.scope_resolver = lambda run: {'version': 1, 'workspace_id': run['workspace_id'],
        'projects': {'ocr': links[run['workspace_id']]}}
    a = store.create_session('workspaceA', 'A', [], 'sessionA')
    original, _ = store.start_run(a['id'], 'Extract A', 'runA', 0, driver='image-text-v1',
        driver_state={'resource_object': '*'})
    store.status(original['id'], 1, 'paused')
    links['workspaceA'].append('shared')
    b = store.create_session('workspaceB', 'B', [], 'sessionB')
    with pytest.raises(Conflict, match=original['id']):
        store.start_run(b['id'], 'Shared native', 'runB', 0, driver='pdf-text-v1',
            driver_state={'resource_object': 'project:shared'})


def test_direct_write_rechecks_new_shared_link_and_can_continue_without_unknown_intent(tmp_path):
    from unittest.mock import Mock
    from workbench_platform.application_services.context import ToolContext
    from workbench_platform.application_services.errors import ResourceBusy
    from workbench_platform.application_services.image_text import _operation
    store = AgentStore(tmp_path / 'agent')
    links = {'workspaceA': [], 'workspaceB': []}
    store.scope_resolver = lambda run: {'version': 1, 'workspace_id': run['workspace_id'],
        'projects': {'ocr': links[run['workspace_id']]}}
    runs = []
    for suffix in ('A', 'B'):
        session = store.create_session('workspace' + suffix, suffix, [], 'session' + suffix)
        run, _ = store.start_run(session['id'], 'Extract', 'run' + suffix, 0,
            driver='image-text-v1', driver_state={'resource_object': '*'})
        runs.append(run)
    links['workspaceA'].append('shared')
    links['workspaceB'].append('shared')
    first, second = runs
    services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()))
    ctx = ToolContext(first['workspace_id'], first['session_id'], first['id'], 1, 0, '', (), services, store)
    handler = Mock(return_value=result())
    with pytest.raises(ResourceBusy, match=second['id']):
        asyncio.run(_operation(SimpleNamespace(), ctx, 'ocr_run', {}, handler))
    handler.assert_not_called()
    assert store.rows('SELECT id FROM operations WHERE run_id=?', (first['id'],)) == []
    store.status(second['id'], 1, 'succeeded')
    receipt, _ = asyncio.run(_operation(SimpleNamespace(), ctx, 'ocr_run', {}, handler))
    assert receipt['status'] == 'success'
    assert handler.call_count == 1
    store.status(second['id'], 1, 'running')
    # A known receipt stays readable even while a newly linked run competes.
    again, _ = asyncio.run(_operation(SimpleNamespace(), ctx, 'ocr_run', {}, handler))
    assert again == receipt and handler.call_count == 1

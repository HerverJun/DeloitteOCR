"""Same-task continuation reuses accepted local output and durable user input."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, Conflict


@pytest.fixture
def runtime(tmp_path):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.store = AgentStore(tmp_path / 'agent')
    runtime.services = SimpleNamespace(workspace_store=SimpleNamespace(list_links=lambda workspace: []))
    runtime.tasks = SimpleNamespace(get=lambda sid: {'manual': False}, record_goal=Mock())
    runtime.connection = SimpleNamespace(view=lambda: {'available': True, 'revision': 9})
    runtime.model_provider = None
    runtime.agent_enabled = True
    runtime.maintenance = {}
    runtime.schedule = Mock()
    runtime.reconcile = Mock()
    runtime.graph = SimpleNamespace(save=Mock())
    runtime.initial_state = lambda run, content: {'messages': [{'role': 'user', 'content': content}], 'run_id': run['id']}
    return runtime


def direct(runtime):
    session = runtime.store.create_session('w', 'test', ['image'], 'session')
    saved = {'version': 1, 'request': {'engines': [], 'source_text': '提取图片文字'},
             'resource_object': 'project:native1', 'receipts': {'delivery': {'result_refs': [{'object_id': 'fixed1'}]}}}
    run, _ = runtime.store.start_run(session['id'], '提取图片文字', 'initial', 0,
        driver='image-text-v1', driver_state=saved)
    return session, run, saved


def test_inflight_followup_is_accepted_once_in_same_run(runtime):
    session, run, _ = direct(runtime)
    runtime.store.status(run['id'], 1, 'waiting_job')
    async def scenario():
        for _ in range(2):
            result = await runtime.submit(session['id'], '提取完再比较两张图金额', 'addition')
            assert result['id'] == run['id']
    asyncio.run(scenario())
    assert len(runtime.store.rows('SELECT * FROM inbox')) == 1
    assert len(runtime.store.rows('SELECT * FROM runs')) == 1
    assert not runtime.schedule.called
    assert runtime.store.rows('SELECT * FROM operations') == []


def test_completed_local_output_hands_to_analysis_without_new_run_or_submission(runtime):
    session, run, saved = direct(runtime)
    asyncio.run(runtime.submit(session['id'], '再比较金额', 'addition'))
    runtime.store.status(run['id'], 1, 'succeeded')
    asyncio.run(runtime.continue_direct_followups(run['id']))
    updated = runtime.store.run(run['id'])
    assert updated['driver'] == 'harness'
    assert updated['connection_revision'] == 9
    assert json.loads(updated['driver_state'])['request'] == saved['request']
    assert json.loads(updated['driver_state'])['receipts'] == saved['receipts']
    assert 'fixed1' in runtime.graph.save.call_args[0][0]['messages'][-1]['content']
    assert len(runtime.store.rows('SELECT * FROM runs')) == 1
    assert runtime.store.rows('SELECT * FROM operations') == []
    # Original request remains idempotent after its driver changes.
    assert asyncio.run(runtime.submit(session['id'], '提取图片文字', 'initial'))['id'] == run['id']


def test_analysis_connection_loss_keeps_completed_output_and_queued_requirement(runtime):
    session, run, saved = direct(runtime)
    asyncio.run(runtime.submit(session['id'], '再比较金额', 'addition'))
    runtime.store.status(run['id'], 1, 'succeeded')
    runtime.connection.view = lambda: {'available': False}
    asyncio.run(runtime.continue_direct_followups(run['id']))
    updated = runtime.store.run(run['id'])
    assert updated['status'] == 'paused'
    assert '已完成' in updated['error']
    assert json.loads(updated['driver_state'])['receipts'] == saved['receipts']
    assert len(runtime.store.rows('SELECT * FROM inbox')) == 1
    assert not runtime.schedule.called


def test_cancelled_direct_task_never_starts_followup(runtime):
    session, run, _ = direct(runtime)
    asyncio.run(runtime.submit(session['id'], '再比较金额', 'addition'))
    with runtime.store.transaction() as db:
        runtime.store.cancel_in_transaction(db, run_id=run['id'])
    asyncio.run(runtime.continue_direct_followups(run['id']))
    assert not runtime.schedule.called
    assert runtime.store.run(run['id'])['status'] == 'cancelled'


def test_conflicting_engine_revision_explains_choices_without_changing_accepted_input(runtime):
    session, run, saved = direct(runtime)
    with pytest.raises(Conflict, match='停止后续步骤'):
        asyncio.run(runtime.submit(session['id'], '改用glm重新识别', 'revision'))
    assert json.loads(runtime.store.run(run['id'])['driver_state']) == {**saved, 'scope_workspace_id': 'w'}
    assert runtime.store.rows('SELECT * FROM inbox') == []

"""Historical recoverable failed runs remain one user goal at the main entry."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore
from workbench_platform.agent.task_recovery import can_resume_run
from workbench_platform.agent import routes


def fixture(tmp_path, code='provider_unavailable', status='failed'):
    runtime = object.__new__(AgentRuntime)
    runtime.store = AgentStore(tmp_path / 'agent')
    session = runtime.store.create_session('workspace', 'Original goal', ['asset'], 'session')
    run, _ = runtime.store.start_run(session['id'], 'Original user goal', 'original-goal', 1)
    runtime.store.event(session['id'], run['id'], 'original-fault', 'error', {'code': code, 'message': 'Connection temporarily failed'})
    runtime.store.status(run['id'], 1, status)
    runtime.maintenance = {}
    runtime.tasks = SimpleNamespace(get=lambda _: {'manual': False}, record_goal=Mock())
    return runtime, session, runtime.store.run(run['id'])


@pytest.mark.parametrize('code,expected', [('provider_unavailable', True), ('authentication', True),
    ('harness_exited', True), ('response_unknown', True), ('execution_conflict', False), ('runtime_failed', False)])
def test_session_and_run_api_publish_authoritative_recovery_capability(tmp_path, monkeypatch, code, expected):
    runtime, session, run = fixture(tmp_path, code)
    monkeypatch.setattr(routes, 'runtime', lambda *_: runtime)
    monkeypatch.setattr(routes, 'session_scope', lambda *_: session)
    monkeypatch.setattr(routes, 'run_scope', lambda *_: run)
    assert routes.session(session['id'], object())['runs'][0]['can_resume'] is expected
    assert routes.run(run['id'], object())['can_resume'] is expected


def test_supplement_to_recoverable_failed_run_keeps_same_goal_receipts_and_selected_inputs(tmp_path):
    runtime, session, run = fixture(tmp_path)
    async def reconcile(run_id):
        assert run_id == run['id']
        runtime.store.status(run_id, 1, 'running')
        return runtime.store.run(run_id)
    runtime.reconcile = reconcile
    result = asyncio.run(runtime.submit(session['id'], '连接修好了，继续并保留编号精度', 'supplement'))
    assert result['id'] == run['id'] and result['status'] == 'running'
    assert len(runtime.store.rows('SELECT id FROM runs')) == 1
    assert runtime.store.session(session['id'])['selected_assets'] == ['asset']
    assert runtime.store.rows('SELECT content FROM inbox WHERE run_id=?', (run['id'],))[0]['content'] == '连接修好了，继续并保留编号精度'
    assert asyncio.run(runtime.submit(session['id'], '连接修好了，继续并保留编号精度', 'supplement'))['id'] == run['id']


def test_cancelled_run_cannot_be_resumed(tmp_path):
    runtime, session, run = fixture(tmp_path, status='cancelled')
    assert can_resume_run(runtime, run) is False


def test_old_failed_run_does_not_absorb_new_input_after_a_newer_completed_run(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from workbench_platform.application_services import goal_routing
    runtime, session, old = fixture(tmp_path)
    newer, _ = runtime.store.start_run(session['id'], 'Newer completed goal', 'newer-goal', 1)
    runtime.store.status(newer['id'], 1, 'succeeded')
    local = AsyncMock(return_value={'id': 'new-local-goal'})
    monkeypatch.setattr(goal_routing, 'submit_local_goal', local)
    runtime.reconcile = Mock(side_effect=AssertionError('Do not resurrect an older failed run'))
    result = asyncio.run(runtime.submit(session['id'], 'An independent new goal', 'new-input'))
    assert result['id'] == 'new-local-goal'
    local.assert_awaited_once()
    assert runtime.store.rows('SELECT id FROM inbox WHERE run_id=?', (old['id'],)) == []

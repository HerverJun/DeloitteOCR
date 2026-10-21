"""Atomic object claims preserve session fencing and unrelated local work."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest
from workbench_platform.agent.store import AgentStore, Conflict
from workbench_platform.application_services.direct_scope import resolve_resource


@pytest.fixture
def ledger(tmp_path):
    return AgentStore(tmp_path / 'agent')


def session(ledger, identity):
    return ledger.create_session('project', identity, [], identity)


def start(ledger, session_id, request_id, driver, resource):
    return ledger.start_run(session_id, 'fixed operation', request_id, 0,
        driver=driver, driver_state={'version': 1, 'resource_object': resource, 'request': {}})[0]


def test_paused_native_object_allows_independent_data_but_rejects_same_object(ledger):
    first = start(ledger, session(ledger, 'first')['id'], 'rfirst', 'image-text-v1', 'project:A')
    ledger.status(first['id'], 1, 'paused')
    second = start(ledger, session(ledger, 'data')['id'], 'rdata', 'data-action-v1', 'newdata')
    assert second['status'] == 'running'
    with pytest.raises(Conflict):
        start(ledger, session(ledger, 'same')['id'], 'rsame', 'pdf-text-v1', 'project:A')
    assert ledger.events(first['session_id'])['items']  # Read-only history is always accessible.


def test_distinct_native_projects_can_run_but_implicit_creation_is_conservative(ledger):
    start(ledger, session(ledger, 'one')['id'], 'rone', 'image-text-v1', 'project:A')
    start(ledger, session(ledger, 'two')['id'], 'rtwo', 'pdf-text-v1', 'project:B')
    with pytest.raises(Conflict):
        start(ledger, session(ledger, 'implicit')['id'], 'rimplicit', 'image-text-v1', '*')
    with pytest.raises(Conflict):
        ledger.start_run(session(ledger, 'open')['id'], 'open analysis', 'rharness', 1)


def test_same_session_never_gains_a_second_active_driver(ledger):
    current = session(ledger, 'same')
    start(ledger, current['id'], 'rone', 'image-text-v1', 'linkA')
    with pytest.raises(Conflict):
        start(ledger, current['id'], 'rtwo', 'data-action-v1', 'independentdata')


def test_two_concurrent_submissions_claim_same_object_atomically(ledger):
    sessions = [session(ledger, name)['id'] for name in ('one', 'two')]
    def attempt(index):
        try:
            return start(ledger, sessions[index], 'rrace' + str(index), 'image-text-v1', 'same')['id']
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert sum(value is not None for value in results) == 1


def test_resolved_implicit_link_uses_the_explicit_same_object_claim():
    links = [SimpleNamespace(app_id='ocr', link_id='linkA', module_project_id='nativeA', role='editor')]
    runtime = SimpleNamespace(services=SimpleNamespace(workspace_store=SimpleNamespace(list_links=lambda workspace: links)))
    assert resolve_resource(runtime, {'workspace_id': 'project'}, 'image-text-v1', 'request') == ('project:nativeA', 'linkA')
    assert resolve_resource(runtime, {'workspace_id': 'project'}, 'pdf-text-v1', 'request', 'linkA') == ('project:nativeA', 'linkA')
    with pytest.raises(ValueError):
        resolve_resource(runtime, {'workspace_id': 'project'}, 'image-text-v1', 'request', 'other')


def test_different_workspace_links_to_same_native_project_still_conflict(ledger):
    first = session(ledger, 'one')
    second = ledger.create_session('otherproject', 'other', [], 'other')
    start(ledger, first['id'], 'rone', 'image-text-v1', 'project:sharedNative')
    with pytest.raises(Conflict):
        start(ledger, second['id'], 'rtwo', 'pdf-text-v1', 'project:sharedNative')


def test_unrelated_paused_harness_allows_new_private_data_session(ledger):
    original, _ = ledger.start_run(session(ledger, 'open')['id'], 'analysis', 'rharness', 1)
    ledger.status(original['id'], 1, 'paused')
    new = start(ledger, session(ledger, 'newdata')['id'], 'rdata', 'data-action-v1', 'new-private-data')
    assert new['status'] == 'running'

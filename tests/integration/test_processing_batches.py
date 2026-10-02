"""Real worker automation: fixed inputs/results, replay, recovery and dispositions."""
import json
from pathlib import Path
import sqlite3
import threading
import time

from fastapi.testclient import TestClient
import pytest
from packages.contracts.automation import ProcessingBatch
from workbench_platform.app import app
from workbench_platform.batches.store import encoded, fingerprint
from workbench_platform.data_api.service import DataWorkbenchService, now

LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}
PREFIX = '/api/platform/v1'
DATA_PYTHON = Path('F:/OCR/output/clear-workbench-preview-20261002/program/runtimes/data/python.exe')


@pytest.fixture
def client(tmp_path, monkeypatch):
    if not DATA_PYTHON.is_file():
        pytest.skip('Existing data worker runtime not available')
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WORKBENCH_DATA_PYTHON', str(DATA_PYTHON))
    with TestClient(app, base_url='http://127.0.0.1:8766') as value:
        yield value


def workspace(client):
    response = client.post(PREFIX + '/workspaces', headers=LOCAL,
                           json={'schema_version': '1.0', 'name': '自动对账'})
    assert response.status_code == 201, response.text
    return response.json()['workspace_id']


def upload(client, workspace_id, name, text):
    response = client.post(f'{PREFIX}/workspaces/{workspace_id}/assets/uploads', headers=LOCAL,
                           params={'name': name}, content=text.encode('utf-8'))
    assert response.status_code == 201, response.text
    value = response.json()
    return {'asset_id': value['asset_id'], 'asset_revision': value['revision'], 'blob_hash': value['blob_hash']}


def template(client, workspace_id):
    role = {'formats': ['csv'], 'columns': [{'header': '编号', 'kinds': ['text']},
            {'header': '金额', 'kinds': ['text']}], 'import_options': {},
            'steps': [{'operator': 'trim', 'parameters': {'column': '编号'}},
                      {'operator': 'cast', 'parameters': {'column': '金额', 'target': 'decimal'}}]}
    response = client.post(PREFIX + '/batch-templates', headers=LOCAL, json={'schema_version': '1.0',
        'workspace_id': workspace_id, 'name': '已确认对账', 'scenario': 'data_compare',
        'config': {'roles': {'left': role, 'right': role}, 'comparison': {
            'key_pairs': [['编号', '编号']], 'compare_pairs': [['金额', '金额']], 'amount_pairs': [['金额', '金额']],
            'amount_tolerance': '0.01', 'type_policy': 'strict', 'null_policy': 'never_match'}}})
    assert response.status_code == 201, response.text
    return response.json()


def request(workspace_id, saved, inputs, key='runA'):
    return {'schema_version': '1.0', 'request_key': key, 'workspace_id': workspace_id,
            'template_id': saved['template_id'], 'template_version': saved['version'], 'inputs': inputs}


def terminal(client, batch_id, timeout=40):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        batch = client.get(f'{PREFIX}/batches/{batch_id}').json()
        if batch['status'] not in {'queued', 'running', 'cancelling'}:
            return batch
        time.sleep(0.05)
    pytest.fail('Batch did not reach a terminal/waiting state')


def test_real_data_closure_replay_reordered_reuse_and_disposition(client):
    ws = workspace(client)
    saved = template(client, ws)
    number = '00000000000000000001'
    left = upload(client, ws, '左.csv', f'编号,金额\n{number},10.00\nB,20.00\nD,1\nD,2\n')
    right = upload(client, ws, '右.csv', f'金额,编号\n10.01,{number}\n21.00,B\n3,D\n')
    payload = request(ws, saved, [{'role': 'left', **left}, {'role': 'right', **right}])
    assert client.post(PREFIX + '/batches/preflight', headers=LOCAL, json=payload).json()['ok'] is True
    response = client.post(PREFIX + '/batches', headers=LOCAL, json=payload)
    assert response.status_code == 202, response.text
    batch = terminal(client, response.json()['batch_id'])
    assert batch['status'] == 'succeeded', batch
    ProcessingBatch.model_validate(batch)
    assert batch['artifact_state'] == 'collected'
    assert batch['summary']['coverage']['same_pairs'] == 1
    assert batch['summary']['coverage']['different_pairs'] == 1
    assert batch['summary']['pending'] > 1 and batch['summary']['review_complete'] is False
    assert any(item['category'].startswith('ambiguous') for item in batch['issues'])
    assert batch['summary']['coverage']['left_total'] == 4
    bundle = batch['artifacts'][0]
    downloaded = client.get(f"{PREFIX}/workspaces/{ws}/artifacts/{bundle['bundle_id']}/files/results.csv")
    assert downloaded.status_code == 200 and number in downloaded.content.decode('utf-8-sig')
    assert client.post(PREFIX + '/batches', headers=LOCAL, json=payload).json()['batch_id'] == batch['batch_id']
    listed = client.get(PREFIX + '/batches', params={'workspace_id': ws, 'request_key': payload['request_key']}).json()
    assert listed['total'] == 1
    assert client.get(f'{PREFIX}/workspaces/{ws}/artifacts').json()['total'] == 1
    item = batch['issues'][0]
    url = f"{PREFIX}/batches/{batch['batch_id']}/dispositions"
    confirmed = client.post(url, headers=LOCAL, json={'item_ids': [item['item_id']], 'action': 'confirm', 'note': '已核对原件'}).json()
    assert confirmed['summary']['confirmed'] == 1
    deferred = client.post(url, headers=LOCAL, json={'item_ids': [item['item_id']], 'action': 'defer'}).json()
    assert deferred['summary']['confirmed'] == 0 and deferred['summary']['deferred'] == 1
    reopened = client.post(url, headers=LOCAL, json={'item_ids': [item['item_id']], 'action': 'reopen'}).json()
    assert reopened['summary']['deferred'] == 0 and reopened['issues'][0]['raw_output'] == item['raw_output']
    assert client.post(url, headers=LOCAL, json={'item_ids': ['otherIssue'], 'action': 'confirm'}).status_code == 409
    from_session = client.post(PREFIX + '/batch-templates/from-data-session', headers=LOCAL, json={
        'schema_version': '1.0', 'workspace_id': ws, 'name': '复用已确认规则', 'session_id': batch['summary']['session_id']})
    assert from_session.status_code == 201, from_session.text
    assert from_session.json()['config']['comparison']['key_pairs'] == [['编号', '编号']]
    session = app.state.data_workbench.get(batch['summary']['session_id'])
    assert len(session['uploads']) == 2 and len(session['state']['exports']) == 1
    # Read-back persisted runner recovery must reuse all child job and export identities.
    app.state.batch_service.store.update(batch['batch_id'], status='queued')
    app.state.batch_service.schedule(batch['batch_id'])
    recovered = terminal(client, batch['batch_id'])
    assert recovered['status'] == 'succeeded', recovered
    assert recovered['artifacts'][0]['bundle_id'] == bundle['bundle_id']
    assert len(app.state.data_workbench.get(session['id'])['state']['exports']) == 1


def test_preflight_fail_closed_and_template_versions(client):
    ws = workspace(client)
    saved = template(client, ws)
    left = upload(client, ws, 'left.csv', '编号,金额\nA,1\n')
    bad = upload(client, ws, 'bad.csv', '编号,编号\nA,B\n')
    payload = request(ws, saved, [{'role': 'left', **left}, {'role': 'right', **bad}])
    checked = client.post(PREFIX + '/batches/preflight', headers=LOCAL, json=payload).json()
    assert checked['ok'] is False
    assert {'missing_column', 'duplicate_column'} <= {item['category'] for item in checked['issues']}
    posted = client.post(PREFIX + '/batches', headers=LOCAL, json=payload).json()
    batch = terminal(client, posted['batch_id'])
    assert batch['status'] == 'waiting_input' and batch['receipts'] == {}
    assert app.state.data_workbench.list() == []
    modified = {key: saved[key] for key in ('schema_version', 'workspace_id', 'template_id', 'name', 'scenario', 'config')}
    modified['name'] = '第二版本'
    version2 = client.post(PREFIX + '/batch-templates', headers=LOCAL, json=modified).json()
    assert version2['version'] == 2
    assert app.state.batch_service.store.template(ws, saved['template_id'], 1)['name'] == '已确认对账'
    conflicting = {**payload, 'template_version': 2}
    assert client.post(PREFIX + '/batches', headers=LOCAL, json=conflicting).status_code == 409


def test_cancel_waits_for_live_child_terminal_and_stops_dispatch(client, monkeypatch):
    ws = workspace(client)
    saved = template(client, ws)
    left = upload(client, ws, 'left.csv', '编号,金额\n' + ''.join(f'{i:020d},1\n' for i in range(50_000)))
    right = upload(client, ws, 'right.csv', '编号,金额\nA,1\n')
    payload = request(ws, saved, [{'role': 'left', **left}, {'role': 'right', **right}])
    # Structure is already known for this fixture; keep cancellation focused on the real subprocess.
    monkeypatch.setattr(app.state.batch_service.data, 'preflight', lambda *_: [])
    posted = client.post(PREFIX + '/batches', headers=LOCAL, json=payload).json()
    started = time.monotonic()
    while time.monotonic() - started < 10:
        batch = client.get(f"{PREFIX}/batches/{posted['batch_id']}").json()
        receipt = batch['receipts'].get('import_left')
        if receipt and receipt['status'] in {'running', 'queued'}:
            break
        time.sleep(0.01)
    else:
        pytest.fail('Real import did not start')
    assert client.post(f"{PREFIX}/batches/{posted['batch_id']}/control", headers=LOCAL,
                       json={'operation': 'cancel'}).status_code == 200
    cancelled = terminal(client, posted['batch_id'])
    assert cancelled['status'] == 'cancelled'
    assert 'import_right' not in cancelled['receipts'] and 'export' not in cancelled['receipts']
    child = app.state.data_workbench.job(receipt['session_id'], receipt['job_id'])
    assert child['status'] not in {'running', 'queued', 'cancelling'}
    assert receipt['job_id'] not in app.state.data_workbench.processes


def test_durable_worker_reply_recovers_after_response_loss(tmp_path):
    service = DataWorkbenchService(tmp_path, DATA_PYTHON)
    session = service.create('回执丢失')
    session_id, job_id = session['id'], 'jLostResponse'
    request = {'job_id': job_id, 'session_dir': str(service.session_dir(session_id)),
               'action': 'compare', 'payload': {}, 'state': session['state']}
    with closing_connection(service.db()) as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                   (job_id, session_id, 'compare', 'running', None, None, now(), now()))
        db.execute('INSERT INTO operation_requests VALUES(?,?,?,?,?,?)',
                   ('replyLost', session_id, job_id, fingerprint({}), 1, encoded(request)))
        db.commit()
    folder = service.session_dir(session_id) / 'jobs' / job_id
    folder.mkdir()
    state = {**session['state'], 'result_id': 'fixedResult'}
    (folder / 'response.json').write_text(encoded({'ok': True, 'state': state, 'result': {'id': 'fixedResult'}}), encoding='utf-8')
    service.close()
    reopened = DataWorkbenchService(tmp_path, DATA_PYTHON)
    try:
        assert reopened.operation('replyLost')['id'] == job_id
        assert reopened.operation('replyLost')['status'] == 'succeeded'
        assert reopened.get(session_id)['state']['result_id'] == 'fixedResult'
        assert reopened.get(session_id)['version'] == 2
    finally:
        reopened.close()


def test_service_restart_retries_collection_without_new_results(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WORKBENCH_DATA_PYTHON', str(DATA_PYTHON))
    with TestClient(app, base_url='http://127.0.0.1:8766') as first:
        ws = workspace(first)
        saved = template(first, ws)
        left = upload(first, ws, 'left.csv', '编号,金额\n00000000000000000001,10.00\n')
        right = upload(first, ws, 'right.csv', '编号,金额\n00000000000000000001,10.00\n')
        payload = request(ws, saved, [{'role': 'left', **left}, {'role': 'right', **right}], key='restartCollection')
        original = app.state.artifact_center.publish_data_export
        def unavailable(*args):
            raise ValueError('测试归集暂不可用')
        app.state.artifact_center.publish_data_export = unavailable
        posted = first.post(PREFIX + '/batches', headers=LOCAL, json=payload).json()
        partial = terminal(first, posted['batch_id'])
        assert partial['status'] == 'partial' and partial['artifact_state'] == 'collection_failed'
        export_id = partial['receipts']['export']['job_id']
        session_id = partial['summary']['session_id']
        app.state.artifact_center.publish_data_export = original
    with TestClient(app, base_url='http://127.0.0.1:8766') as restored:
        read_back = restored.get(f"{PREFIX}/batches/{posted['batch_id']}").json()
        assert read_back['artifact_state'] == 'collection_failed'
        assert restored.post(f"{PREFIX}/batches/{posted['batch_id']}/control", headers=LOCAL,
                             json={'operation': 'retry'}).status_code == 200
        completed = terminal(restored, posted['batch_id'])
        assert completed['status'] == 'succeeded', completed
        assert completed['receipts']['export']['job_id'] == export_id
        assert completed['summary']['pending'] == 0
        assert len(app.state.data_workbench.get(session_id)['state']['exports']) == 1
        assert restored.get(f'{PREFIX}/workspaces/{ws}/artifacts').json()['total'] == 1


from contextlib import closing as closing_connection


def test_uncertain_native_cancel_remains_unverified_and_retry_keeps_cancel(client, monkeypatch):
    from types import SimpleNamespace
    from workbench_platform.batches.native import NativeBatchError
    ws = workspace(client)
    asset = upload(client, ws, 'input.csv', '编号,金额\nA,1\n')
    saved = client.post(PREFIX + '/batch-templates', headers=LOCAL, json={
        'schema_version': '1.0', 'workspace_id': ws, 'name': '取消回执核实',
        'scenario': 'classification', 'config': {'link_id': 'linkedProject', 'model_id': 'existingModel'}}).json()
    submitted = threading.Event()
    release = threading.Event()
    attempts = []
    def run(batch, checkpoint, cancelled):
        attempts.append({'cancel_requested': batch['cancel_requested'], 'inputs': batch['inputs']})
        if len(attempts) == 1:
            checkpoint('native_submission', {'platform_job_id': 'jExistingNativeJob'})
            submitted.set()
            assert release.wait(5)
            # The native worker may still run; unavailable status cannot prove termination.
            raise NativeBatchError('原生服务不可达，取消终态需核实', 'needs_verification')
        assert cancelled(), 'Retry must continue cancellation, not resume dispatch'
        assert batch['receipts']['native_submission']['platform_job_id'] == 'jExistingNativeJob'
        return {'status': 'cancelled', 'summary': {'total': 1, 'cancelled': 1}, 'issues': [],
                'artifacts': [], 'artifact_state': 'not_generated'}
    monkeypatch.setattr(app.state.batch_service, 'native', SimpleNamespace(preflight=lambda *_: [], run=run))
    payload = request(ws, saved, [{'role': 'images', **asset}], key='cancelUnverified')
    posted = client.post(PREFIX + '/batches', headers=LOCAL, json=payload).json()
    assert submitted.wait(5)
    assert client.post(f"{PREFIX}/batches/{posted['batch_id']}/control", headers=LOCAL,
                       json={'operation': 'cancel'}).status_code == 200
    release.set()
    unknown = terminal(client, posted['batch_id'])
    assert unknown['status'] == 'needs_verification' and unknown['cancel_requested'] is True
    assert unknown['summary']['execution_complete'] is False
    assert client.post(f"{PREFIX}/batches/{posted['batch_id']}/control", headers=LOCAL,
                       json={'operation': 'retry'}).status_code == 200
    reconciled = terminal(client, posted['batch_id'])
    assert reconciled['status'] == 'cancelled' and reconciled['cancel_requested'] is True
    assert len(attempts) == 2 and attempts[1]['cancel_requested'] is True
    assert attempts[0]['inputs'] == attempts[1]['inputs']

"""Regressions for response loss, partial association and hidden old outputs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.data_api.service import DataWorkbenchService
from workbench_platform.store.workspaces import WorkspaceStore, RevisionConflict

NATIVE_ROOT = Path(__file__).resolve().parents[2].parent

LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}


def test_workspace_create_replay_concurrent_restart_and_same_name(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / 'workspaces.sqlite3')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.create_workspace('同名项目', request_id='intentA'), range(8)))
    assert len({item.workspace_id for item in results}) == 1
    reopened = WorkspaceStore(store.path)
    assert reopened.create_workspace('同名项目', request_id='intentA').workspace_id == results[0].workspace_id
    assert reopened.create_workspace('同名项目', request_id='intentB').workspace_id != results[0].workspace_id
    with pytest.raises(RevisionConflict):
        reopened.create_workspace('更改内容', request_id='intentA')
    (tmp_path / 'api').mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path / 'api'))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        payload = {'schema_version': '1.0', 'name': '项目', 'request_id': 'intentC'}
        first = client.post('/api/platform/v1/workspaces', headers=LOCAL, json=payload)
        assert first.status_code == 201
        assert client.post('/api/platform/v1/workspaces', headers=LOCAL, json=payload).json() == first.json()
        conflict = client.post('/api/platform/v1/workspaces', headers=LOCAL, json={**payload, 'name': '不同'})
        assert conflict.status_code == 409 and conflict.json()['code'] == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('app_id,class_name', [('ocr', 'OcrProjects'), ('classification', 'ClassificationProjects')])
def test_native_create_survives_lost_reply_and_partial_link(tmp_path, monkeypatch, app_id, class_name):
    if not (NATIVE_ROOT / app_id / 'src').is_dir():
        pytest.skip('Native source checkout required for cross-store replay proof')
    monkeypatch.syspath_prepend(str(NATIVE_ROOT / 'ocr/src'))
    monkeypatch.syspath_prepend(str(NATIVE_ROOT / 'classification/src'))
    from ocr_workbench.store import Store as OcrStore
    from image_platform.settings import Settings
    from image_platform.store import Store as ClassificationStore
    ledger = WorkspaceStore(tmp_path / 'platform.sqlite3')
    ws = ledger.create_workspace('项目').workspace_id
    native_root = tmp_path / app_id
    def native_store():
        return OcrStore(native_root) if app_id == 'ocr' else ClassificationStore(Settings(native_root))
    module = importlib.import_module(f'workbench_platform.jobs.{app_id}_projects')
    service_type = getattr(module, class_name)
    calls, lost = [], [True]
    def request(self, method, path, payload=None):
        if method == 'GET':
            return {'project_create_idempotency': '1.0'}
        calls.append(payload.copy())
        store = native_store()
        if app_id == 'ocr':
            item = store.project(payload['name'], payload['request_id'])
        else:
            item = store.create_project(payload['name'], '', payload['labels'], payload['request_id'])
        if lost[0]:
            lost[0] = False
            raise OSError('committed native create; response lost')
        return item
    def projects(self, workspace_id):
        store = native_store()
        rows = store.rows('SELECT id,name FROM projects')
        links = {item.module_project_id: item.link_id for item in ledger.list_links(workspace_id)}
        return {'items': [{'project_id': row['id'], 'name': row['name'], 'link_id': links.get(row['id'])} for row in rows]}
    monkeypatch.setattr(service_type, '_request', request)
    monkeypatch.setattr(service_type, 'projects', projects)
    args = (ws, '项目') + ((['甲', '乙'],) if app_id == 'classification' else ())
    def create():
        return service_type(SimpleNamespace(workspaces=ledger)).create(*args, request_id='nativeIntent')
    with pytest.raises(OSError):
        create()
    add_link = ledger.add_link
    monkeypatch.setattr(ledger, 'add_link', lambda *args, **kwargs: (_ for _ in ()).throw(RevisionConflict('link interrupted')))
    with pytest.raises(RevisionConflict):
        create()
    monkeypatch.setattr(ledger, 'add_link', add_link)
    linked = create()
    assert linked['link_id']
    assert create()['project_id'] == linked['project_id']
    assert len(native_store().rows('SELECT * FROM projects')) == 1
    assert len({item['request_id'] for item in calls}) == 1
    with pytest.raises(RevisionConflict):
        service_type(SimpleNamespace(workspaces=ledger)).create(ws, '不同内容', *((['甲', '乙'],) if app_id == 'classification' else ()), request_id='nativeIntent')


def test_data_history_pagination_reaches_101st_record_and_survives_restart(tmp_path):
    service = DataWorkbenchService(tmp_path, None)
    try:
        with closing(service.db()) as db, db:
            for index in range(101):
                key, export = f's{index:03}', f'e{index:03}'
                state = {'tables': {}, 'exports': [{'id': export, 'files': [{'name': 'results.csv', 'bytes': 1}]}]}
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?)', (key, key, 'same', 'same', 1, json.dumps(state), 'workspaceA'))
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', (export, key, 'export', 'succeeded', None, '{}', 'same', 'same'))
        sessions = [item['id'] for offset in range(0, 101, 20) for item in service.list_page('workspaceA', offset, 20)['items']]
        outputs = [item['export_id'] for offset in range(0, 101, 20) for item in service.output_page('workspaceA', offset, 20)['items']]
        assert len(set(sessions)) == len(set(outputs)) == 101
        assert sessions[-1] == 's000' and outputs[-1] == 'e000'
        assert service.output_page('workspaceB')['total'] == 0
    finally:
        service.close()
    service = DataWorkbenchService(tmp_path, None)
    try:
        assert service.list_page('workspaceA', 100, 20)['items'][0]['id'] == 's000'
        assert service.output_page('workspaceA', 100, 20)['items'][0]['export_id'] == 'e000'
    finally:
        service.close()


def test_missing_optional_forensic_config_preserves_portal_startup(tmp_path, monkeypatch):
    from workbench_platform.forensics import service as forensic_module
    (tmp_path / 'workspace').mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path / 'workspace'))
    monkeypatch.setattr(forensic_module, 'SOURCE_ROOT', tmp_path / 'missing-module')
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        result = client.post('/api/platform/v1/workspaces', headers=LOCAL, json={'schema_version': '1.0', 'name': '仍可使用'})
        assert result.status_code == 201
        ws = result.json()['workspace_id']
        assert client.get(f'/api/platform/v1/workspaces/{ws}/forensics').status_code == 503
        assert client.get('/api/platform/v1/data/health').status_code == 200

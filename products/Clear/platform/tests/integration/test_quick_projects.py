"""Lightweight project isolation and restart-safe data output discovery."""
import json
import sqlite3
import importlib
from types import SimpleNamespace
from contextlib import closing
import pytest

from fastapi.testclient import TestClient
from workbench_platform.app import app
from workbench_platform.data_api.service import DataWorkbenchService
from workbench_platform.bridge.context import ResolvedContext

LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}


def test_project_session_replay_isolation_and_output_survive_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('WORKBENCH_PORTAL_DIST', raising=False)
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        def project(name):
            return client.post('/api/platform/v1/workspaces', headers=LOCAL,
                json={'schema_version': '1.0', 'name': name}).json()['workspace_id']
        a, b = project('甲项目'), project('乙项目')
        payload = {'name': '甲项目 · 比对', 'workspace_id': a, 'request_id': 'startA'}
        first = client.post('/api/platform/v1/data/sessions', headers=LOCAL, json=payload)
        assert first.status_code == 201
        session = first.json()
        assert session['workspace_id'] == a
        assert client.post('/api/platform/v1/data/sessions', headers=LOCAL, json=payload).json()['id'] == session['id']
        assert client.post('/api/platform/v1/data/sessions', headers=LOCAL, json={**payload, 'workspace_id': b}).status_code == 409
        assert client.post('/api/platform/v1/data/sessions', headers=LOCAL, json={**payload, 'workspace_id': 'missing'}).status_code == 404
        assert len(client.get('/api/platform/v1/data/sessions', params={'workspace_id': a}).json()['items']) == 1
        assert client.get('/api/platform/v1/data/sessions', params={'workspace_id': b}).json()['items'] == []
        # This fixture checks indexing only; it does not claim a real engine run.
        state = session['state']
        state['exports'] = [{'id': 'exportA', 'result_id': 'resultA', 'files': [{'name': 'results.csv', 'bytes': 10}]}]
        with closing(app.state.data_workbench.db()) as db, db:
            db.execute('UPDATE sessions SET state=? WHERE id=?', (json.dumps(state), session['id']))
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)', ('exportA', session['id'], 'export', 'succeeded', None, '{}', '2026-10-03', '2026-10-03'))
        assert client.get('/api/platform/v1/data/outputs', params={'workspace_id': a}).json()['items'][0]['export_id'] == 'exportA'
        assert client.get('/api/platform/v1/data/outputs', params={'workspace_id': b}).json()['items'] == []
        rejected = client.post(f'/api/platform/v1/workspaces/{b}/artifacts/data-exports', headers=LOCAL,
                              json={'session_id': session['id'], 'export_id': 'exportA'})
        assert rejected.status_code == 409
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        assert client.get('/api/platform/v1/data/outputs', params={'workspace_id': a}).json()['items'][0]['session_id'] == session['id']
        assert client.post('/api/platform/v1/data/sessions', headers=LOCAL, json=payload).json()['id'] == session['id']


def test_existing_unscoped_sessions_are_preserved_when_project_column_is_added(tmp_path):
    root = tmp_path / 'legacy'
    root.mkdir()
    with sqlite3.connect(root / 'sessions.sqlite3') as db:
        db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,name TEXT,created TEXT,updated TEXT,version INTEGER,state TEXT)')
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', ('legacyA', '旧作业', 'before', 'before', 7,
            json.dumps({'tables': {}, 'recipes': [], 'comparisons': [], 'exports': [], 'result_id': None})))
    service = DataWorkbenchService(root, None)
    try:
        assert service.get('legacyA')['version'] == 7
        assert service.get('legacyA')['workspace_id'] is None
        assert service.list('projectA') == []
        assert service.list()[0]['id'] == 'legacyA'
        assert service.create('新作业', request_key='newA', workspace_id='projectA')['workspace_id'] == 'projectA'
    finally:
        service.close()


@pytest.mark.parametrize('app_id,class_name', [('ocr', 'OcrProjects'), ('classification', 'ClassificationProjects')])
def test_quick_navigation_returns_to_simple_tool_entry(tmp_path, monkeypatch, app_id, class_name):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('WORKBENCH_PORTAL_DIST', raising=False)
    module = importlib.import_module(f'workbench_platform.jobs.{app_id}_projects')
    captured = []
    monkeypatch.setattr(getattr(module, class_name), 'projects', lambda self, workspace: {'items': [{'project_id': 'nativeA'}]})
    def issue(context, **kwargs):
        captured.append(context.return_route)
        return 'http://127.0.0.1:49000/#platform_ticket=' + 'x' * 48
    monkeypatch.setattr(module, 'issue_navigation', issue)
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        workspace = client.post('/api/platform/v1/workspaces', headers=LOCAL,
            json={'schema_version': '1.0', 'name': '快速项目'}).json()['workspace_id']
        context = ResolvedContext(workspace, app_id, 'instanceA', 'linkA', 'nativeA', 'owner', '/', 'http://127.0.0.1:49000/')
        app.state.job_service = SimpleNamespace(_context=lambda *args: context,
            launches=SimpleNamespace(current=lambda app_id: SimpleNamespace(instance_id='instanceA', base_url=context.base_url, handshake_nonce='nonceA')))
        base = f'/api/platform/v1/workspaces/{workspace}/{app_id}/links/linkA'
        assert client.post(base + '/navigation', headers=LOCAL).status_code == 200
        assert client.post(base + '/quick-navigation', headers=LOCAL).status_code == 200
        assert captured == [f'/apps/{app_id}/workflow', f'/apps/{app_id}/start']
        assert client.post(base + '/quick-navigation', headers=LOCAL, json={'url': 'bad'}).status_code == 400

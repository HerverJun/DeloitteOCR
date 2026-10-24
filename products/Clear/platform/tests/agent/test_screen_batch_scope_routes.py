"""Real HTTP asset-set references must establish the durable selected scope."""
import importlib

from fastapi.testclient import TestClient

from workbench_platform.app import app


LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}


def test_set_reference_establishes_fixed_scope_and_rejects_changed_or_foreign_refs(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WORKBENCH_AGENT_ENABLED', '1')
    monkeypatch.setattr(importlib.import_module('workbench_platform.app'), 'configure_forensics',
                        lambda value: setattr(value.state, 'forensic_service', None))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        def workspace(name):
            return client.post('/api/platform/v1/workspaces', headers=LOCAL,
                json={'schema_version': '1.0', 'name': name}).json()['workspace_id']
        current, foreign = workspace('fixed screen scope'), workspace('other project')
        assets = f'/api/platform/v1/workspaces/{current}/assets'
        identifiers = [client.post(assets + f'/uploads?name=screen-{index}.png', headers=LOCAL,
            content=b'HTTP-scope-fixture-' + str(index).encode()).json()['asset_id'] for index in range(3)]
        fixed = client.post(assets + '/sets', headers=LOCAL, json={'asset_ids': identifiers}).json()
        path = f'/api/platform/v1/workspaces/{current}/agent'
        resolved = client.post(path + '/references', headers=LOCAL,
            json={'kind': 'asset_set', 'object_id': fixed['asset_set_id'], 'revision': fixed['revision']})
        assert resolved.status_code == 200, resolved.text
        reference = resolved.json()['reference']
        assert reference['sha256'] == fixed['manifest_hash']
        payload = {'name': 'one set reference', 'request_id': 'scopeSession', 'initial_refs': [reference]}
        created = client.post(path + '/sessions', headers=LOCAL, json=payload)
        assert created.status_code == 201, created.text
        assert created.json()['selected_assets'] == identifiers
        assert created.json()['initial_refs'] == [reference]
        repeated = client.post(path + '/sessions', headers=LOCAL, json=payload)
        assert repeated.status_code == 201 and repeated.json()['id'] == created.json()['id']
        changed = {**reference, 'sha256': '0' * 64}
        invalid = client.post(path + '/sessions', headers=LOCAL,
            json={**payload, 'request_id': 'changedScope', 'initial_refs': [changed]})
        assert invalid.status_code == 400
        crossed = client.post(f'/api/platform/v1/workspaces/{foreign}/agent/sessions', headers=LOCAL,
            json={**payload, 'request_id': 'foreignScope'})
        assert crossed.status_code == 400
        assert app.state.agent.store.rows('SELECT COUNT(*) total FROM sessions')[0]['total'] == 1

from fastapi.testclient import TestClient
from workbench_platform.app import app
from types import SimpleNamespace
from workbench_platform.audit.evidence import _data_fields


def test_direct_item_beyond_preview_retains_literal_and_checks_revision(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    prefix = '/api/platform/v1'
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        ws = client.post(prefix + '/workspaces', headers={'origin': 'http://127.0.0.1:8766'},
                         json={'schema_version': '1.0', 'name': '合成深页事项'}).json()['workspace_id']
        store = app.state.batch_service.store
        template = store.save_template({'schema_version': '1.0', 'workspace_id': ws,
            'name': '合成固定模板', 'scenario': 'data_compare', 'config': {}})
        batch, _ = store.accept({'schema_version': '1.0', 'workspace_id': ws,
            'request_key': 'directReadFixture', 'template_id': template['template_id'],
            'template_version': template['version'], 'inputs': []}, template)
        items = [{'item_id': 'item' + str(n), 'category': 'different', 'title': str(n),
                  'raw_output': {'identifier': '000012345678901234567890', 'amount': '1.0100'},
                  'disposition': 'deferred' if n == 44 else 'pending', 'note': '保留原说明'}
                 for n in range(45)]
        store.update(batch['batch_id'], status='succeeded', issues=items)
        revision = store.get(batch['batch_id'])['revision']
        path = f"{prefix}/batches/{batch['batch_id']}/items/item44"
        result = client.get(path, params={'batch_revision': revision})
        assert result.status_code == 200, result.text
        actual = result.json()
        assert actual['workspace_id'] == ws and actual['revision'] == revision
        assert actual['item']['item_id'] == 'item44' and actual['item']['disposition'] == 'deferred'
        assert actual['item']['raw_output'] == items[-1]['raw_output']
        assert actual['item']['note'] == '保留原说明'
        assert client.get(path, params={'batch_revision': revision + 1}).status_code == 409
        assert client.get(path.replace('item44', 'notInBatch')).status_code == 404


def test_empty_source_literal_remains_distinct_from_null():
    raw = {'ordinal': 0, 'category': 'different', 'different_columns_json': '["c1:c1"]',
           'left_row_id': 'r' + 'a' * 32, 'right_row_id': 'r' + 'b' * 32,
           'left_source_row': 2, 'right_source_row': 2,
           'L_c1_kind': 'text', 'L_c1_value': '', 'L_c1_raw': '',
           'R_c1_kind': 'null', 'R_c1_value': None, 'R_c1_raw': None}
    table = {'revision': 'v' + 'a' * 64, 'source': {'sheet_name': 'Sheet1'},
             'columns': [{'column_id': 'c1', 'header': '业务值'}]}
    page = {'rows': [raw], 'metadata': {'left': table, 'right': table,
        'policy': {'key_pairs': [['c1', 'c1']], 'compare_pairs': [], 'amount_pairs': [],
                   'type_policy': 'strict', 'null_policy': 'never_match'}}}
    service = SimpleNamespace(data=SimpleNamespace(data=SimpleNamespace(result_page_after=lambda *args: page)))
    batch = {'inputs': [{'role': side, 'asset_id': 'a' + side, 'asset_revision': 1, 'blob_hash': 'a' * 64}
                         for side in ('left', 'right')]}
    fields, _, _ = _data_fields(service, batch, {'source': {'session_id': 'session', 'result_id': 'result'},
                                               'raw_output': raw})
    left, right = fields[0]['values']
    assert (left['kind'], left['raw'], left['value']) == ('text', '', '')
    assert (right['kind'], right['raw'], right['value']) == ('null', None, None)

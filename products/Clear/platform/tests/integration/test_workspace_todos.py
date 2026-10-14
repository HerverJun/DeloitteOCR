"""Focused stored-ledger fixture; no worker, native service or model execution."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
from packages.contracts.audit import ItemEvidence, TodoLocate, TodoPage
from workbench_platform.app import app
from workbench_platform.batches.store import encoded, todo_identity

LOCAL = {'host': '127.0.0.1:8766', 'origin': 'http://127.0.0.1:8766'}
PREFIX = '/api/platform/v1'
ACTOR = {'id': 'todoFixtureUser', 'display_name': '合成待办检查', 'source': 'local'}
EVIDENCE = Path('F:/OCR/output/audit-platform-dev-20261002/evidence/D17/1')


def test_global_todos_paging_filters_location_context_and_native_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        def workspace(name):
            response = client.post(PREFIX + '/workspaces', headers=LOCAL,
                json={'schema_version': '1.0', 'name': name})
            assert response.status_code == 201, response.text
            return response.json()['workspace_id']

        ws, foreign_ws = workspace('合成全局待办'), workspace('隔离工作空间')
        asset = client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads', headers=LOCAL,
                           params={'name': '合成原件.txt'}, content=b'synthetic fixed source').json()
        member = {'asset_id': asset['asset_id'], 'asset_revision': asset['revision'], 'blob_hash': asset['blob_hash']}
        store = app.state.batch_service.store

        def seed_batch(key, scenario, issues, workspace_id=ws, status='succeeded'):
            saved = store.save_template({'schema_version': '1.0', 'workspace_id': workspace_id,
                'name': '合成模板 ' + key, 'scenario': scenario, 'config': {'link_id': 'nativeFixtureProject'}})
            batch, _ = store.accept({'schema_version': '1.0', 'request_key': key, 'workspace_id': workspace_id,
                'template_id': saved['template_id'], 'template_version': saved['version'],
                'inputs': [{'role': 'left' if scenario == 'data_compare' else 'images', **member}]}, saved)
            store.update(batch['batch_id'], status=status, issues=issues)
            return store.get(batch['batch_id'])

        initial = datetime(2026, 1, 1, tzinfo=timezone.utc)
        data_issues = [{'item_id': 'dataItem' + str(index), 'category': 'different', 'title': '合成金额差异 ' + str(index),
            'detail': '合成资料待核查', 'source': {'result_id': 'syntheticComparison'}, 'raw_output': {'ordinal': index},
            'disposition': 'confirmed' if index % 3 == 0 else 'pending',
            'updated_at': (initial + timedelta(minutes=index)).isoformat().replace('+00:00', 'Z')}
            for index in range(80)]
        data_batch = seed_batch('todosData', 'data_compare', data_issues)
        original_text = '000001234567890123456789 原识别文字\n' * 120
        adopted_text = '000001234567890123456789 采用修订 2\n' * 120
        ocr_items = [{'item_id': 'ocrLongText', 'category': 'review', 'title': '合成长原文', 'source': member,
                     'raw_output': {'native_result_id': 'resultFixture', 'native_version_id': 'versionFixture', 'revision': 0,
                                    'original_output': {'text': original_text}, 'edited_output': {'text': '固定编制前文字'}},
                     'updated_at': '2026-01-02T00:00:00Z'},
                    {'item_id': 'ocrUnknownText', 'category': 'review', 'title': '明确未知原文', 'source': member,
                     'raw_output': {'native_result_id': 'unknownResult', 'native_version_id': 'unknownVersion', 'revision': 0},
                     'updated_at': '2026-01-02T00:00:00Z'}]
        ocr_batch = seed_batch('todosOcr', 'ocr', ocr_items)
        classification = seed_batch('todosClassification', 'classification', [{'item_id': 'classLabel', 'category': 'review',
            'title': '合成分类建议', 'source': member, 'raw_output': {'prediction_label_id': '000001234567890123456789',
              'disposition_revision': 0}, 'updated_at': '2026-01-02T00:00:00Z'}])
        technical = seed_batch('todosTechnical', 'ocr', [{'item_id': 'technicalFailure', 'category': 'execution',
            'title': '识别任务失败（技术异常）', 'detail': '合成进程未成功完成', 'source': member,
            'raw_output': {'error': 'synthetic process failure'}, 'updated_at': '2099-01-01T00:00:00Z'}], status='failed')
        foreign = seed_batch('foreignTodos', 'data_compare', [{'item_id': 'foreignItem', 'category': 'different',
            'title': '其他工作空间', 'raw_output': {}, 'updated_at': '2099-01-01T00:00:00Z'}], workspace_id=foreign_ws)
        with store.db() as db, db:
            db.execute('INSERT INTO batch_item_adoptions VALUES(?,?,?,?,?,?)', (ocr_batch['batch_id'], 'ocrLongText',
                'ocrAdoptionFixture', 1, encoded({'native_revision': 2, 'operation_id': 'ocrAdoptionFixture',
                    'snapshot': {'edited': {'text': adopted_text}}}), '2026-01-03T00:00:00Z'))
            db.execute('INSERT INTO batch_item_adoptions VALUES(?,?,?,?,?,?)', (classification['batch_id'], 'classLabel',
                'classAdoptionFixture', 1, encoded({'native_revision': 3, 'operation_id': 'classAdoptionFixture',
                    'human_label_id': 'approvedClassB'}), '2026-01-03T00:00:00Z'))

        projects, procedures, findings = [], [], []
        for index, (priority, batches) in enumerate([('high', [data_batch]), ('low', [data_batch, ocr_batch])]):
            project = client.post(f'{PREFIX}/workspaces/{ws}/audit-projects', headers=LOCAL,
                json={'schema_version': '1.0', 'request_id': 'todoProject' + str(index), 'actor': ACTOR,
                      'project': {'title': '合成项目 ' + str(index), 'audit_object': '合成资料',
                          'period_start': '2026-01-01', 'period_end': '2026-09-30', 'objective': '检查人工调查优先级。'}}).json()
            project_id = project['project_id']
            base = f'{PREFIX}/audit-projects/{project_id}'
            procedure = client.post(base + '/procedures', headers=LOCAL, json={'schema_version': '1.0',
                'request_id': 'todoProcedure' + str(index), 'actor': ACTOR, 'expected_revision': project['project_revision'],
                'procedure': {'title': '合成程序 ' + str(index), 'basis': '比对', 'scope': '合成范围'},
                'batches': [{'batch_id': batch['batch_id'], 'batch_revision': batch['revision']} for batch in batches]})
            assert procedure.status_code == 201, procedure.text
            procedure_id = procedure.json()['entity_id']
            finding = client.post(base + '/findings', headers=LOCAL, json={'schema_version': '1.0',
                'request_id': 'todoFinding' + str(index), 'actor': ACTOR, 'expected_revision': procedure.json()['project_revision'],
                'procedure_id': procedure_id, 'finding': {'title': '合成人工调查 ' + str(index), 'priority': priority}, 'evidence': []})
            assert finding.status_code == 201, finding.text
            projects.append(project_id)
            procedures.append(procedure_id)
            findings.append(finding.json()['entity_id'])

        url = f'{PREFIX}/workspaces/{ws}/todos'

        def page(**query):
            response = client.get(url, params=query)
            assert response.status_code == 200, response.text
            return TodoPage.model_validate(response.json()).model_dump(mode='json')

        first = page(limit=7)
        assert first['total'] == 86 and len(first['items']) == 7
        assert first['items'][0]['kind'] == 'finding' and first['items'][0]['priority'] == 'high'
        collected, cursor = list(first['items']), first['next_cursor']
        while cursor:
            value = page(limit=7, snapshot_token=first['snapshot_token'], cursor=cursor)
            assert value['query_hash'] == first['query_hash'] and value['snapshot_token'] == first['snapshot_token']
            collected.extend(value['items'])
            cursor = value['next_cursor']
        assert len(collected) == len({item['todo_id'] for item in collected}) == 86
        assert collected[-1]['kind'] == 'finding' and collected[-1]['priority'] == 'low'
        assert all(item['priority'] == 'normal' and item['priority_basis'] == 'default_review_order'
                   for item in collected if item['kind'] == 'issue')
        assert page(sort='updated', limit=1)['items'][0]['context']['item_id'] == 'technicalFailure'
        filtered = page(issue_type='technical')
        assert filtered['total'] == 1 and filtered['items'][0]['context']['batch_id'] == technical['batch_id']
        assert not {'confirm', 'adopt'} & set(filtered['items'][0]['actions'])
        assert page(issue_type='investigation')['total'] == 2
        assert page(kind='finding', priority='high')['total'] == 1
        assert page(finding_status='draft')['total'] == 2
        assert page(scenario='ocr')['total'] == 4
        assert page(execution_state='failed')['total'] == 1
        assert page(disposition='confirmed')['total'] == 27
        assert page(disposition='pending', finding_status='draft')['total'] == 0
        assert page(project_id=projects[0])['total'] == 81
        override = page(project_id=projects[1], procedure_id=procedures[1], kind='issue', limit=100)
        assert override['total'] == 82
        assert all(item['context']['project_id'] == projects[1] and item['context']['procedure_id'] == procedures[1]
                   for item in override['items'])
        default_context = min(zip(projects, procedures))
        data_todos = [item for item in collected if item['context']['batch_id'] == data_batch['batch_id']]
        assert all((item['context']['project_id'], item['context']['procedure_id']) == default_context for item in data_todos)
        mixed = next(item for item in collected if item['context']['finding_id'] == findings[1])
        assert mixed['scenario'] is None and mixed['context']['batch_id'] is None

        target_id = todo_identity('issue', data_batch['batch_id'], 'dataItem61')
        query = {'limit': 7, 'snapshot_token': first['snapshot_token']}
        located_response = client.get(url + '/' + target_id, params=query)
        assert located_response.status_code == 200, located_response.text
        located = TodoLocate.model_validate(located_response.json()).model_dump(mode='json')
        assert located['item']['context']['item_id'] == 'dataItem61' and located['matches_filters'] is True
        target_page = page(**query, cursor=located['page_cursor'])
        assert target_id in {item['todo_id'] for item in target_page['items']}
        override_locate = client.get(url + '/' + target_id, params={'project_id': projects[1], 'procedure_id': procedures[1]}).json()
        assert override_locate['item']['context']['project_id'] == projects[1]
        excluded = client.get(url + '/' + target_id, params={'disposition': 'confirmed'}).json()
        assert excluded['matches_filters'] is False and excluded['page_cursor'] is None
        assert excluded['item']['context']['item_id'] == 'dataItem61'
        foreign_id = todo_identity('issue', foreign['batch_id'], 'foreignItem')
        assert client.get(url + '/' + foreign_id).status_code == 404
        assert client.get(url, params={'limit': 8, 'snapshot_token': first['snapshot_token'],
                                       'cursor': first['next_cursor']}).status_code == 409
        assert client.get(url, params={'limit': 0}).status_code == 400

        readable = {}
        for batch, item_id in [(ocr_batch, 'ocrLongText'), (ocr_batch, 'ocrUnknownText'),
                               (classification, 'classLabel'), (technical, 'technicalFailure')]:
            response = client.get(f"{PREFIX}/batches/{batch['batch_id']}/items/{item_id}/evidence",
                                  params={'batch_revision': batch['revision']})
            assert response.status_code == 200, response.text
            readable[item_id] = ItemEvidence.model_validate(response.json()).model_dump(mode='json')
        assert readable['ocrLongText']['fields'][0]['values'][0]['value'] == original_text
        assert readable['ocrLongText']['fields'][0]['values'][-1]['value'] == adopted_text
        assert readable['ocrLongText']['native_revision'] == 2 and readable['ocrLongText']['native_version_id'] == 'versionFixture'
        assert readable['ocrLongText']['adoption_operation_id'] == 'ocrAdoptionFixture'
        assert readable['ocrUnknownText']['fields'][0]['values'][0]['kind'] == 'unknown'
        assert readable['ocrUnknownText']['fields'][0]['values'][0]['value'] is None
        assert readable['classLabel']['fields'][0]['values'][0]['value'] == '000001234567890123456789'
        assert readable['classLabel']['fields'][0]['values'][-1]['value'] == 'approvedClassB'
        assert readable['classLabel']['native_revision'] == 3
        assert readable['technicalFailure']['issue_type'] == 'technical' and readable['technicalFailure']['fields'] == []

        changed = store.disposition(data_batch['batch_id'], {'request_id': 'todoDispositionChange', 'actor': ACTOR,
            'expected_revision': data_batch['revision'], 'item_ids': ['dataItem61'], 'action': 'confirm', 'note': '合成确认'})
        assert client.get(url, params={'limit': 7, 'snapshot_token': first['snapshot_token'],
                                       'cursor': first['next_cursor']}).status_code == 409
        assert client.get(url + '/' + target_id, params=query).status_code == 409
        refreshed = page(disposition='pending', limit=5)
        after = client.get(url + '/' + target_id, params={'disposition': 'pending', 'limit': 5,
            'snapshot_token': refreshed['snapshot_token']}).json()
        assert after['matches_filters'] is False and after['item']['state'] == 'confirmed'
        assert after['item']['batch_revision'] == changed['batch']['revision']
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / 'todo-synthetic-sample.json').write_text(json.dumps({'fixture': 'synthetic stored ledgers; no worker or model',
            'first_page': first, 'technical_filter': filtered, 'stable_location': located, 'native_evidence': readable,
            'after_confirmation': after}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

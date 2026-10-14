"""One real two-table workflow: investigation, review, immutable paper, restore."""
import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from packages.contracts.audit import AuditFinding, ItemEvidence, WorkpaperBundle
from scripts.backup_workspace import create, restore
from workbench_platform.app import app
from test_processing_batches import DATA_PYTHON, LOCAL, PREFIX, workspace, upload, template, request, terminal

ACTOR = {'id': 'auditPreparer', 'display_name': '合成样例编制人', 'source': 'local'}
REVIEWER = {'id': 'auditReviewer', 'display_name': '合成样例复核人', 'source': 'local'}


def test_actual_two_table_audit_to_workpaper_and_cold_restore(tmp_path, monkeypatch):
    demo = tmp_path / "demo-workpaper"
    if not DATA_PYTHON.is_file():
        pytest.skip('Existing data worker runtime not available')
    data_root = tmp_path / 'audit-source'
    data_root.mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(data_root))
    monkeypatch.setenv('WORKBENCH_DATA_PYTHON', str(DATA_PYTHON))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        ws = workspace(client)
        saved = template(client, ws)
        identifier = '00123456789012345678'
        left = upload(client, ws, '合成样例-账面.csv', f'编号,金额\n{identifier},1.00\nB,20.00\n')
        right = upload(client, ws, '合成样例-核对.csv', f'编号,金额\n{identifier},2.00\nB,22.00\n')
        submitted = client.post(PREFIX + '/batches', headers=LOCAL, json=request(ws, saved,
            [{'role': 'left', **left}, {'role': 'right', **right}], 'auditSyntheticBatch'))
        assert submitted.status_code == 202, submitted.text
        batch = terminal(client, submitted.json()['batch_id'])
        assert batch['status'] == 'succeeded' and batch['summary']['coverage']['different_pairs'] == 2
        issues = client.get(f"{PREFIX}/batches/{batch['batch_id']}/issues").json()['items']
        assert len(issues) == 2
        confirmed = client.post(f"{PREFIX}/batches/{batch['batch_id']}/dispositions", headers=LOCAL, json={
            'request_id': 'auditConfirmSynthetic', 'expected_revision': batch['revision'], 'actor': ACTOR,
            'item_ids': [issue['item_id'] for issue in issues], 'action': 'confirm', 'note': '合成差异已核对'}).json()
        review_response = client.post(f"{PREFIX}/batches/{batch['batch_id']}/review-artifacts", headers=LOCAL, json={
            'request_id': 'auditReviewSynthetic', 'expected_revision': confirmed['revision'], 'actor': REVIEWER, 'kind': 'final'})
        assert review_response.status_code == 201, review_response.text
        batch = review_response.json()
        review_bundle_id = next(bundle['bundle_id'] for bundle in batch['artifacts'] if bundle['kind'] == 'batch_review')
        evidence = []
        for issue in issues:
            response = client.get(f"{PREFIX}/batches/{batch['batch_id']}/items/{issue['item_id']}/evidence",
                                  params={'batch_revision': batch['revision']})
            assert response.status_code == 200, response.text
            evidence.append(ItemEvidence.model_validate(response.json()).model_dump(mode='json'))
        amount_fields = [field for item in evidence for field in item['fields'] if field['rule_kind'] == 'amount']
        assert {field['delta'] for field in amount_fields} == {'-1.00', '-2.00'}
        assert all(field['tolerance'] == '0.01' and field['within_tolerance'] is False for field in amount_fields)
        assert any(value['value'] == identifier for item in evidence for field in item['fields'] for value in field['values'])
        assert client.get(f"{PREFIX}/batches/{batch['batch_id']}/items/{issues[0]['item_id']}/evidence",
                          params={'batch_revision': batch['revision'] - 1}).status_code == 409
        project_payload = {'schema_version': '1.0', 'request_id': 'auditProjectSynthetic', 'actor': ACTOR,
            'project': {'title': '合成样例 · 应付余额核查', 'audit_object': '合成应付账款明细',
                        'period_start': '2026-01-01', 'period_end': '2026-09-30',
                        'objective': '核查账面与对方明细差异，明确差异解释和审计结论。'}}
        create_url = f'{PREFIX}/workspaces/{ws}/audit-projects'
        created = client.post(create_url, headers=LOCAL, json=project_payload)
        assert created.status_code == 201, created.text
        creation_receipt = created.json()
        project_id = creation_receipt['project_id']
        base = f'{PREFIX}/audit-projects/{project_id}'

        def project():
            return client.get(base).json()

        def command(request_id, **content):
            return {'schema_version': '1.0', 'request_id': request_id,
                    'actor': ACTOR, 'expected_revision': project()['revision'], **content}

        procedure_response = client.post(base + '/procedures', headers=LOCAL, json=command('auditProcedureSynthetic',
            procedure={'title': '核对双方应付余额', 'basis': '合成资料比对及差异调查', 'scope': '双方 2 条明细',
                       'note': '按编号精确配对，金额容差为 0.01。', 'conclusion': '两项合成差异均已找到解释。'},
            batches=[{'batch_id': batch['batch_id'], 'batch_revision': batch['revision']}]))
        assert procedure_response.status_code == 201, procedure_response.text
        procedure_id = procedure_response.json()['entity_id']
        stale = command('staleOtherWindow', project=project_payload['project'])
        finding_fields = {'title': '合成样例 · 两项跨期应付差异', 'priority': 'high', 'status': 'draft',
                          'explanation': '', 'conclusion': ''}
        finding_command = command('auditFindingSynthetic', procedure_id=procedure_id, finding=finding_fields,
            evidence=[{'batch_id': batch['batch_id'], 'item_id': issue['item_id'], 'batch_revision': batch['revision']} for issue in issues])
        response = client.post(base + '/findings', headers=LOCAL, json=finding_command)
        assert response.status_code == 201, response.text
        finding_receipt = response.json()
        finding_id = finding_receipt['entity_id']
        assert client.post(base + '/findings', headers=LOCAL, json=finding_command).json() == finding_receipt
        assert client.post(create_url, headers=LOCAL, json=project_payload).json() == creation_receipt
        assert client.post(base, headers=LOCAL, json=stale).status_code == 409
        assert client.get(base + '/requests/staleOtherWindow').status_code == 404
        assert client.post(base + '/findings', headers=LOCAL,
                           json={**finding_command, 'finding': {**finding_fields, 'title': '替换原请求'}}).status_code == 409
        finding = AuditFinding.model_validate(client.get(base + '/findings/' + finding_id).json()).model_dump(mode='json')
        assert len(finding['evidence_refs']) == 2
        captured = [client.get(base + '/evidence/' + ref['evidence_id']).json() for ref in finding['evidence_refs']]
        assert captured == evidence
        unresolved_request = command('auditUnresolvedSynthetic', kind='final', review_bundle_ids=[review_bundle_id])
        assert client.post(base + '/workpapers', headers=LOCAL, json=unresolved_request).status_code == 409
        middle_request = command('auditIntermediateSynthetic', kind='intermediate', review_bundle_ids=[review_bundle_id])
        response = client.post(base + '/workpapers', headers=LOCAL, json=middle_request)
        assert response.status_code == 201, response.text
        middle = WorkpaperBundle.model_validate(response.json()).model_dump(mode='json')
        assert middle['status'] == 'current' and middle['unresolved_count'] > 0
        artifact_base = f'{PREFIX}/workspaces/{ws}/artifacts'
        original_middle = client.get(artifact_base + '/' + middle['bundle_id'] + '/files/workpaper.json').content
        resolved = command('auditResolveSynthetic', finding={**finding_fields, 'status': 'resolved',
            'explanation': '合成样例：两项差额分别为 1.00 和 2.00，均为对方先入账的跨期款项。',
            'conclusion': '合成样例：差异合计 3.00；在后续期间处理，不构成当前期间账面错报。'},
            evidence=[{'evidence_id': ref['evidence_id']} for ref in finding['evidence_refs']])
        response = client.post(base + '/findings/' + finding_id, headers=LOCAL, json=resolved)
        assert response.status_code == 200, response.text
        assert client.get(base + '/workpapers/' + middle['workpaper_id']).json()['status'] == 'outdated'
        assert client.get(artifact_base + '/' + middle['bundle_id'] + '/files/workpaper.json').content == original_middle
        for kind, entity_id, path in [('finding', finding_id, base + '/findings/' + finding_id),
                                      ('procedure', procedure_id, base + '/procedures/' + procedure_id),
                                      ('project', project_id, base)]:
            for role in ('prepared', 'reviewed'):
                entity = client.get(path).json()
                payload = command('record_' + kind + '_' + role, entity_kind=kind, entity_id=entity_id,
                    content_revision=entity['content_revision'], project_context_revision=entity['project_context_revision'],
                    role=role, note='合成样例：已核对本版本资料、解释与结论。')
                if role == 'reviewed':
                    payload['actor'] = REVIEWER
                response = client.post(base + '/records', headers=LOCAL, json=payload)
                assert response.status_code == 201, response.text
            assert client.get(path).json()['records_current'] is True
        final_request = command('auditFinalSynthetic', kind='final', review_bundle_ids=[review_bundle_id])
        response = client.post(base + '/workpapers', headers=LOCAL, json=final_request)
        assert response.status_code == 201, response.text
        final = WorkpaperBundle.model_validate(response.json()).model_dump(mode='json')
        assert final['status'] == 'current' and final['unresolved_count'] == 0
        assert final['predecessor_workpaper_id'] == middle['workpaper_id']
        assert client.get(base + '/workpapers/' + middle['workpaper_id']).json()['successor_workpaper_ids'] == [final['workpaper_id']]
        assert client.post(base + '/workpapers', headers=LOCAL, json=final_request).json() == final
        assert client.get(base + '/requests/' + final_request['request_id']).json()['receipt'] == final
        listed = client.get(artifact_base).json()['items']
        assert len([bundle for bundle in listed if bundle['kind'] == 'audit_workpaper']) == 2
        downloaded = {}
        demo.mkdir(parents=True, exist_ok=True)
        for file in final['files']:
            response = client.get(artifact_base + '/' + final['bundle_id'] + '/files/' + file['name'])
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == file['blob_hash']
            downloaded[file['name']] = response.content
            (demo / file['name']).write_bytes(response.content)
        document = json.loads(downloaded['workpaper.json'])
        assert len(document['findings'][0]['evidence_refs']) == 2 and len(document['evidence']) == 2
        assert document['review_refs'][0]['results_xlsx']['name'] == 'results.xlsx'
        assert identifier in downloaded['workpaper.md'].decode('utf-8')
        history = client.get(base + '/history').json()
        assert history['total'] == 12 and {event['action'] for event in history['items']} >= {'create_finding', 'publish_workpaper'}
        (demo / 'README.md').write_text('这是集成检查生成的合成业务样例，非真实审计资料。\n'
            '两份 CSV、两项金额差异、一个项目/工作程序/多证据调查事项、编制与复核、正式底稿。\n', encoding='utf-8')
        (demo / 'receipt.json').write_text(json.dumps(final, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        saved_project = project()
    archive, recovered = tmp_path / 'archive', tmp_path / 'recovered'
    manifest = create(data_root, archive)
    with sqlite3.connect(data_root / 'audit' / 'audit.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    assert 'audit/audit.sqlite3' in manifest['files']
    restore(archive, recovered)
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(recovered))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        assert client.get(base).json() == saved_project
        assert client.post(create_url, headers=LOCAL, json=project_payload).json() == creation_receipt
        assert client.post(base + '/workpapers', headers=LOCAL, json=final_request).json() == final
        assert client.get(base + '/workpapers/' + final['workpaper_id']).json()['status'] == 'current'
        assert client.get(base + '/evidence/' + finding['evidence_refs'][0]['evidence_id']).json() == evidence[0]
        for name, payload in downloaded.items():
            assert client.get(artifact_base + '/' + final['bundle_id'] + '/files/' + name).content == payload

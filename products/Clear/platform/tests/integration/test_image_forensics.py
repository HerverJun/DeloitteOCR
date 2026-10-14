"""One real classic-tool workflow and hard counterexamples for audit adoption."""
import hashlib
import io
import json
from pathlib import Path
import struct
import time
import zlib
import zipfile
import re

from fastapi.testclient import TestClient
from workbench_platform.app import app
from scripts.backup_workspace import create, restore
from tests.integration.test_processing_batches import LOCAL, PREFIX, workspace

ACTOR = {'source': 'local', 'id': 'forensicReviewer', 'display_name': '合成样例审阅人'}


def png():
    def chunk(name, value):
        return struct.pack('>I', len(value)) + name + value + struct.pack('>I', zlib.crc32(name + value))
    width, height = 256, 160
    rows = []
    for y in range(height):
        rows.append(b'\0' + bytes(value for x in range(width) for value in ((35, 40, 30) if
            30 < x < 225 and any(top < y < top + 7 for top in (35, 60, 85, 110)) else (245, 245, 238))))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')


def wait(client, base, inspection_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        item = client.get(base + '/' + inspection_id).json()
        if item['status'] not in {'queued', 'running'}:
            return item
        time.sleep(.05)
    raise AssertionError('local inspection did not finish')


def test_real_inspection_review_finding_workpaper_restore_and_conflicts(tmp_path, monkeypatch):
    data = tmp_path / 'source'
    data.mkdir()
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(data))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        ws = workspace(client)
        raw = png()
        response = client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads', params={'name': '合成截图.png'}, headers=LOCAL, content=raw)
        assert response.status_code == 201, response.text
        asset = response.json()
        base = f'{PREFIX}/workspaces/{ws}/forensics'
        command = {'schema_version': '1.0', 'request_id': 'startSyntheticInspection', 'asset_id': asset['asset_id'],
                   'asset_revision': asset['revision'], 'blob_hash': asset['blob_hash'],
                   'tools': ['metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move']}
        response = client.post(base, headers=LOCAL, json=command)
        assert response.status_code == 202, response.text
        inspection_id = response.json()['inspection_id']
        assert client.post(base, headers=LOCAL, json=command).json()['inspection_id'] == inspection_id
        assert client.post(base, headers=LOCAL, json={**command, 'tools': ['metadata']}).status_code == 409
        assert client.post(base, headers=LOCAL, json={**command, 'request_id': 'unapprovedModel', 'tools': ['external_model']}).status_code == 400
        item = wait(client, base, inspection_id)
        assert item['status'] == 'succeeded', item
        assert item['report']['verdict'] is None
        assert item['report']['input']['sha256'] == hashlib.sha256(raw).hexdigest()
        assert item['report']['input']['path'] == '合成截图.png'
        assert len(item['report']['evidence']) == 6
        assert {tool['status'] for tool in item['report']['evidence'] if tool['tool'].startswith('jpeg')} == {'skipped'}
        fixed_report = client.get(base + '/' + inspection_id + '/files/report.json').content
        assert hashlib.sha256(fixed_report).hexdigest() == item['report_hash']
        downloaded = client.get(base + '/' + inspection_id + '/download')
        assert downloaded.status_code == 200 and downloaded.headers['content-type'] == 'application/zip'
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as complete:
            html = complete.read('review.html').decode('utf-8')
            assert complete.read('report.json') == fixed_report
            for image in re.findall(r'<img[^>]+src="([^"]+)"', html):
                assert image in complete.namelist()
            for record in item['files']:
                assert hashlib.sha256(complete.read(record['name'])).hexdigest() == record['sha256']
        assert client.get(base + '/' + inspection_id + '/files/not-registered.png').status_code == 404
        other = workspace(client)
        assert client.get(f'{PREFIX}/workspaces/{other}/forensics/{inspection_id}').status_code == 404
        reviewed = {'schema_version': '1.0', 'request_id': 'reviewSyntheticInspection', 'expected_revision': 1,
                    'actor': ACTOR, 'assessment': 'inconclusive', 'note': '合成流程检查：缺少可信原图，经典热图不构成真伪结论。'}
        response = client.post(base + '/' + inspection_id + '/review', headers=LOCAL, json=reviewed)
        assert response.status_code == 200, response.text
        item = response.json()
        assert item['revision'] == 2
        assert client.post(base + '/' + inspection_id + '/review', headers=LOCAL, json=reviewed).json() == item
        assert client.post(base + '/' + inspection_id + '/review', headers=LOCAL, json={**reviewed, 'request_id': 'staleReview'}).status_code == 409
        assert client.post(base + '/' + inspection_id + '/review', headers=LOCAL, json={**reviewed, 'note': '覆盖同请求'}).status_code == 409
        bundle = client.post(base + '/' + inspection_id + '/collect', headers=LOCAL).json()
        assert bundle['kind'] == 'image_forensics' and bundle['files']
        assert client.post(base + '/' + inspection_id + '/collect', headers=LOCAL).json() == bundle
        artifact_base = f'{PREFIX}/workspaces/{ws}/artifacts'
        assert client.get(artifact_base + '/' + inspection_id + '/files/report.json').content == fixed_report
        assert any(value['bundle_id'] == inspection_id for value in client.get(artifact_base + '?limit=20').json()['items'])

        project_fields = {'title': '合成样例图像取证审计', 'audit_object': '合成截图', 'period_start': '2026-01-01',
                          'period_end': '2026-09-30', 'objective': '检查图像证据及工具适用边界'}
        response = client.post(f'{PREFIX}/workspaces/{ws}/audit-projects', headers=LOCAL,
                              json={'schema_version': '1.0', 'request_id': 'imageAuditProject', 'actor': ACTOR, 'project': project_fields})
        assert response.status_code == 201, response.text
        project_id = response.json()['project_id']
        audit = PREFIX + '/audit-projects/' + project_id

        def command(request_id, **values):
            return {'schema_version': '1.0', 'request_id': request_id, 'actor': ACTOR,
                    'expected_revision': client.get(audit).json()['revision'], **values}

        response = client.post(audit + '/procedures', headers=LOCAL, json=command('imageProcedure', batches=[],
            procedure={'title': '核对图片线索', 'basis': '本地取证与人工调查', 'scope': '一张合成图片',
                       'note': '不评价识别准确率', 'conclusion': '当前证据无法判断，记录局限并执行后续核查'}))
        assert response.status_code == 201, response.text
        procedure_id = response.json()['entity_id']
        choice = {'schema_version': '1.0', 'inspection_id': inspection_id, 'inspection_revision': item['revision'], 'report_hash': item['report_hash']}
        finding_fields = {'title': '图像线索说明', 'priority': 'normal', 'status': 'resolved',
                          'explanation': '工具报告与人工审阅已固定，仅作为线索。', 'conclusion': '无法判断真伪；需结合来源核查。'}
        response = client.post(audit + '/findings', headers=LOCAL, json=command('imageFinding', procedure_id=procedure_id,
                              finding=finding_fields, evidence=[choice]))
        assert response.status_code == 201, response.text
        finding_id = response.json()['entity_id']
        finding = client.get(audit + '/findings/' + finding_id).json()
        ref = finding['evidence_refs'][0]
        snapshot = client.get(audit + '/evidence/' + ref['evidence_id']).json()
        assert snapshot['scenario'] == 'image_forensics'
        assert snapshot['inspection']['review']['note'] == reviewed['note']
        todos = client.get(f'{PREFIX}/workspaces/{ws}/todos?scenario=image_forensics&limit=20')
        assert todos.status_code == 200, todos.text
        assert todos.json()['total'] == 1 and todos.json()['items'][0]['scenario'] == 'image_forensics'
        for kind, entity_id, url in [('project', project_id, audit), ('procedure', procedure_id, audit + '/procedures/' + procedure_id),
                                     ('finding', finding_id, audit + '/findings/' + finding_id)]:
            for role in ['prepared', 'reviewed']:
                entity = client.get(url).json()
                response = client.post(audit + '/records', headers=LOCAL, json=command(kind + role,
                    entity_kind=kind, entity_id=entity_id, content_revision=entity['content_revision'],
                    project_context_revision=entity['project_context_revision'], role=role, note='合成样例检查'))
                assert response.status_code == 201, response.text
        response = client.post(audit + '/workpapers', headers=LOCAL, json=command('imageWorkpaper', kind='final', review_bundle_ids=[]))
        assert response.status_code == 201, response.text
        paper = response.json()
        markdown = client.get(artifact_base + '/' + paper['bundle_id'] + '/files/workpaper.md').text
        assert '图像取证证据' in markdown and '无法判断' in markdown and item['report_hash'] in markdown
        fixed_paper = client.get(artifact_base + '/' + paper['bundle_id'] + '/files/workpaper.json').content
        response = client.post(base + '/' + inspection_id + '/review', headers=LOCAL, json={**reviewed,
            'request_id': 'laterReview', 'expected_revision': 2, 'assessment': 'needs_investigation', 'note': '需要新增来源核查'})
        assert response.status_code == 200
        assert client.get(artifact_base + '/' + paper['bundle_id']).json()['status'] == 'outdated'
        assert client.get(audit + '/evidence/' + ref['evidence_id']).json() == snapshot
        assert client.get(artifact_base + '/' + paper['bundle_id'] + '/files/workpaper.json').content == fixed_paper
        assert client.post(audit + '/workpapers', headers=LOCAL, json=command('staleImagePaper', kind='final', review_bundle_ids=[])).status_code == 409

    archive = tmp_path / 'backup'
    restored = tmp_path / 'restored'
    create(data, archive, kind='platform')
    restore(archive, restored)
    assert (restored / 'forensics/forensics.sqlite3').is_file()
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(restored))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        assert client.get(base + '/' + inspection_id + '/files/report.json').content == fixed_report
        assert client.get(base + '/' + inspection_id).json()['revision'] == 3
        assert client.get(base + '/' + inspection_id + '/download').status_code == 200
        assert client.get(audit + '/evidence/' + ref['evidence_id']).json() == snapshot
        service = app.state.forensic_service
        with service.db() as db:
            relative = db.execute('SELECT path FROM output_locations WHERE inspection_id=?', (inspection_id,)).fetchone()[0]
        (service.root / relative / 'report.json').write_bytes(b'corrupt')
        assert client.get(base + '/' + inspection_id + '/files/report.json').status_code == 409


def test_invalid_image_is_technical_failure_and_cannot_be_adopted(tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_DATA_DIR', str(tmp_path))
    with TestClient(app, base_url='http://127.0.0.1:8766') as client:
        ws = workspace(client)
        asset = client.post(f'{PREFIX}/workspaces/{ws}/assets/uploads', params={'name': 'broken.png'}, headers=LOCAL, content=b'not an image').json()
        base = f'{PREFIX}/workspaces/{ws}/forensics'
        response = client.post(base, headers=LOCAL, json={'schema_version': '1.0', 'request_id': 'invalidImage', 'asset_id': asset['asset_id'],
            'asset_revision': asset['revision'], 'blob_hash': asset['blob_hash'], 'tools': ['metadata']})
        assert response.status_code == 202, response.text
        item = wait(client, base, response.json()['inspection_id'])
        assert item['status'] == 'failed' and item['report'] is None and item['review'] is None
        assert client.post(base + '/' + item['inspection_id'] + '/review', headers=LOCAL, json={'schema_version': '1.0', 'request_id': 'invalidReview',
            'expected_revision': 1, 'actor': ACTOR, 'assessment': 'no_supported_clue', 'note': '不得用技术失败代替判断'}).status_code == 409

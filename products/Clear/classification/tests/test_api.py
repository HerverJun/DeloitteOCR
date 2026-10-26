from io import BytesIO
import csv
import io
import json

import pytest

from PIL import Image

from image_platform.store import dump


def create_project(client):
    response = client.post("/api/v1/projects", json={"name": "测试", "labels": [{"label_id": "a", "display_name": "A"}, {"label_id": "b", "display_name": "B"}]})
    assert response.status_code == 201
    return response.json()["id"]


def test_runtime_framework_probe_is_explicit_and_separate_from_hardware(client, monkeypatch):
    from image_platform import runtime
    import shutil
    calls = []
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    devices = {'training': {'cpu': {'available': True}, 'cuda': {'available': False}},
               'inference': {'cpu': {'available': True}, 'cuda': {'available': None}}}
    def readiness(refresh=False):
        calls.append(refresh)
        return devices
    monkeypatch.setattr(runtime, 'device_readiness', readiness)
    assert 'devices' not in client.get('/api/v1/runtime').json()
    assert calls == []
    response = client.get('/api/v1/runtime?include_devices=true').json()
    assert response['gpu'] is None
    assert response['devices'] == devices
    client.get('/api/v1/runtime?refresh_devices=true')
    assert calls == [False, True]


def test_cross_site_writes_are_blocked(client):
    response = client.post("/api/v1/projects", json={}, headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    response = client.post("/api/v1/projects", json={}, headers={"X-Platform-Request": ""})
    assert response.status_code == 403


def test_duplicate_labels_are_rejected(client):
    response = client.post("/api/v1/projects", json={"name": "test", "labels": [{"label_id": "a", "display_name": "A"}, {"label_id": "a", "display_name": "B"}]})
    assert response.status_code == 422


def test_custom_port_same_origin_and_invalid_host(client):
    client.base_url = "http://127.0.0.1:8877"
    body = {"name": "自定义端口", "labels": [{"label_id": "a", "display_name": "A"}, {"label_id": "b", "display_name": "B"}]}
    assert client.post("/api/v1/projects", json=body, headers={"Origin": "http://127.0.0.1:8877"}).status_code == 201
    assert client.post("/api/v1/projects", json=body, headers={"Origin": "http://untrusted.example:8877"}).status_code == 403
    assert client.post("/api/v1/projects", json=body, headers={"Host": "untrusted.example:8877", "Origin": "http://untrusted.example:8877"}).status_code == 400


def test_upload_annotation_thumbnail_and_project_isolation(client):
    project_id = create_project(client)
    second_id = create_project(client)
    stream = BytesIO()
    Image.new("RGB", (32, 64), "red").save(stream, format="PNG")
    uploaded = client.post(f"/api/v1/projects/{project_id}/uploads", files=[("files", ("中文.png", stream.getvalue(), "image/png"))]).json()
    assert not uploaded["errors"]
    asset_id = uploaded["asset_ids"][0]
    response = client.get(f"/api/v1/assets/{asset_id}/thumbnail")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert client.post(f"/api/v1/projects/{second_id}/annotations", json={"asset_ids": [asset_id], "label_id": "a"}).status_code == 404
    assert client.post(f"/api/v1/projects/{project_id}/annotations", json={"asset_ids": [asset_id], "label_id": "a", "group_id": "document-1"}).status_code == 200
    assets = client.get(f"/api/v1/projects/{project_id}/assets").json()
    assert assets["items"][0]["label_id"] == "a"
    assert assets["items"][0]["group_id"] == "document-1"


def test_corrupt_upload_does_not_register_asset(client):
    project_id = create_project(client)
    response = client.post(f"/api/v1/projects/{project_id}/uploads", files=[("files", ("bad.png", b"not an image", "image/png"))])
    assert response.status_code == 200
    assert len(response.json()["errors"]) == 1
    assert client.get(f"/api/v1/projects/{project_id}/assets").json()["total"] == 0


def test_import_idempotency_and_invalid_directory(client, tmp_path):
    project_id = create_project(client)
    url = f"/api/v1/projects/{project_id}/imports"
    assert client.post(url, json={"path": str(tmp_path / "missing")}).status_code == 409
    first = client.post(url, json={"path": str(tmp_path)}, headers={"Idempotency-Key": "import-once"})
    second = client.post(url, json={"path": str(tmp_path)}, headers={"Idempotency-Key": "import-once"})
    assert first.json() == second.json()
    assert first.status_code == 202


@pytest.mark.parametrize('format', ['csv', 'jsonl'])
def test_review_exports_current_annotation_without_overwriting_prediction(client, store, format):
    stream = BytesIO()
    Image.new('RGB', (32, 32), 'blue').save(stream, format='PNG')
    asset_id = client.post('/api/v1/projects/p/uploads', files=[('files', ('review.png', stream.getvalue(), 'image/png'))]).json()['asset_ids'][0]
    asset = store.one('SELECT * FROM assets WHERE id=?', (asset_id,))
    job_id = store.submit('p', 'inference', {'model_id': 'model-test'}, [asset])
    original = {'label_id': 'a', 'score': 0.9, 'status': 'needs_review', 'model_version_id': 'model-test'}
    with store.connect(write=True) as con:
        con.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?", (dump(original), job_id))

    def exported():
        response = client.get(f'/api/v1/jobs/{job_id}/export?format={format}')
        assert response.status_code == 200
        if format == 'csv':
            return list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))[0]
        return json.loads(response.text)

    first = exported()
    assert not first['current_annotation_label_id']
    assert first['effective_label_id'] == 'a'
    assert client.post('/api/v1/projects/p/annotations', json={'asset_ids': [asset_id], 'label_id': 'b'}).status_code == 200
    reviewed = exported()
    assert reviewed['label_id'] == 'a'
    assert reviewed['current_annotation_label_id'] == reviewed['effective_label_id'] == 'b'
    assert reviewed['status'] == 'needs_review'
    assert json.loads(store.one('SELECT prediction FROM job_items WHERE job_id=?', (job_id,))['prediction']) == original
    assert client.post('/api/v1/projects/p/annotations', json={'asset_ids': [asset_id], 'label_id': None}).status_code == 200
    cleared = exported()
    assert not cleared['current_annotation_label_id']
    assert cleared['effective_label_id'] == 'a'

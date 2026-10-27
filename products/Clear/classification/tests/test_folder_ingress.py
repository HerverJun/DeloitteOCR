"""Explicit single-user folder ingress copies originals before normal readers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from image_platform.api import create_app
from image_platform.ingress import authorize_directory, copy_selected_input
from image_platform.paths import require_path
from image_platform.settings import Settings
from image_platform.store import Store, dump, now
from image_platform.worker import serve_worker


@pytest.fixture
def managed(tmp_path, monkeypatch):
    platform = Path(__file__).resolve().parents[2] / 'platform'
    monkeypatch.syspath_prepend(str(platform))
    roots = {'program': str(platform.parent), 'assets': str(tmp_path/'assets'),
        'runtimes': str(Path(sys.executable).parent), 'tools': str(platform.parent),
        'data': str(tmp_path/'DATA'), 'cache': str(tmp_path/'cache'), 'temp': str(tmp_path/'temp'),
        'paddle_temp': str(tmp_path/'ascii-temp'), 'secrets': str(tmp_path/'secrets'),
        'backup': str(tmp_path/'backup'), 'postgres': str(tmp_path/'postgres')}
    for name in ('assets', 'data', 'cache', 'temp', 'paddle_temp', 'secrets', 'backup', 'postgres'):
        Path(roots[name]).mkdir()
    config = tmp_path / 'clear.install.json'
    value = {'schema_version': 1, 'installation_id': str(uuid.uuid4()), 'mode': 'single', 'paths': roots,
        'resources': {}, 'network': {'transport': 'loopback-http', 'listen': '127.0.0.1',
            'public_origin': 'http://127.0.0.1:8889', 'portal_port': 8889, 'worker_port': 8890, 'postgres_port': 5432},
        'policy': {'allowed_drives': ['F:']}}
    config.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setenv('CLEAR_INSTALL_CONFIG', str(config))
    settings = Settings(Path(roots['data'])/'native/classification')
    store = Store(settings)
    with store.connect(write=True) as db:
        db.execute('INSERT INTO projects(id,name,labels,created_at) VALUES(?,?,?,?)',
            ('p', 'folder import', dump([{'label_id': 'a', 'display_name': '类别 A', 'definition': 'A'},
                                        {'label_id': 'b', 'display_name': '类别 B', 'definition': 'B'}]), now()))
    return settings, store, config, value


def source_image(root, *, name='原图.png'):
    path = root / '类别 A' / name
    path.parent.mkdir(parents=True)
    Image.new('RGB', (48, 32), (20, 100, 180)).save(path)
    return path


def client(settings):
    return TestClient(create_app(settings), base_url='http://127.0.0.1:8765', headers={'X-Platform-Request': '1'})


def test_single_mode_external_folder_real_job_copies_data_keeps_source_and_deduplicates_retry(managed, tmp_path):
    settings, store, _, _ = managed
    source = source_image(tmp_path/'outside-installation')
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    with pytest.raises(ValueError, match='registered read roots'):
        require_path(source, role='read')
    with client(settings) as session:
        posted = session.post('/api/v1/projects/p/imports', json={'path': str(source.parents[1]), 'source_id': 'batch-001'})
        assert posted.status_code == 202, posted.text
        serve_worker(settings, once=True)
        job = store.job(posted.json()['job_id'])
        assert job['state'] == 'succeeded' and job['result']['imported'] == 1
        asset = store.one('SELECT * FROM assets')
        physical = Path(asset['path'])
        assert physical == settings.data_dir/'uploads/imports'/digest/source.name
        assert physical.read_bytes() == original
        assert (asset['name'], asset['source_id'], asset['label_id'], asset['sha256']) == (source.name, 'batch-001', 'a', digest)
        provenance = store.one('SELECT * FROM folder_import_sources')
        assert provenance['source_path'] == str(source) and provenance['source_sha256'] == digest
        again = session.post('/api/v1/projects/p/imports', json={'path': str(source.parents[1]), 'source_id': 'batch-001'})
        assert again.status_code == 202
        serve_worker(settings, once=True)
        assert store.job(again.json()['job_id'])['result']['duplicates'] == 1
        assert store.one('SELECT COUNT(*) total FROM assets')['total'] == 1
        assert store.one('SELECT COUNT(*) total FROM folder_import_sources')['total'] == 1
    assert source.read_bytes() == original


def test_sensitive_folders_rejected_before_a_job_is_submitted(managed):
    settings, store, _, value = managed
    with client(settings) as session:
        for name in ('program', 'assets', 'runtimes', 'tools', 'secrets', 'backup', 'postgres'):
            response = session.post('/api/v1/projects/p/imports', json={'path': value['paths'][name]})
            assert response.status_code == 409, (name, response.text)
            assert '不能将程序' in response.json()['detail']
    assert store.one('SELECT COUNT(*) total FROM jobs')['total'] == 0


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory junction')
def test_requested_junction_is_rejected_before_resolving_its_external_target(managed, tmp_path):
    settings, store, _, _ = managed
    target = tmp_path/'outside-source'
    source_image(target)
    link = tmp_path/'selected-link'
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        f'New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null'],
        check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with client(settings) as session:
            response = session.post('/api/v1/projects/p/imports', json={'path': str(link)})
            assert response.status_code == 409
            assert '符号链接或目录联接' in response.json()['detail']
        assert store.one('SELECT COUNT(*) total FROM jobs')['total'] == 0
    finally:
        link.rmdir()


def test_multi_mode_rejects_arbitrary_server_folder_and_accepts_only_project_upload_scope(managed, tmp_path):
    settings, store, config, value = managed
    value['mode'] = 'multi'
    config.write_text(json.dumps(value), encoding='utf-8')
    external = source_image(tmp_path/'external')
    with client(settings) as session:
        denied = session.post('/api/v1/projects/p/imports', json={'path': str(external.parents[1])})
        assert denied.status_code == 409 and '上传图片' in denied.json()['detail']
        other = source_image(settings.data_dir/'uploads/other-project')
        denied = session.post('/api/v1/projects/p/imports', json={'path': str(other.parents[1])})
        assert denied.status_code == 409
        owned = source_image(settings.data_dir/'uploads/p')
        accepted = session.post('/api/v1/projects/p/imports', json={'path': str(owned.parents[1])})
        assert accepted.status_code == 202, accepted.text
        serve_worker(settings, once=True)
        assert store.job(accepted.json()['job_id'])['state'] == 'succeeded'
    assert store.one('SELECT COUNT(*) total FROM jobs')['total'] == 1


def test_source_change_during_copy_discards_staging_and_keeps_no_accepted_asset(managed, tmp_path):
    settings, store, _, _ = managed
    source = source_image(tmp_path/'changing-source')
    root, authorization = authorize_directory(source.parents[1], settings, 'p')
    job_id = store.submit('p', 'import', {'path': str(root), 'ingress': authorization})
    job = store.claim('ingress-test')
    assert job['id'] == job_id
    mutated = False
    def change_original():
        nonlocal mutated
        if not mutated:
            source.write_bytes(source.read_bytes() + b'changed while copying')
            mutated = True
    with pytest.raises(ValueError, match='变化'):
        copy_selected_input(store, job, source, root, change_original)
    assert not list((settings.data_dir/'uploads/imports/.staging').iterdir())
    assert store.one('SELECT COUNT(*) total FROM assets')['total'] == 0

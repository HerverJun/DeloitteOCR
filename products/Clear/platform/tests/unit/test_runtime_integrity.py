import hashlib
import json
import os
from pathlib import Path
import uuid

from packages.runtime import load_install
from packages.runtime.integrity import verify_content


def make_package(tmp_path):
    root = tmp_path/'package'
    (root/'app/platform').mkdir(parents=True)
    payload = root/'app/platform/module.py'
    payload.write_bytes(b'original bytes')
    (root/'manifests').mkdir()
    (root/'manifests/package.json').write_text(json.dumps({
        'format': 'clear-portable-package-v1', 'standalone': True, 'files': {
            'app/platform/module.py': {'bytes': payload.stat().st_size,
                'sha256': hashlib.sha256(payload.read_bytes()).hexdigest()}}}), encoding='utf-8')
    paths = {name: str(root/name) for name in ('assets','runtimes','tools')}
    paths.update(program=str(root), **{name: str(tmp_path/name) for name in
        ('data','cache','temp','paddle_temp','secrets','backup','postgres')})
    path = tmp_path/'clear.install.json'
    path.write_text(json.dumps({'schema_version': 1, 'installation_id': str(uuid.uuid4()),
        'mode': 'single', 'paths': paths, 'resources': {'platform_source':'app/platform'},
        'network': {'transport':'loopback-http','listen':'127.0.0.1','public_origin':'http://127.0.0.1:18889',
            'portal_port':18889,'worker_port':18890,'postgres_port':15432}}), encoding='utf-8')
    return load_install(path), payload


def test_startup_reuses_exact_file_identity_but_same_mtime_tamper_is_rehashed(tmp_path):
    config, payload = make_package(tmp_path)
    first = verify_content(config)
    assert first['ok'] and first['files_hashed'] == 1
    second = verify_content(config, use_cache=True)
    assert second['ok'] and second['files_hashed'] == 0 and second['files_cached'] == 1
    info = payload.stat()
    payload.write_bytes(b'tampered bytes')
    assert payload.stat().st_size == info.st_size
    os.utime(payload, ns=(info.st_atime_ns, info.st_mtime_ns))
    third = verify_content(config, use_cache=True)
    assert not third['ok'] and third['files_cached'] == 0
    assert 'checksum' in third['errors'][0]['error']


def test_undeclared_import_and_package_manifest_change_invalidate_cache(tmp_path):
    config, payload = make_package(tmp_path)
    assert verify_content(config)['ok']
    payload.with_name('shadow.py').write_text('unexpected source', encoding='utf-8')
    result = verify_content(config, use_cache=True)
    assert not result['ok'] and any('Undeclared' in row['error'] for row in result['errors'])
    payload.with_name('shadow.py').unlink()
    assert verify_content(config)['ok']
    manifest = config.program/'manifests/package.json'
    value = json.loads(manifest.read_text('utf-8'))
    value['blockers'] = ['intentionally unavailable required resource']
    manifest.write_text(json.dumps(value), encoding='utf-8')
    assert not verify_content(config, use_cache=True)['ok']


def test_same_program_can_reuse_verified_bytes_for_a_new_data_identity(tmp_path):
    from dataclasses import replace
    config,payload = make_package(tmp_path)
    assert verify_content(config)['ok']
    another = replace(config,installation_id=str(uuid.uuid4()))
    result = verify_content(another,use_cache=True)
    assert result['ok'] and result['files_cached'] == 1 and result['files_hashed'] == 0
    assert result['installation_id'] == another.installation_id

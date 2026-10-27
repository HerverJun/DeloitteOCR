import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from packages.runtime import load_install
from packages.runtime.integrity import verify_content
from packages.runtime import integrity


def make_package(tmp_path, package_name='package'):
    root = tmp_path/package_name
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


@pytest.mark.skipif(os.name != 'nt', reason='Windows NTFS metadata handles')
def test_windows_signature_preserves_all_legacy_fields_without_data_read_handle(tmp_path, monkeypatch):
    path = tmp_path/'中文 空格'/'source.py'
    path.parent.mkdir()
    path.write_bytes(b'unchanged source bytes')
    info = path.stat()
    expected = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        integrity._legacy_windows_change_time(path)]
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with monkeypatch.context() as context:
        def no_data_open(*args, **kwargs):
            raise AssertionError('Metadata signature must not open a data-read handle')
        context.setattr(Path, 'open', no_data_open)
        assert integrity.signature(path) == expected
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_sha


@pytest.mark.skipif(os.name != 'nt', reason='Windows NTFS metadata handles')
def test_cached_startup_does_not_open_unchanged_payload_for_data(tmp_path, monkeypatch):
    config, payload = make_package(tmp_path, '中文 空格')
    assert verify_content(config)['ok']
    original_open = Path.open
    source_sha = hashlib.sha256(payload.read_bytes()).hexdigest()
    def protected_open(path, *args, **kwargs):
        if path == payload:
            raise AssertionError('Cached startup unexpectedly opened the unchanged payload for data')
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(Path, 'open', protected_open)
        result = verify_content(config, use_cache=True)
        assert result['ok'] and result['files_cached'] == 1 and result['files_hashed'] == 0
    assert hashlib.sha256(payload.read_bytes()).hexdigest() == source_sha


@pytest.mark.skipif(os.name != 'nt', reason='Windows NTFS metadata handles')
@pytest.mark.parametrize('code', [1, 50, 87, 120])
def test_unsupported_metadata_api_falls_back_to_same_legacy_signature(tmp_path, monkeypatch, code):
    path = tmp_path/'source.py'
    path.write_bytes(b'unchanged')
    expected = integrity.signature(path)
    def unsupported(_path):
        raise integrity.ctypes.WinError(code)
    monkeypatch.setattr(integrity, '_windows_change_time', unsupported)
    assert integrity.signature(path) == expected


@pytest.mark.skipif(os.name != 'nt', reason='Windows NTFS metadata handles')
def test_access_denied_is_not_silently_reclassified_as_unsupported(tmp_path, monkeypatch):
    path = tmp_path/'source.py'
    path.write_bytes(b'unchanged')
    def denied(_path):
        raise integrity.ctypes.WinError(5)
    def no_fallback(_path):
        raise AssertionError('Access denied must remain a verification failure')
    monkeypatch.setattr(integrity, '_windows_change_time', denied)
    monkeypatch.setattr(integrity, '_legacy_windows_change_time', no_fallback)
    with pytest.raises(OSError):
        integrity.signature(path)


def test_same_size_replacement_and_deletion_cannot_reuse_prior_cache(tmp_path):
    config, payload = make_package(tmp_path)
    assert verify_content(config)['ok']
    original = payload.stat()
    manifest = config.program/'manifests/package.json'
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    replacement = payload.with_name('replacement.tmp')
    replacement.write_bytes(b'tampered bytes')
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, payload)
    assert payload.stat().st_size == original.st_size
    result = verify_content(config, use_cache=True)
    assert not result['ok'] and result['files_cached'] == 0
    assert any('checksum' in error['error'] for error in result['errors'])
    assert payload.read_bytes() == b'tampered bytes'
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == manifest_sha
    payload.unlink()
    result = verify_content(config, use_cache=True)
    assert not result['ok'] and result['files_cached'] == 0


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory junction')
def test_metadata_handle_rejects_a_junction_ancestor(tmp_path):
    import subprocess
    target = tmp_path/'actual'
    target.mkdir()
    (target/'source.py').write_bytes(b'outside namespace')
    junction = tmp_path/'alias'
    subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(target)],
        check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='reparse target'):
            integrity.signature(junction/'source.py')
    finally:
        junction.rmdir()

"""Portable packaging must bind contents without binding the developer's paths."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/package_portable.py'
spec = importlib.util.spec_from_file_location('clear_test_portable_packager', SCRIPT)
packager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = packager
spec.loader.exec_module(packager)


@pytest.fixture
def vendor_patch(tmp_path, monkeypatch):
    original = ("# Original Apache-2.0 license retained\r\nimport os\r\n"
        "DATA_HOME = os.path.join(os.path.expanduser('~'), 'dataset')\r\n"
        "os.makedirs(DATA_HOME, exist_ok=True)\r\n").encode()
    patched = ("# Original Apache-2.0 license retained\r\nimport os\r\n"
        + packager.PADDLE_CACHE_GUARD.replace('\n', '\r\n')
        + "DATA_HOME = os.path.join(_CLEAR_PADDLE_CACHE, 'dataset')\r\n"
        + "os.makedirs(DATA_HOME, exist_ok=True)\r\n").encode()
    rule = {'upstream_sha256': hashlib.sha256(original).hexdigest(),
        'patched_sha256': hashlib.sha256(patched).hexdigest(),
        'replacements': [("DATA_HOME = os.path.join(os.path.expanduser('~'), 'dataset')",
                          "DATA_HOME = os.path.join(_CLEAR_PADDLE_CACHE, 'dataset')")]}
    monkeypatch.setattr(packager, 'PADDLE_CACHE_PATCHES', {'dataset/common.py': rule})
    output = tmp_path/'Clear'
    files = []
    for route in ('ppocr', 'paddlevl'):
        path = output/f'assets/program/bundles/ocr/runtimes/{route}/Lib/site-packages/paddle/dataset/common.py'
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
        files.append(path)
    return output, files, original, patched, rule


def test_vendor_cache_patch_is_pinned_idempotent_and_seals_transformed_bytes(vendor_patch, monkeypatch, tmp_path):
    output, paths, original, patched, rule = vendor_patch
    for path in paths:
        info = path.stat()
        packager._HASH_CACHE[(str(path), info.st_size, info.st_mtime_ns)] = {
            'bytes': len(original), 'sha256': rule['upstream_sha256']}
    records = packager.portable_vendor_compatibility(output)
    assert all(path.read_bytes() == patched for path in paths)
    assert packager.portable_vendor_compatibility(output) == records
    assert {row['engine_id'] for row in records} == {'ppocr', 'paddlevl'}
    assert all(row['resource_id'] == 'program_assets' for row in records)
    assert str(tmp_path) not in json.dumps(records)
    manifest = packager.seal(output)
    for path in paths:
        assert manifest['files'][path.relative_to(output).as_posix()] == {
            'bytes': len(patched), 'sha256': rule['patched_sha256']}
    monkeypatch.delenv('PADDLE_HOME', raising=False)
    with pytest.raises(RuntimeError, match='absolute managed PADDLE_HOME'):
        exec(compile(patched, str(paths[0]), 'exec'), {})
    monkeypatch.setenv('PADDLE_HOME', str(tmp_path/'managed-cache'))
    context = {}
    exec(compile(patched, str(paths[0]), 'exec'), context)
    assert Path(context['DATA_HOME']) == tmp_path/'managed-cache/dataset'
    # A same-size changed vendor source is not silently accepted as a new pin.
    paths[-1].write_bytes(patched.replace(b'Apache', b'Xxxxxx', 1))
    with pytest.raises(ValueError, match='Unsupported or corrupted'):
        packager.portable_vendor_compatibility(output)


def test_vendor_patch_checks_all_inputs_before_first_write(vendor_patch):
    output, paths, original, _patched, _rule = vendor_patch
    paths[-1].write_bytes(b'unknown vendor version')
    with pytest.raises(ValueError, match='Unsupported or corrupted'):
        packager.portable_vendor_compatibility(output)
    assert paths[0].read_bytes() == original
    assert not (output/'manifests/vendor-compatibility.json').exists()


def test_vendor_component_refresh_preserves_other_expected_asset_hashes():
    prior = {'files': {'runtime/cache.py': {'bytes': 4, 'sha256': 'old'},
                       'runtime/model.dat': {'bytes': 500, 'sha256': 'frozen-model'}}}
    planned = [packager.Member(Path('unused'), name, 'runtime') for name in prior['files']]
    result = packager.vendor_component_identities(prior, planned, [
        {'member': 'runtime/cache.py', 'patched_bytes': 8, 'patched_sha256': 'patched'}])
    assert result['runtime']['runtime/model.dat'] == prior['files']['runtime/model.dat']
    assert result['runtime']['runtime/cache.py'] == {'bytes': 8, 'sha256': 'patched'}
    assert prior['files']['runtime/cache.py']['sha256'] == 'old'


@pytest.mark.parametrize('name', ['../escape', 'C:/data', '/root', 'a\\b', 'a//b', 'a/./b'])
def test_reject_package_member_escape(name):
    with pytest.raises(ValueError, match='Invalid package member'):
        packager.relative_member(name)


def test_embedded_python_imports_remain_relative_after_relocation(tmp_path):
    source = tmp_path / 'old/runtime'
    source.mkdir(parents=True)
    shared = tmp_path / 'old/site'
    shared.mkdir()
    path = source / 'python312._pth'
    path.write_text('python312.zip\n.\n' + str(shared) + '\nimport site\n', 'utf-8')
    output = tmp_path / '成品 中文 空格/Clear'
    member = packager.Member(path, 'runtimes/special/python312._pth', 'test')
    content = packager.rebase_pth(member, output, [(shared, 'runtimes/shared')]).decode()
    lines = content.splitlines()
    assert str(tmp_path) not in content
    assert lines[:2] == ['python312.zip', '.']
    assert lines[-1] == 'import site'
    relocated = tmp_path / '移动目录/Clear'
    resolved = [(relocated / member.destination).parent / line for line in lines[2:-1]]
    assert {path.resolve() for path in resolved} == {
        (relocated / 'runtimes/shared').resolve(),
        (relocated / 'app/platform').resolve(),
        (relocated / 'app/platform/src').resolve(),
    }


def test_unknown_embedded_import_is_a_build_failure(tmp_path):
    path = tmp_path / 'python312._pth'
    path.write_text('python312.zip\n.\nF:/unregistered-site\nimport site\n', 'utf-8')
    with pytest.raises(ValueError, match='outside registered inputs'):
        packager.rebase_pth(packager.Member(path, 'runtimes/python312._pth', 'test'), tmp_path, [])


def test_manifest_has_relative_identities_and_stays_unqualified(tmp_path):
    output = tmp_path / 'Clear'
    output.mkdir()
    payload = output / 'app/code.py'
    payload.parent.mkdir()
    payload.write_text('print("offline")\n', 'utf-8')
    value = packager.seal(output)
    assert value['standalone'] is True
    assert value['status'] == 'unqualified'
    assert value['qualification']['independent_windows'] == 'pending'
    assert value['files'] == {'app/code.py': packager.identity(payload)}
    assert 'manifests/package.json' not in value['files']
    saved = json.loads((output / 'manifests/package.json').read_text('utf-8'))
    assert saved == value
    payload.write_text('changed', 'utf-8')
    assert packager.identity(payload) != value['files']['app/code.py']


def test_network_and_non_f_build_paths_are_rejected():
    for path in ('C:/outside', '//server/share/package'):
        with pytest.raises(ValueError, match='local F'):
            packager.physical(path)


def test_download_rejects_unpinned_source_before_network(tmp_path):
    with pytest.raises(ValueError, match='fixed SHA256'):
        packager.fetch_missing({'downloads': [{'target': str(tmp_path / 'asset'),
            'url': 'https://example.com/latest', 'bytes': 1}]}, tmp_path)


def test_existing_resource_must_match_historical_content_pin(tmp_path):
    path = tmp_path / 'weight.dat'
    path.write_bytes(b'wrong model')
    member = packager.Member(path, 'assets/weight.dat', 'model')
    with pytest.raises(ValueError, match='Historical content pin mismatch'):
        packager.input_lock([member], pins={path: {'bytes': 11, 'sha256': '0' * 64}})


def test_streaming_copy_checks_source_pin_and_preserves_expected_output_hash(tmp_path):
    source = tmp_path / 'source/model.dat'
    source.parent.mkdir()
    source.write_bytes(b'fixed-weight')
    output = tmp_path / 'Clear'
    target = output / 'assets/model.dat'
    member = packager.Member(source, 'assets/model.dat', 'model')
    expected = packager.identity(source)
    row = packager.stream_member(member, target, {source: expected}, output, [])
    assert row['sha256'] == expected['sha256']
    assert packager.digest(target) == expected['sha256']
    manifest = packager.seal(output)
    assert manifest['files']['assets/model.dat'] == expected
    with pytest.raises(ValueError, match='Historical content pin mismatch'):
        packager.stream_member(member, target, {source: {'sha256': '0' * 64}}, output, [])


def test_mineru_binary_manifest_has_no_host_paths(tmp_path):
    output = tmp_path / 'Clear'
    runtime = output / 'runtimes/mineru-pro'
    shared = output / 'runtimes/shared'
    runtime.mkdir(parents=True)
    shared.mkdir()
    (runtime / 'python312._pth').write_text('python312.zip\n.\n../shared\nimport site\n', 'utf-8')
    (runtime / 'python.exe').write_bytes(b'embedded')
    (shared / 'dependency.pyd').write_bytes(b'pinned')
    (shared / 'module.py').write_text('pass', 'utf-8')
    packager.portable_mineru(output)
    record = json.loads((output / 'manifests/mineru-runtime.json').read_text('utf-8'))
    assert record['schema_version'] == 2
    assert record['root_base'] == 'manifest'
    assert {row['root'] for row in record['sources']} == {'../runtimes/mineru-pro', '../runtimes/shared'}
    assert sum(len(row['files']) for row in record['sources']) == 2
    assert str(tmp_path) not in json.dumps(record)


def test_pdf_table_relocation_only_rewrites_known_path_fields(tmp_path):
    source = tmp_path / 'source'
    config = source / 'ocr/config/pdf-table-tool.json'
    config.parent.mkdir(parents=True)
    prefix = 'build/public-quality-20260916/tool/site-packages'
    config.write_text(json.dumps({'source': prefix,
        'files': {prefix + '/pdfplumber/pdf.py': 'fixed-hash'},
        'settings_policy': prefix}), 'utf-8')
    output = tmp_path / 'Clear'
    packager.portable_pdf_tables(output, {'source': str(source)})
    value = json.loads((output / 'app/ocr/config/pdf-table-tool.json').read_text('utf-8'))
    assert value['source'] == 'resources/pdf-tables/site-packages'
    assert value['files'] == {'resources/pdf-tables/site-packages/pdfplumber/pdf.py': 'fixed-hash'}
    assert value['settings_policy'] == prefix


def test_archive_pins_exclude_non_server_postgres_apps(tmp_path):
    archive = tmp_path / 'postgres.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('pgsql/bin/postgres.exe', b'pinned-server')
        bundle.writestr('pgsql/pgAdmin 4/python/python313._pth', b'another-runtime')
        bundle.writestr('pgsql/StackBuilder/download.exe', b'unused')
    root = tmp_path / 'pgsql'
    pins = packager.archive_members(archive, root, prefix='pgsql/', excluded_roots={'pgAdmin 4', 'StackBuilder'})
    assert set(pins) == {root / 'bin/postgres.exe'}
    assert pins[root / 'bin/postgres.exe']['bytes'] == len(b'pinned-server')


def test_resume_preserves_expected_hash_for_later_independent_readback(tmp_path):
    source = tmp_path / 'model.dat'
    source.write_bytes(b'good')
    target = tmp_path / 'Clear/model.dat'
    target.parent.mkdir()
    target.write_bytes(b'bad!')
    member = packager.Member(source, 'model.dat', 'model')
    expected = packager.identity(source)
    prior = {'source': str(source), 'source_mtime_ns': source.stat().st_mtime_ns,
        'destination': 'model.dat', 'component': 'model', **expected}
    row = packager.resume_member(member, target, prior, {source: expected}, source.stat(), target.stat())
    assert row['sha256'] == expected['sha256']
    assert packager.seal(target.parent)['files']['model.dat']['sha256'] == expected['sha256']
    assert packager.digest(target) != expected['sha256']
    with pytest.raises(ValueError, match='Checkpoint content differs'):
        packager.resume_member(member, target, prior, {source: {'sha256': '0' * 64}}, source.stat(), target.stat())

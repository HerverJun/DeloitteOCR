"""Exercise native policy boundaries and real Windows aliases without a GPU."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch

import pytest

from ocr_workbench.adapter import WORKER_BOOTSTRAP, configured_worker_source
from ocr_workbench.offline import configure
from ocr_workbench.store import Store
from ocr_workbench.windows_paths import ascii_model_directory


@pytest.fixture
def managed(tmp_path, monkeypatch):
    # Tests only use real F paths. Other-drive acceptance belongs on a separate
    # target machine; claiming that from a string-only test would be misleading.
    platform = Path(__file__).resolve().parents[2] / 'platform'
    monkeypatch.syspath_prepend(str(platform))
    root = tmp_path / '中文 安装数据'
    assets = root / '模型'
    (assets / 'ocr-models').mkdir(parents=True)
    paths = {
        'program': str(platform.parent), 'assets': str(assets),
        'runtimes': str(Path(sys.executable).parent), 'tools': str(platform.parent),
        'data': str(root / 'DATA'), 'cache': str(root / 'cache'),
        'temp': str(root / 'temp'), 'paddle_temp': str(tmp_path / 'ascii-temp'),
        'secrets': str(root / 'secrets'), 'backup': str(root / 'backup'),
    }
    config = tmp_path / 'clear.install.json'
    config.write_text(json.dumps({'schema_version': 1, 'installation_id': str(uuid.uuid4()),
        'paths': paths, 'mode': 'single', 'network': {'transport': 'loopback-http',
        'listen': '127.0.0.1', 'public_origin': 'http://127.0.0.1:8889',
        'portal_port': 8889, 'worker_port': 8890, 'postgres_port': 5432},
        'resources': {'platform_source': str(platform), 'platform_python': sys.executable,
                      'ocr_models': str(assets / 'ocr-models'),
                      'pdf_table_config': str(assets / 'tool/config/pdf-table-tool.json'),
                      'pdf_tables': str(assets / 'tool/resources/pdf-tables/site-packages')},
        'policy': {'allowed_drives': ['F:']}}), encoding='utf-8')
    monkeypatch.setenv('CLEAR_INSTALL_CONFIG', str(config))
    monkeypatch.setenv('CLEAR_RUNTIME_PACKAGE_ROOT', str(platform))
    monkeypatch.setenv('CLEAR_PADDLE_TEMP', paths['paddle_temp'])
    return {name: Path(path) for name, path in paths.items()}


def test_native_store_and_offline_cache_use_registered_data_and_reject_unregistered_writes(managed, tmp_path):
    store = Store(managed['data'] / 'ocr')
    with patch.dict(os.environ):
        configure(store.root / 'session')
        assert (store.root / 'workbench.sqlite3').is_file()
        assert Path(os.environ['HF_HOME']).is_relative_to(store.root)
    with pytest.raises(ValueError, match='registered write roots'):
        Store(tmp_path / 'unregistered')
    assert not (tmp_path / 'unregistered').exists()
    with pytest.raises(ValueError, match='registered write roots'):
        configure(managed['program'] / 'must-not-write-program')
    assert not (managed['program'] / 'must-not-write-program').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows Paddle junction')
def test_unicode_models_use_only_registered_transient_mapping_and_keep_original_bytes(managed):
    models = managed['assets'] / 'ocr-models'
    weights = models / 'weights.bin'
    weights.write_bytes(b'pinned model identity')
    before = hashlib.sha256(weights.read_bytes()).hexdigest()
    with ascii_model_directory(models) as alias:
        assert str(alias).isascii()
        assert alias.resolve() == models.resolve()
        assert hashlib.sha256((alias / weights.name).read_bytes()).hexdigest() == before
    assert not alias.exists()
    assert hashlib.sha256(weights.read_bytes()).hexdigest() == before
    unregistered = managed['assets'] / 'another-model'
    unregistered.mkdir()
    with pytest.raises(ValueError, match='registered'):
        with ascii_model_directory(unregistered):
            pytest.fail('an unregistered Paddle model root was accepted')


def test_isolated_managed_worker_fails_before_importing_old_runtime_without_package_root(managed):
    environment = dict(os.environ)
    environment.pop('CLEAR_RUNTIME_PACKAGE_ROOT', None)
    source = Path(__file__).resolve().parents[1] / 'src'
    child = subprocess.run([sys.executable, '-B', '-I', '-c', WORKER_BOOTSTRAP, str(source)],
        env=environment, capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert child.returncode != 0
    assert 'missing the shared runtime package root' in child.stderr


def test_source_override_requires_hash_and_cannot_point_outside_installation(managed, tmp_path, monkeypatch):
    source = tmp_path / 'old-development-source'
    source.mkdir()
    binding = managed['data'] / 'binding.json'
    binding.parent.mkdir(parents=True)
    binding.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('CLEAR_OCR_WORKER_SOURCE', str(source))
    monkeypatch.setenv('CLEAR_OCR_WORKER_SOURCE_MANIFEST', str(binding))
    monkeypatch.setenv('CLEAR_OCR_WORKER_BINDING_SHA256', hashlib.sha256(binding.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match='registered read roots'):
        configured_worker_source()


def test_relative_mineru_runtime_manifest_retains_pinned_binary_hash_checks(managed, monkeypatch):
    from ocr_workbench.runtime_audit import audit_module_paths
    manifests = managed['assets'] / 'manifests'
    manifests.mkdir()
    library = managed['assets'] / 'runtime/test.dll'
    library.parent.mkdir()
    library.write_bytes(b'pinned runtime bytes')
    record = {'schema_version': 2, 'root_base': 'manifest', 'sources': [{'root': '../runtime',
        'files': {'test.dll': {'bytes': library.stat().st_size,
                               'sha256': hashlib.sha256(library.read_bytes()).hexdigest()}}}]}
    manifest = manifests / 'mineru.json'
    manifest.write_text(json.dumps(record), encoding='utf-8')
    monkeypatch.setenv('CLEAR_MINERU_PRO_RUNTIME_MANIFEST', str(manifest))
    monkeypatch.setenv('CLEAR_MINERU_PRO_RUNTIME_MANIFEST_SHA256', hashlib.sha256(manifest.read_bytes()).hexdigest())
    result = audit_module_paths(managed['program'] / 'ocr', [library], managed['data'] / 'Windows', managed['data'] / 'Defender')
    assert result['verified_runtime_source_modules'][0]['path'] == str(library)
    library.write_bytes(b'x' * library.stat().st_size)
    with pytest.raises(RuntimeError, match='integrity changed'):
        audit_module_paths(managed['program'] / 'ocr', [library], managed['data'] / 'Windows', managed['data'] / 'Defender')


def test_managed_pdf_table_config_uses_stable_resource_root_without_historical_build_fallback(managed):
    from ocr_workbench.pdf_tables import tool_config
    from ocr_workbench.table_tool import tool_identity
    base = managed['assets'] / 'tool'
    path = base / 'config/pdf-table-tool.json'
    path.parent.mkdir(parents=True)
    tool = base / 'resources/pdf-tables/site-packages'
    tool.mkdir(parents=True)
    value = {'source': 'resources/pdf-tables/site-packages', 'files': {},
             'version': 'test-locked', 'provider': 'pdfplumber'}
    path.write_text(json.dumps(value), encoding='utf-8')
    assert tool_config() == (path, base, value)
    assert tool_identity()['config_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    value['source'] = 'build/public-quality-20260916/tool/site-packages'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='registered portable resource'):
        tool_config()

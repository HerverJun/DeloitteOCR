"""Native launch bindings reject stale source, asset and frontend identities."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import os
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/portable_native.py'
spec = importlib.util.spec_from_file_location('portable_native', SCRIPT)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


@pytest.fixture
def binding(tmp_path):
    platform = tmp_path / 'program/platform'
    source = tmp_path / 'program/classification'
    assets = tmp_path / 'assets/program'
    data = tmp_path / 'DATA'
    package = source / 'src/image_platform'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('# same current source\n', encoding='utf-8')
    asset = assets / 'bundles/classification'
    asset.mkdir(parents=True)
    (asset / 'bundle.manifest.json').write_text('{"files": []}', encoding='utf-8')
    config = SimpleNamespace(installation_id='test-installation', data=data,
        resource=lambda key: {'platform_source': platform, 'classification_source': source, 'program_assets': assets}[key])
    value = {'app_id': 'classification', 'installation_id': config.installation_id,
        'platform': str(platform), 'source': str(source), 'asset': str(asset), 'data_root': str(data),
        'worker_source': str(source / 'src'), 'web_root': str(source / 'apps/web/dist'),
        'content_manifest_sha256': native.sha(asset / 'bundle.manifest.json'),
        'worker_source_files': [{'path': 'image_platform/__init__.py', 'sha256': native.sha(package / '__init__.py')}]}
    return config, value, package


def test_generated_native_entrypoints_compile_and_matching_sources_are_accepted(binding):
    config, value, package = binding
    compile(native.SERVER, 'portable-native-server', 'exec')
    compile(native.WORKER, 'portable-native-worker', 'exec')
    assert native.verify_binding(value, config) == (package.parent, Path(value['asset']))


def test_native_binding_cannot_hide_extra_worker_modules_or_stale_asset_manifest(binding):
    config, value, package = binding
    extra = package / 'unregistered.py'
    extra.write_text('# unregistered module\n', encoding='utf-8')
    with pytest.raises(ValueError, match='undeclared'):
        native.verify_binding(value, config)
    extra.unlink()
    (Path(value['asset']) / 'bundle.manifest.json').write_text('{"files": [1]}', encoding='utf-8')
    with pytest.raises(ValueError, match='manifest identity'):
        native.verify_binding(value, config)


def test_native_binding_cannot_select_a_stale_frontend_or_escaped_source_member(binding):
    config, value, package = binding
    value['web_root'] = str(package.parent / 'old-frontend')
    with pytest.raises(ValueError, match='registered package resources'):
        native.verify_binding(value, config)
    value['web_root'] = str(package.parents[1] / 'apps/web/dist')
    value['worker_source_files'][0]['path'] = '../escaped.py'
    with pytest.raises(ValueError):
        native.verify_binding(value, config)


@pytest.mark.skipif(os.name != 'nt', reason='Windows cmd Unicode launch contract')
def test_real_native_batch_uses_unicode_percent_and_bang_runtime_paths(tmp_path):
    runtime = tmp_path/'运行库 %CLEAR_TEST_EXPANSION% ! with spaces'
    runtime.mkdir()
    original = Path(sys.executable).parent
    for source in [original/'python.exe', original/'python312.zip', *original.glob('python*.dll'), *original.glob('vcruntime*.dll')]:
        shutil.copy2(source,runtime/source.name)
    (runtime/'python312._pth').write_text('python312.zip\n.\nimport site\n',encoding='ascii')
    launch = tmp_path/'launch'
    launch.mkdir()
    native.write_launcher(launch/'launch.cmd','ocr')
    (launch/'server.py').write_text("from pathlib import Path;Path(__file__).with_name('ready.txt').write_text('started',encoding='ascii')",encoding='ascii')
    environment = {**os.environ,'CLEAR_OCR_CONTROL_PYTHON':str(runtime/'python.exe'),
                   'CLEAR_TEST_EXPANSION':'SHOULD-NOT-EXPAND','PYTHONDONTWRITEBYTECODE':'1'}
    result = subprocess.run(['cmd.exe','/d','/v:on','/c',str(launch/'launch.cmd')],cwd=launch,
        env=environment,capture_output=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert (launch/'ready.txt').read_text('ascii') == 'started'

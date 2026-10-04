"""Physical mode must reject links and corrupted preserved native payloads."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts.development_native import _sha
from scripts.physical_development import assert_physical_tree, configure


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def fixture(tmp_path):
    source, program, data = tmp_path / 'worktrees/platform', tmp_path / 'program', tmp_path / 'data'
    project = Path(__file__).resolve().parents[2]
    for app in ('ocr', 'classification'):
        native = source.parent / app
        (native / 'src').mkdir(parents=True)
        (native / 'frontend/dist').mkdir(parents=True)
        (native / 'frontend/dist/index.html').write_text('current UI', encoding='utf-8')
        config = source / f'config/apps/{app}.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / f'config/apps/{app}.json', config)
        bundle = program / 'bundles' / app
        files = {'runtimes/control/python.exe': b'python', 'runtimes/control/python312._pth': b'old path',
                 'model.bin': b'sealed model'}
        if app == 'classification':
            files['src/image_platform/platform_weights.py'] = b'def install_weights(*args): pass'
        for name, content in files.items():
            path = bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        write_json(program / f'provenance/{app}-bundle.manifest.json', {'app_id': app,
            'files': [{'path': name, 'sha256': _sha(bundle / name)} for name in files]})
    for name in ('platform', 'data', 'forensics'):
        (program / 'runtimes' / name).mkdir(parents=True)
    write_json(program / 'dependency-manifest.json', {'files': {}})
    write_json(program / 'copy-verification.json', {'program_root': str(program), 'directory_links': 0,
        'dependency_manifest_sha256': _sha(program / 'dependency-manifest.json')})
    data.mkdir()
    return source, program, data


def test_physical_registration_stays_in_development_program_and_restarts(tmp_path, monkeypatch):
    source, program, data = fixture(tmp_path)
    monkeypatch.setenv('CLEAR_PADDLE_TEMP', str(tmp_path / 'temp'))
    for _ in range(2):
        registry = configure(source, program, data)
        assert not registry.failures
        for app, installation in registry.installations.items():
            assert installation.installed
            assert installation.executable == program / 'bundles' / app / 'launch.cmd'
            binding = json.loads((installation.working_directory / 'native-binding.json').read_text())
            assert Path(binding['asset_bundle']) == installation.working_directory
            pth = installation.working_directory / 'runtimes/control/python312._pth'
            assert str(source.parent / app / 'src') in pth.read_text()
            assert registry.startup_entry_hashes(app, installation, snapshot_revision=registry.snapshot_revision)
    assert (program / 'bundles/ocr/web/index.html').read_text() == 'current UI'


def test_corrupted_preserved_model_prevents_registration(tmp_path, monkeypatch):
    source, program, data = fixture(tmp_path)
    monkeypatch.setenv('CLEAR_PADDLE_TEMP', str(tmp_path / 'temp'))
    (program / 'bundles/ocr/model.bin').write_bytes(b'corrupted model')
    with pytest.raises(ValueError, match='Copied native asset differs'):
        configure(source, program, data)


def test_dependencies_outside_current_development_root_are_rejected(tmp_path):
    source, program, data = fixture(tmp_path)
    other = tmp_path / 'elsewhere/program'
    other.parent.mkdir()
    program.rename(other)
    with pytest.raises(ValueError, match='current development root'):
        configure(source, other, data)


@pytest.mark.parametrize('at_root', [False, True])
def test_physical_dependencies_reject_real_windows_junctions(tmp_path, at_root):
    actual, root = tmp_path / 'actual', tmp_path / 'program'
    actual.mkdir()
    if at_root:
        link = root
    else:
        root.mkdir()
        link = root / 'runtime'
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        f'New-Item -ItemType Junction -Path {quote(link)} -Target {quote(actual)} | Out-Null'], check=True)
    try:
        with pytest.raises(ValueError, match='directory link'):
            assert_physical_tree(root)
    finally:
        link.rmdir()

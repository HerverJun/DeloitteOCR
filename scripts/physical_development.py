"""Bind current sources to physical dependencies inside the development root."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat

from scripts.development_native import SERVER, WORKER, _json, _sha
from workbench_platform.catalog import routes
from workbench_platform.catalog.registry import AppRegistry, _bundle_path
from workbench_platform.supervisor.processes import require_f_path, storage_environment


def assert_physical_tree(root: Path) -> None:
    require_f_path(root)
    if root.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f'Central dependencies root is a directory link: {root}')
    for base, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            member = Path(base) / name
            if member.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f'Central dependencies contain a directory link: {member}')


def pth(runtime: Path, source: Path, native_source: Path | None = None) -> None:
    paths = ['python312.zip', '.']
    if native_source:
        paths.append(str(native_source / 'src'))
    paths += [str(source), str(source / 'src'), 'Lib/site-packages', 'import site', '']
    (runtime / 'python312._pth').write_text('\n'.join(paths), encoding='utf-8')


def configure(source: Path, program: Path, data: Path) -> AppRegistry:
    assert_physical_tree(program)
    source, program, data = (require_f_path(path) for path in (source, program, data))
    if program.parent != source.parent.parent:
        raise ValueError('Dependencies must be inside the current development root')
    manifest = program / 'dependency-manifest.json'
    receipt = json.loads((program / 'copy-verification.json').read_text(encoding='utf-8'))
    if (receipt['program_root'] != str(program) or receipt['directory_links'] != 0 or
            receipt['dependency_manifest_sha256'] != _sha(manifest)):
        raise ValueError('Physical dependency verification identity differs')
    os.environ.update(storage_environment(data))
    os.environ['WORKBENCH_DATA_PYTHON'] = str(program / 'runtimes/data/python.exe')
    os.environ['WORKBENCH_FORENSICS_PYTHON'] = str(program / 'runtimes/forensics/python.exe')
    for name in ('platform', 'data'):
        pth(program / 'runtimes' / name, source)
    forensic = program / 'runtimes/forensics/python312._pth'
    forensic.write_text('\n'.join(['python312.zip', '.', 'Lib/site-packages',
        str(source / 'modules/image_forensics'), 'import site', '']), encoding='utf-8')
    configs = data / 'config/apps'
    configs.mkdir(parents=True, exist_ok=True)
    entry_hashes = {}
    for app_id in ('ocr', 'classification'):
        print(f'Checking physically copied {app_id} launch and model declarations', flush=True)
        bundle = program / 'bundles' / app_id
        native_source = require_f_path(source.parent / app_id)
        if not native_source.is_relative_to(program.parent):
            raise ValueError('Native source escapes the development root')
        original = json.loads((program / f'provenance/{app_id}-bundle.manifest.json').read_text(encoding='utf-8'))
        changed = {'launch.cmd', 'server.py', 'worker.py'}
        for runtime in (bundle / 'runtimes').iterdir():
            if (runtime / 'python.exe').is_file():
                pth(runtime, source, native_source)
                changed.add(f'runtimes/{runtime.name}/python312._pth')
        if app_id == 'ocr':
            assert_physical_tree(native_source / 'frontend/dist')
            shutil.copytree(native_source / 'frontend/dist', bundle / 'web', dirs_exist_ok=True)
        else:
            shutil.copyfile(bundle / 'src/image_platform/platform_weights.py', bundle / 'offline_weights.py')
            (bundle / 'worker.py').write_text(WORKER, encoding='utf-8')
            changed.update({'offline_weights.py', 'worker.py'})
        _json(bundle / 'native-binding.json', {'app_id': app_id, 'data_root': str(data),
            'asset_bundle': str(bundle), 'native_source': str(native_source), 'platform_source': str(source),
            'qualification': 'physical dependencies inside current development directory'})
        changed.add('native-binding.json')
        (bundle / 'server.py').write_text(SERVER, encoding='utf-8')
        (bundle / 'launch.cmd').write_text(
            '@echo off\n"%~dp0runtimes\\control\\python.exe" -I -B -u "%~dp0server.py"\n', encoding='utf-8')
        declared = {item['path']: item['sha256'] for item in original['files']
                    if item['path'] not in changed and not item['path'].startswith('web/')}
        # Verify preserved critical payloads against the original package, then
        # declare changed launchers and the current UI by their actual hashes.
        for name, expected in declared.items():
            if _sha(bundle.joinpath(*_bundle_path(name))) != expected:
                raise ValueError(f'Copied native asset differs: {app_id}/{name}')
        if app_id == 'ocr':
            changed.update(path.relative_to(bundle).as_posix() for path in (bundle / 'web').rglob('*') if path.is_file())
        declared.update({name: _sha(bundle / name) for name in changed if (bundle / name).is_file()})
        _json(bundle / 'bundle.manifest.json', {'app_id': app_id, 'files': [
            {'path': name, 'sha256': digest} for name, digest in sorted(declared.items())]})
        shutil.copyfile(source / f'config/apps/{app_id}.json', configs / f'{app_id}.json')
        _json(configs / f'{app_id}.install.json', {'app_id': app_id, 'installed': True,
            'manifest_sha256': _sha(configs / f'{app_id}.json'), 'launch': {
                'bundle_root': str(bundle), 'executable': str(bundle / 'launch.cmd'),
                'working_directory': str(bundle), 'allowed_arguments': [],
                'executable_sha256': _sha(bundle / 'launch.cmd'),
                'bundle_manifest_sha256': _sha(bundle / 'bundle.manifest.json')}})
        entry_hashes[app_id] = {name: _sha(bundle / name) for name in (
            'bundle.manifest.json', 'launch.cmd', 'server.py',
            'runtimes/control/python.exe', 'runtimes/control/python312._pth')}
    token = data / 'ocr-token.txt'
    if not token.is_file():
        import secrets
        token.write_text(secrets.token_urlsafe(36), encoding='utf-8')
    os.environ['WORKBENCH_OCR_TOKEN_FILE'] = str(token)
    registry = AppRegistry(configs, install_root=program / 'bundles')
    if registry.failures or not all(value.installed for value in registry.installations.values()):
        raise ValueError(f'Physical development registration failed: {registry.failures}')
    for app_id, hashes in entry_hashes.items():
        registry.trust_startup_verification(app_id, hashes)
    _json(program / 'development-binding.json', {'program_root': str(program), 'source': str(source),
        'business_data': str(data), 'dependency_copy_receipt': receipt,
        'launch_entry_hashes': entry_hashes,
        'qualification': 'Original copy receipt covers preserved sealed bytes; development overrides are separately declared'})
    routes._registry = registry
    return registry

"""Relocatable shared-program Clear/Harness candidate entry, copied as launch.py."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


_verification_paths = None


@contextmanager
def verification_scope():
    """Reuse ancestor lstat results only within one read-only admission pass."""
    global _verification_paths
    previous = _verification_paths
    if previous is None:
        _verification_paths = {}
    try:
        yield
    finally:
        _verification_paths = previous


def verification(function):
    @wraps(function)
    def verified(*args, **kwargs):
        with verification_scope():
            return function(*args, **kwargs)
    return verified


def checked(value, *, exists=False):
    path = Path(value).absolute()
    if _verification_paths is not None:
        if path.drive.upper() != 'F:' or '..' in path.parts:
            raise ValueError('Integrity inputs must remain on real local F paths')
        for member in reversed((path, *path.parents)):
            if member not in _verification_paths:
                try:
                    identity = member.lstat()
                except FileNotFoundError:
                    if exists:
                        raise
                    continue
                if (stat.S_ISLNK(identity.st_mode)
                        or getattr(identity, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError('Linked integrity inputs are not permitted: ' + str(member))
                _verification_paths[member] = identity
        return path
    if (path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:'
            or any(item.is_symlink() or item.is_junction() for item in (path, *path.parents))):
        raise ValueError('Candidate, shared assets and writable paths must be real local F paths')
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return path.resolve()


def sha(path):
    with checked(path, exists=True).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@verification
def integration_runtime(manifest):
    """Validate optional fixed local document/Pro workers before enabling routes."""
    integration = manifest.get('document_integration')
    if integration is None:
        return {}, ()
    if integration.get('format') != 'clear-document-integration-v1':
        raise ValueError('Unknown document integration declaration')
    runtime_root = checked(integration['root'], exists=True)
    program = checked(manifest['shared_program'], exists=True)

    def identities(directory, declared):
        if not isinstance(declared, dict) or not declared:
            raise ValueError('Document workers require a nonempty pinned dependency closure')
        for name, expected in declared.items():
            relative = PurePosixPath(name)
            if (not isinstance(name, str) or '\\' in name or ':' in name or relative.is_absolute()
                    or any(part in {'', '.', '..'} for part in name.split('/'))
                    or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
                raise ValueError('Invalid document dependency identity')
            path = checked(directory / relative, exists=True)
            if not path.is_file() or not path.is_relative_to(directory) or sha(path) != expected:
                raise ValueError('Pinned document/Pro dependency changed: ' + name)

    def closure(directory, declared):
        actual = set()
        for path in directory.rglob('*'):
            checked(path, exists=True)
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in {'.pyc', '.pyo'}:
                actual.add(path.relative_to(directory).as_posix())
        if actual != set(declared):
            raise ValueError('Document dependency closure has missing or undeclared files: ' + str(directory))

    identities(runtime_root, integration['files'])
    expected_closed = {'dependencies/docvortex', 'dependencies/mineru_pro', 'models/MinerU2.5-Pro-2605-1.2B'}
    if set(integration.get('closed_directories', [])) != expected_closed:
        raise ValueError('Document workers must close their two isolated runtimes and local model')
    for name in sorted(expected_closed):
        directory = checked(runtime_root / name, exists=True)
        declared = {key[len(name) + 1:]: value for key, value in integration['files'].items()
                    if key.startswith(name + '/')}
        closure(directory, declared)
    allowed = {'WORKBENCH_DOCUMENTS_PYTHON', 'CLEAR_MINERU_PRO_PYTHON', 'CLEAR_MINERU_PRO_MODEL',
               'CLEAR_MINERU_PRO_RUNTIME_MANIFEST', 'CLEAR_MINERU_PRO_RUNTIME_MANIFEST_SHA256'}
    environment = integration['environment']
    if set(environment) != allowed:
        raise ValueError('Incomplete document integration worker environment')
    for key, value in environment.items():
        if key.endswith('_SHA256'):
            continue
        if not checked(value, exists=True).is_relative_to(runtime_root):
            raise ValueError('Document worker/model escapes pinned dependency root')
    required_paths = {'WORKBENCH_DOCUMENTS_PYTHON': 'dependencies/docvortex/python.exe',
                      'CLEAR_MINERU_PRO_PYTHON': 'dependencies/mineru_pro/python.exe',
                      'CLEAR_MINERU_PRO_MODEL': 'models/MinerU2.5-Pro-2605-1.2B',
                      'CLEAR_MINERU_PRO_RUNTIME_MANIFEST': 'dependencies/mineru-runtime-sources.json'}
    if any(checked(environment[key], exists=True) != runtime_root / name for key, name in required_paths.items()):
        raise ValueError('Document runtime environment differs from its pinned worker paths')
    runtime_manifest = required_paths['CLEAR_MINERU_PRO_RUNTIME_MANIFEST']
    if (integration.get('runtime_manifest') != runtime_manifest
            or integration['files'].get(runtime_manifest) != environment['CLEAR_MINERU_PRO_RUNTIME_MANIFEST_SHA256']):
        raise ValueError('MinerU native runtime inventory is not explicitly pinned')
    # Embedded Python may import shared libraries through ._pth. Pin every such
    # directory as a closed read-only input, without copying CUDA runtimes.
    expected_imports = set()
    for worker in ('docvortex', 'mineru_pro'):
        directory = runtime_root / 'dependencies' / worker
        lines = (directory / 'python312._pth').read_text('utf-8-sig').splitlines()
        values = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith('#')]
        if values[:2] != ['python312.zip', '.'] or values[-1:] != ['import site']:
            raise ValueError('Document embedded Python import configuration changed')
        for value in values[2:-1]:
            path = checked(value, exists=True)
            if path == directory / 'Lib/site-packages':
                continue
            if not path.is_dir() or not path.is_relative_to(program):
                raise ValueError('Document shared imports escape the pinned shared program')
            expected_imports.add(path)
    shared = integration.get('shared_imports', [])
    actual_imports = [checked(item['root'], exists=True) for item in shared]
    if set(actual_imports) != expected_imports or len(actual_imports) != len(expected_imports):
        raise ValueError('Document shared import closure differs from its embedded Python configuration')
    for directory, item in zip(actual_imports, shared):
        identities(directory, item['files'])
        closure(directory, item['files'])
    read_roots = tuple(checked(runtime_root / name, exists=True) for name in sorted(expected_closed))
    return environment, (*read_roots, checked(runtime_root / runtime_manifest, exists=True))


@verification
def safire_runtime(manifest):
    """Verify the separately sealed local SAFIRE inference payload read-only."""
    integration = manifest.get('safire_integration')
    backend = manifest.get('deep_forensics_backend')
    if backend not in {None, 'safire', 'catnet'}:
        raise ValueError('Unknown declared deep forensics backend')
    if integration is None:
        if backend == 'safire':
            raise ValueError('SAFIRE default requires its pinned inference payload')
        if backend == 'catnet':
            return {'WORKBENCH_DEEP_FORENSICS_BACKEND': 'catnet'}, ()
        return {}, ()
    if integration.get('format') != 'clear-safire-integration-v1':
        raise ValueError('Unknown SAFIRE integration declaration')
    runtime_root = checked(integration['root'], exists=True)
    identities = integration['files']
    if not isinstance(identities, dict) or not identities:
        raise ValueError('SAFIRE integration must pin its inference closure')
    for name, expected in identities.items():
        path = checked(runtime_root / name, exists=True)
        if not path.is_file() or not path.is_relative_to(runtime_root) or sha(path) != expected:
            raise ValueError('Pinned SAFIRE dependency changed: ' + name)
    for name in integration['closed_directories']:
        directory = checked(runtime_root / name, exists=True)
        if not directory.is_relative_to(runtime_root):
            raise ValueError('SAFIRE closure directory escapes its payload')
        actual = set()
        for path in directory.rglob('*'):
            checked(path, exists=True)
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in {'.pyc', '.pyo'}:
                actual.add(path.relative_to(runtime_root).as_posix())
        expected = {key for key in identities if key.startswith(name.rstrip('/') + '/')}
        if actual != expected:
            raise ValueError('SAFIRE dependency closure has missing or undeclared files: ' + name)
    environment = integration['environment']
    allowed = {'WORKBENCH_SAFIRE_ROOT', 'WORKBENCH_SAFIRE_PYTHON', 'WORKBENCH_DEEP_FORENSICS_BACKEND'}
    if set(environment) != allowed or environment['WORKBENCH_DEEP_FORENSICS_BACKEND'] not in {'safire', 'catnet'}:
        raise ValueError('Incomplete SAFIRE integration environment')
    if backend is not None and environment['WORKBENCH_DEEP_FORENSICS_BACKEND'] != backend:
        raise ValueError('Deep backend and SAFIRE environment declaration disagree')
    if checked(environment['WORKBENCH_SAFIRE_ROOT'], exists=True) != runtime_root:
        raise ValueError('SAFIRE root differs from its pinned payload')
    runtime = checked(environment['WORKBENCH_SAFIRE_PYTHON'], exists=True)
    program = checked(manifest['shared_program'], exists=True)
    if runtime != program / 'runtimes/catnet/python.exe' or str(runtime) not in manifest['shared_identity']:
        raise ValueError('SAFIRE runtime must be the pinned shared CUDA Python')
    # Deployment DATA backups are a sibling under this asset root. Grant only
    # inference directories, so this dependency cannot broaden workspace reads.
    read_roots = tuple(checked(runtime_root / name, exists=True)
                       for name in ('scripts', 'models', 'dependencies', 'upstream', 'evidence'))
    return environment, read_roots


@verification
def comparison_runtime(manifest):
    integration = manifest.get('comparison_integration')
    if integration is None:
        return {}, ()
    if integration.get('format') != 'clear-comparison-runtime-v1':
        raise ValueError('Unknown comparison runtime declaration')
    office = checked(integration['libreoffice_root'], exists=True)
    inventory_path = checked(integration['inventory_path'], exists=True)
    if sha(inventory_path) != integration['inventory_sha256']:
        raise ValueError('Comparison renderer inventory changed')
    inventory = json.loads(inventory_path.read_text('utf-8'))
    declared = inventory['files']
    if not declared or checked(inventory['root'], exists=True) != office:
        raise ValueError('Comparison renderer root differs from its inventory')
    actual = {path.relative_to(office).as_posix() for path in office.rglob('*') if checked(path, exists=True).is_file()}
    if actual != set(declared):
        raise ValueError('Comparison renderer closure changed')
    for name, expected in declared.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ':' in name or '\\' in name or any(part in {'', '.', '..'} for part in name.split('/')):
            raise ValueError('Invalid comparison renderer member')
        if sha(office / relative) != expected:
            raise ValueError('Comparison renderer member changed: ' + name)
    entry = checked(office / 'program/soffice.com', exists=True)
    return {'WORKBENCH_LIBREOFFICE': str(entry)}, (office, inventory_path.parent)


def main():
    root = checked(Path(__file__).resolve().parent, exists=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path)
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--enable-agent', action='store_true')
    parser.add_argument('--disable-agent', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--offline-smoke', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    manifest = json.loads((root / 'candidate.json').read_text('utf-8'))
    if manifest.get('engine', {}).get('name') != 'deepseek-harness':
        raise ValueError('Not a Harness candidate')
    with verification_scope():
        entries = [checked(path, exists=True) for path in root.rglob('*')]
        actual = {path.relative_to(root).as_posix() for path in entries if path.is_file() and path != root / 'candidate.json'}
        if actual != set(manifest['files']):
            raise ValueError('Candidate closure contains missing or undeclared files')
        for relative, expected in manifest['files'].items():
            path = checked(root / relative, exists=True)
            if not path.is_relative_to(root) or sha(path) != expected:
                raise ValueError('Candidate member changed: ' + relative)
        program = checked(manifest['shared_program'], exists=True)
        for name, expected in manifest['shared_identity'].items():
            path = checked(name, exists=True)
            if not path.is_relative_to(program) or sha(path) != expected:
                raise ValueError('Pinned shared program identity changed: ' + name)
        document_environment, integration_roots = integration_runtime(manifest)
        safire_environment, safire_roots = safire_runtime(manifest)
        comparison_environment, comparison_roots = comparison_runtime(manifest)
    if checked(sys.executable) != program / 'runtimes/platform/python.exe':
        raise ValueError('Use the candidate declared shared platform Python with -I -B')
    for key in list(os.environ):
        if key.startswith('WORKBENCH_') or key in {'PYTHONPATH', 'PYTHONHOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NODE_OPTIONS', 'NODE_PATH',
                                                  'CLEAR_EXECUTION_MODE', 'CLEAR_TASK_DATABASE_URL'}:
            os.environ.pop(key, None)
    sys.path[:0] = [str(root / 'platform'), str(root / 'platform/src')]
    os.environ['WORKBENCH_AGENT_LAYER'] = str(root / 'dependencies/agent')
    from workbench_platform.agent.dependencies import load_support_dependencies
    load_support_dependencies()
    from workbench_platform.agent.harness import installation
    harness, node = installation()
    if node != root / 'platform/agent_harness/node/node.exe' or harness != root / 'platform/agent_harness':
        raise ValueError('Harness runtime is not the packaged local runtime')
    if subprocess.check_output([str(node), '--version'], cwd=root, text=True).strip() != 'v24.22.0':
        raise ValueError('Packaged Node runtime version changed')
    from scripts.package_harness_candidate import shared_worker_identity
    worker_identity = shared_worker_identity(program, manifest['shared_identity'])
    if any(manifest['shared_identity'].get(name) != expected for name, expected in worker_identity.items()):
        raise ValueError('Candidate does not pin its shared classification worker payload')
    from scripts.agent_native_preview import _native_inputs
    _native_inputs(root / 'platform', program)
    from scripts.classification_worker_compat import validate as classification_store_compatibility
    classification_compatibility = classification_store_compatibility(root / 'classification', program / 'bundles/classification')
    compatibility_identity = {key: value for key, value in classification_compatibility.items()
                              if key not in {'api_source', 'store_path', 'adapter_path'}}
    if manifest.get('classification_store_compatibility') != compatibility_identity:
        raise ValueError('Candidate classification Store compatibility identity changed')
    if args.offline_smoke:
        if args.output is None:
            raise ValueError('--offline-smoke requires a new --output directory on F:')
        output = checked(args.output)
        if output.exists() or output.is_relative_to(root) or output.is_relative_to(program):
            raise ValueError('Offline evidence must be a new F directory outside the candidate/shared program')
        output.mkdir(parents=True)
        from scripts.package_harness_candidate import environment
        environment(output)
        subprocess.run([str(node), str(root / 'platform/scripts/harness_candidate_smoke.mjs'), str(root), str(output)],
                       cwd=root, env=dict(os.environ), check=True)
        return
    if args.data is None:
        raise ValueError('--data must identify an explicit F workspace')
    data = checked(args.data)
    if data.is_relative_to(root) or data.is_relative_to(program):
        raise ValueError('DATA must stay outside the sealed candidate and shared program')
    if not 1 <= args.port <= 65535:
        raise ValueError('Invalid port')
    from scripts.product_compatibility import check
    compatibility = check(data, manifest['data_compatibility'])
    if args.check_only:
        print(json.dumps({'checked': True, 'candidate': str(root), 'product_version': manifest['product_version'],
            'data': str(data), 'compatible': compatibility, 'node': str(node), 'agent_enabled': args.enable_agent and not args.disable_agent,
            'server_started': False, 'data_changed': False}, ensure_ascii=False))
        return
    os.environ['CLEAR_PADDLE_TEMP'] = str(checked(data / 'cache/temp'))
    native_temp = checked(data / 'cache/temp/native')
    if not str(native_temp).isascii():
        raise ValueError('Native temporary directory must be an ASCII F drive path')
    native_temp.mkdir(parents=True, exist_ok=True)
    os.environ['CLEAR_NATIVE_TEMP_ROOT'] = str(native_temp)
    from workbench_platform.supervisor.processes import storage_environment
    os.environ.update(storage_environment(data))
    import tempfile
    tempfile.tempdir = os.environ['TEMP']
    os.environ.update(TMPDIR=os.environ['TEMP'], WORKBENCH_PROGRAM_ROOT=str(program),
        WORKBENCH_AGENT_ENABLED='1' if args.enable_agent and not args.disable_agent else '0',
        WORKBENCH_PORTAL_DIST=str(root / 'platform/apps/portal/dist'), DSH_HOME=str(data / 'agent/harness'),
        DSH_RESOURCE_CACHE=str(data / 'agent/harness/cache'), DSH_PRIMARY_RUNTIME='',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1',
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
        LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false')
    os.environ.update(document_environment)
    os.environ.update(safire_environment)
    os.environ.update(comparison_environment)
    os.environ['WORKBENCH_OCR_SOURCE'] = str(root / 'ocr')
    from scripts.harness_io_guard import install_guard
    install_guard(write_roots=(data, Path(os.environ['TEMP'])), read_roots=(root, program, *integration_roots, *safire_roots, *comparison_roots))
    from scripts.agent_native_preview import main as serve
    sys.argv = ['clear-harness', '--source', str(root / 'platform'), '--program', str(program),
                '--data', str(data), '--port', str(args.port)]
    serve()


if __name__ == '__main__':
    main()

"""Relocatable shared-program Clear/Harness candidate entry, copied as launch.py."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def checked(value, *, exists=False):
    path = Path(value).absolute()
    if (path.drive.upper() != 'F:' or path.resolve().drive.upper() != 'F:'
            or any(item.is_symlink() or item.is_junction() for item in (path, *path.parents))):
        raise ValueError('Candidate, shared assets and writable paths must be real local F paths')
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return path.resolve()


def sha(path):
    with checked(path, exists=True).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
    from workbench_platform.supervisor.processes import storage_environment
    os.environ.update(storage_environment(data))
    os.environ.update(TMPDIR=os.environ['TEMP'], WORKBENCH_PROGRAM_ROOT=str(program),
        WORKBENCH_AGENT_ENABLED='1' if args.enable_agent and not args.disable_agent else '0',
        WORKBENCH_PORTAL_DIST=str(root / 'platform/apps/portal/dist'), DSH_HOME=str(data / 'agent/harness'),
        DSH_RESOURCE_CACHE=str(data / 'agent/harness/cache'), DSH_PRIMARY_RUNTIME='',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1',
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
        LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false')
    from scripts.harness_io_guard import install_guard
    install_guard(write_roots=(data, Path(os.environ['TEMP'])), read_roots=(root, program))
    from scripts.agent_native_preview import main as serve
    sys.argv = ['clear-harness', '--source', str(root / 'platform'), '--program', str(program),
                '--data', str(data), '--port', str(args.port)]
    serve()


if __name__ == '__main__':
    main()

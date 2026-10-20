"""Serve current Clear/Harness source against isolated F-drive QA DATA only.

API/Agent/Portal use --source. Native OCR/classification source and all shared
inference assets remain explicitly pinned to --sealed-release. No live entry
or shared file is modified. Creating output/stop.request closes the service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.package_harness_candidate import DEFAULT_RELEASE, DEFAULT_WORK, environment, local_f, save, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--sealed-release', type=Path, default=DEFAULT_RELEASE)
    parser.add_argument('--work', type=Path, default=DEFAULT_WORK)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--layer', type=Path)
    parser.add_argument('--portal-dist', type=Path)
    parser.add_argument('--port', type=int, default=8920)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    source, sealed, work = [local_f(value, exists=True) for value in (args.source, args.sealed_release, args.work)]
    data, output = local_f(args.data), local_f(args.output)
    program = local_f(sealed / 'program', exists=True)
    layer = local_f(args.layer or sealed / 'dependencies/agent', exists=True)
    portal = local_f(args.portal_dist or source / 'apps/portal/dist', exists=True)
    local_f(portal / 'index.html', exists=True)
    if not data.is_relative_to(work) or not output.is_relative_to(work):
        raise ValueError('QA DATA and evidence must remain beneath the explicit F task root')
    if (data == output or data.is_relative_to(output) or output.is_relative_to(data)
            or any(data.is_relative_to(path) or output.is_relative_to(path) for path in (source, sealed))):
        raise ValueError('QA DATA, source/release and server evidence must be separate directories')
    if not 1 <= args.port <= 65535:
        raise ValueError('Invalid QA port')
    if Path(sys.executable).resolve() != (program / 'runtimes/platform/python.exe').resolve():
        raise ValueError('Use the pinned shared platform Python with -I -B')
    for relative in ('src/workbench_platform/agent/harness.py', 'agent_harness/host.mjs'):
        local_f(source / relative, exists=True)
    environment(work)
    for key in list(os.environ):
        if key.startswith('WORKBENCH_') or key in {'PYTHONPATH', 'PYTHONHOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                                  'CLEAR_EXECUTION_MODE', 'CLEAR_TASK_DATABASE_URL'}:
            os.environ.pop(key, None)
    os.environ['WORKBENCH_AGENT_LAYER'] = str(layer)
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.agent.harness import installation
    harness_root, node = installation()
    if harness_root != source / 'agent_harness':
        raise ValueError('Harness import escaped the requested source')
    from scripts.agent_native_preview import _native_inputs
    _native_inputs(sealed / 'platform', program)
    if args.check_only:
        print(json.dumps({'checked': True, 'source': str(source), 'data': str(data), 'output': str(output),
            'shared_program': str(program), 'agent_layer': str(layer), 'node': str(node), 'port': args.port,
            'server_started': False, 'data_changed': False}, ensure_ascii=False))
        return
    if output.exists():
        raise ValueError('Use a new server evidence directory')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
    output.mkdir(parents=True)
    native_temp = local_f(work / 'temp' / ('qa-' + hashlib.sha256(str(output).encode()).hexdigest()[:16]))
    native_temp.mkdir(parents=True, exist_ok=True)
    os.environ.update(CLEAR_PADDLE_TEMP=str(native_temp), CLEAR_NATIVE_TEMP_ROOT=str(native_temp),
        WORKBENCH_AGENT_ENABLED='1', WORKBENCH_PORTAL_DIST=str(portal),
        LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false')
    from workbench_platform.supervisor.processes import storage_environment
    os.environ.update(storage_environment(data))
    os.environ['TMPDIR'] = os.environ['TEMP']
    from scripts.agent_native_preview import configure
    from scripts.candidate_bootstrap import _FileLease
    lease = _FileLease(data / '.live-preview.lock')
    if not lease.acquire():
        raise ValueError('Isolated QA DATA is already in use')
    violations = []
    from scripts.harness_io_guard import install_guard
    install_guard(write_roots=(data, output, native_temp), read_roots=(source, sealed, layer, portal), violations=violations)
    receipt = {'format': 'clear-harness-source-qa-v1', 'source': str(source), 'shared_program': str(program),
        'native_source': str(sealed), 'agent_layer': str(layer), 'node': str(node), 'node_sha256': sha(node),
        'data': str(data), 'pid': os.getpid(), 'origin': f'http://127.0.0.1:{args.port}',
        'stop_file': str(output / 'stop.request'), 'server_closed': False, 'blocked_actions': violations}
    try:
        configure(sealed / 'platform', program, data)
        import uvicorn
        from workbench_platform.app import app
        receipt['imports'] = {}
        for name in ('workbench_platform.app', 'workbench_platform.agent.harness', 'scripts.agent_native_preview'):
            location = Path(sys.modules[name].__file__).resolve()
            if not location.is_relative_to(source):
                raise ValueError('QA application import escaped source: ' + str(location))
            receipt['imports'][name] = {'path': str(location), 'sha256': sha(location)}
        receipt['portal_sha256'] = sha(portal / 'index.html')
        save(output / 'server-receipt.json', receipt)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False,
                                               timeout_graceful_shutdown=20))
        def watch_stop():
            while not (output / 'stop.request').exists() and not server.should_exit:
                time.sleep(.25)
            server.should_exit = True
        threading.Thread(target=watch_stop, daemon=True).start()
        print(json.dumps({'configured': True, 'origin': receipt['origin'], 'pid': os.getpid(),
                          'stop_file': receipt['stop_file']}, ensure_ascii=False), flush=True)
        server.run()
        receipt['server_closed'] = True
    finally:
        lease.close()
        save(output / 'server-receipt.json', receipt)


if __name__ == '__main__':
    main()

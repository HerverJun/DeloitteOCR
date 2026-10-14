"""Serve only the sealed F: candidate's API and production Portal for browser QA.

Use the candidate platform Python with -I -B. This harness writes only to the
explicit acceptance DATA, a fresh server output directory and its native temp.
It never changes candidate product code. A stop-file requests graceful shutdown.
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

RUN = Path('F:/OCR/output/forensics-stability-20261014')
DEFAULT_CANDIDATE = RUN/'delivery/Clear-0.11.1-forensics-candidate-001'


def local_f(value, *, exists=False):
    requested = Path(value).absolute()
    if requested.drive.upper() != 'F:' or requested.resolve().drive.upper() != 'F:':
        raise ValueError('Path must resolve to local F: ' + str(requested))
    if any(parent.is_symlink() or parent.is_junction() for parent in (requested, *requested.parents)):
        raise ValueError('Redirected path is not permitted: ' + str(requested))
    if exists and not requested.exists():
        raise FileNotFoundError(requested)
    return requested.resolve()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8894)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    candidate = local_f(args.candidate, exists=True)
    data = local_f(args.data)
    output = local_f(args.output)
    if not candidate.is_relative_to(RUN/'delivery') or not data.is_relative_to(RUN) or not output.is_relative_to(RUN):
        raise ValueError('Candidate, DATA and server output must remain in this run')
    if data.is_relative_to(candidate) or output.is_relative_to(candidate) or output.is_relative_to(data):
        raise ValueError('Use acceptance DATA and a separate server evidence directory outside the candidate')
    runtime = local_f(candidate/'program/runtimes/platform/python.exe', exists=True)
    if Path(sys.executable).resolve() != runtime:
        raise ValueError('Use the candidate platform runtime: ' + str(runtime))
    required = [
        candidate/'platform/src/workbench_platform/app.py',
        candidate/'platform/scripts/agent_native_preview.py',
        candidate/'platform/apps/portal/dist/index.html',
        candidate/'ocr/frontend/dist/index.html',
        candidate/'classification/apps/web/dist/index.html',
        candidate/'program/bundles/ocr/runtimes/control/python.exe',
        candidate/'program/bundles/classification/runtimes/control/python.exe',
        candidate/'program/runtimes/data/python.exe',
        candidate/'program/runtimes/forensics/python.exe',
    ]
    for path in required:
        local_f(path, exists=True)
    if args.check_only:
        print(json.dumps({'checked': True, 'server_started': False, 'candidate': str(candidate), 'data': str(data),
                          'runtime': str(runtime), 'portal': str(required[2]), 'port': args.port}, ensure_ascii=False))
        return
    local_f(data, exists=True)
    if output.exists():
        raise ValueError('Use a new server evidence directory')
    output.mkdir(parents=True)
    native = local_f(RUN/'temp'/('portal-server-' + hashlib.sha256(str(output).encode()).hexdigest()[:16]))
    native.mkdir(parents=True, exist_ok=True)
    # Never inherit development bindings, proxy configuration or import paths.
    for key in list(os.environ):
        if key.startswith('WORKBENCH_') or key.upper() in {'PYTHONPATH','PYTHONHOME','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','CLEAR_FORENSICS_ACCEPT_GUARD'}:
            os.environ.pop(key, None)
    caches = ('APPDATA','LOCALAPPDATA','HF_HOME','TORCH_HOME','MODELSCOPE_CACHE','XDG_CACHE_HOME','XDG_CONFIG_HOME',
              'XDG_DATA_HOME','MPLCONFIGDIR','NUMBA_CACHE_DIR','PADDLE_PDX_CACHE_HOME','PADDLEOCR_HOME','PIP_CACHE_DIR',
              'npm_config_cache','CUDA_CACHE_PATH','TORCHINDUCTOR_CACHE_DIR','TRITON_CACHE_DIR','PYTHONPYCACHEPREFIX',
              'HF_DATASETS_CACHE','HUGGINGFACE_HUB_CACHE','JUPYTER_CONFIG_DIR','JUPYTER_DATA_DIR')
    for name in caches:
        location = local_f(output/'runtime'/name.lower())
        location.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(location)
    for name in ('TEMP','TMP','TMPDIR','CLEAR_NATIVE_TEMP_ROOT','CLEAR_PADDLE_TEMP'):
        os.environ[name] = str(native)
    os.environ.update(WORKBENCH_DATA_DIR=str(data), WORKBENCH_PROGRAM_ROOT=str(candidate/'program'),
        WORKBENCH_AGENT_LAYER=str(candidate/'dependencies/agent'), WORKBENCH_AGENT_ENABLED='0',
        WORKBENCH_DATA_PYTHON=str(candidate/'program/runtimes/data/python.exe'),
        WORKBENCH_FORENSICS_PYTHON=str(candidate/'program/runtimes/forensics/python.exe'),
        WORKBENCH_PORTAL_DIST=str(candidate/'platform/apps/portal/dist'),
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1',
        PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8', CUDA_VISIBLE_DEVICES='',
        PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True')
    violations = []
    write_roots = (data, output, native)
    forbidden_roots = (RUN/'worktrees', Path('F:/OCR/output/clear-automation-dev-20261002'),
                       Path('F:/OCR/output/clear-workbench-preview-20261002'), Path('F:/OCR/products/Clear'))
    harness = Path(__file__).resolve()
    def path_value(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            text = os.fsdecode(value)
            if text.casefold() not in {'nul','nul:',r'\\.\nul'}:
                return Path(text).absolute().resolve()
        return None
    def writable(path):
        return any(path == root or path.is_relative_to(root) for root in write_roots)
    def reject(event, path):
        violations.append({'event': event, 'path': str(path), 'pid': os.getpid()})
        save(output/'blocked-actions.json', violations)
        raise RuntimeError('Candidate Portal guard blocked ' + event + ': ' + str(path))
    def audit(event, fields):
        if event in {'open','os.listdir','os.scandir'} and fields:
            path = path_value(fields[0])
            if path and path != harness and any(path == root or path.is_relative_to(root) for root in forbidden_roots):
                reject('development_read', path)
            if event == 'open' and path:
                mode = fields[1] if len(fields) > 1 else ''
                flags = fields[2] if len(fields) > 2 else 0
                writing = isinstance(mode, str) and any(char in mode for char in 'wax+')
                writing |= isinstance(flags, int) and bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
                if writing and not writable(path):
                    reject('write_outside_acceptance', path)
        if event in {'os.mkdir','os.remove','os.rmdir','os.chmod','os.utime','sqlite3.connect'} and fields:
            path = path_value(fields[0])
            if path and str(fields[0]) != ':memory:' and not writable(path):
                reject(event, path)
        if event in {'os.rename','os.replace','os.link','os.symlink'}:
            targets = fields[:2] if event in {'os.rename','os.replace'} else fields[1:2]
            for value in targets:
                path = path_value(value)
                if path and not writable(path):
                    reject(event, path)
    sys.addaudithook(audit)
    sys.path[:0] = [str(candidate/'platform'), str(candidate/'platform/src')]
    from scripts.agent_native_preview import configure
    from scripts.candidate_bootstrap import _FileLease
    lease = _FileLease(data/'.live-preview.lock')
    if not lease.acquire():
        raise RuntimeError('Acceptance DATA is already in use')
    receipt = {'candidate': str(candidate), 'data': str(data), 'runtime': str(runtime), 'runtime_sha256': sha(runtime),
               'harness': {'path': str(harness), 'sha256': sha(harness)}, 'pid': os.getpid(),
               'origin': 'http://127.0.0.1:' + str(args.port), 'server_closed': False, 'blocked_actions': violations,
               'scope': 'Same-computer candidate API and production Portal; CPU browser rendering; no business accuracy claim'}
    try:
        configure(candidate/'platform', candidate/'program', data)
        import uvicorn
        from workbench_platform.app import app
        modules = ('scripts.agent_native_preview','workbench_platform.app','workbench_platform.multimodal.service','workbench_platform.forensics.service')
        receipt['imports'] = {}
        for name in modules:
            location = Path(sys.modules[name].__file__).resolve()
            if not location.is_relative_to(candidate):
                raise RuntimeError('Application source escaped candidate: ' + str(location))
            receipt['imports'][name] = {'path': str(location), 'sha256': sha(location)}
        bindings = json.loads((data/'agent-native/receipt.json').read_text('utf-8'))['bindings']
        for binding in bindings:
            for key in ('source','asset','web_root'):
                if not Path(binding[key]).resolve().is_relative_to(candidate):
                    raise RuntimeError('Native binding escaped candidate: ' + binding[key])
        receipt['native_bindings'] = bindings
        receipt['portal_sha256'] = sha(candidate/'platform/apps/portal/dist/index.html')
        receipt['stop_file'] = str(output/'stop.request')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', args.port))
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False, timeout_graceful_shutdown=15))
        def watch_stop():
            while not (output/'stop.request').exists() and not server.should_exit:
                time.sleep(.25)
            server.should_exit = True
        threading.Thread(target=watch_stop, daemon=True).start()
        save(output/'server-receipt.json', receipt)
        print(json.dumps({'configured': True, 'origin': receipt['origin'], 'pid': os.getpid(), 'stop_file': receipt['stop_file']}), flush=True)
        server.run()
        receipt['server_closed'] = True
    finally:
        lease.close()
        receipt['blocked_actions'] = violations
        save(output/'server-receipt.json', receipt)


if __name__ == '__main__':
    main()

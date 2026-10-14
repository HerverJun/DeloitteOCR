"""Start the physical local development environment without old-package links."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--development-root', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--agent-layer', type=Path, default=Path('F:/OCR/output/platform-agent-dev-20261004/dependencies/agent'))
    args = parser.parse_args()
    root, data = args.development_root.resolve(), args.data.resolve()
    source, program, run = root / 'worktrees/platform', root / 'program', root / 'runs/local-development'
    for path in (root, data, source, program, run):
        if path.drive.upper() != 'F:':
            raise ValueError('Development paths must remain on the local F drive')
    run.mkdir(parents=True, exist_ok=True)
    temporary = root / 'temp'
    temporary.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
                      CLEAR_PADDLE_TEMP=str(temporary), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    sys.path[:0] = [str(source), str(source / 'src')]
    os.environ['WORKBENCH_DATA_DIR'] = str(data)
    os.environ['WORKBENCH_PORTAL_DIST'] = str(source / 'apps/portal/dist')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', args.port))
    from scripts.candidate_bootstrap import _FileLease
    from workbench_platform.supervisor.processes import require_f_path, storage_environment
    os.environ.update(storage_environment(data))
    layer = require_f_path(args.agent_layer)
    if not (layer / 'layer-receipt.json').is_file():
        raise ValueError('Verified Agent dependency layer is missing')
    os.environ['WORKBENCH_AGENT_LAYER'] = str(layer)
    from scripts.agent_native_preview import configure
    lease = _FileLease(data / '.live-preview.lock')
    if not lease.acquire():
        raise RuntimeError('This business data directory is already in use')
    try:
        configure(source, program, data)
        import uvicorn
        import workbench_platform.app as platform
        (run / 'service.json').write_text(json.dumps({
            'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat(),
            'url': f'http://127.0.0.1:{args.port}/', 'source': str(source), 'data': str(data),
            'program': str(program), 'python': sys.executable, 'platform_module': platform.__file__,
            'native_registration': 'private launchers over shared F-drive dependencies',
            'agent_layer': str(layer), 'product': 'clear-intelligent-audit',
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        uvicorn.run(platform.app, host='127.0.0.1', port=args.port, access_log=False, log_level='info')
    finally:
        lease.close()


if __name__ == '__main__':
    main()

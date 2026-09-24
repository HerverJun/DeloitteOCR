"""Replay the finished real-GPU batch through the current authenticated UI.

The original GPU workspace remains immutable. This checks UI/history/download
against its actual persisted 20-page run, not a fabricated browser fixture.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from fastapi.staticfiles import StaticFiles
import uvicorn
from ocr_workbench.service import create_app


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source, out = args.source.resolve(), args.output.resolve()
    report = json.loads((source / 'gpu-batch.json').read_text('utf-8'))
    assert report['status'] == 'pass' and report['pages'] == 20
    assert report['run']['outcome'] == 'success'
    assert source != out and not out.exists()
    out.mkdir(parents=True)
    shutil.copytree(source / 'workspace', out / 'workspace')
    token = 'isolated-gpu-ui-replay'
    app = create_app(ROOT, out / 'workspace', token, start_queue=False, agent_enabled=True)
    store = app.state.store
    run = store.rows('SELECT * FROM agent_runs WHERE id=?', (report['run']['id'],))[0]
    session = store.rows('SELECT * FROM agent_sessions WHERE id=?', (report['run']['session_id'],))[0]
    artifacts = store.rows('SELECT id,project_id FROM agent_artifacts WHERE run_id=? AND state=?', (run['id'], 'ready'))
    assert run['status'] == 'completed' and run['outcome'] == 'success'
    assert session['project_id'] == artifacts[0]['project_id'] and len(artifacts) == 1
    expected = report['workbook']['sha256']
    artifact = artifacts[0]['id']
    original = source / 'workspace' / Path(report['workbook']['path']).relative_to(source / 'workspace')
    assert sha256(original) == expected
    app.mount('/', StaticFiles(directory=ROOT / 'frontend/dist', html=True))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run)
    thread.start()
    status = 'failed'
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() >= deadline or not thread.is_alive():
                raise RuntimeError('Isolated service failed to start')
            time.sleep(.05)
        seed = {'base': 'http://127.0.0.1:' + str(server.servers[0].sockets[0].getsockname()[1]),
                'token': token, 'project': session['project_id'], 'session': session['id'],
                'run': run['id'], 'artifact': artifact, 'expected_sha256': expected}
        (out / 'seed.json').write_text(json.dumps(seed, indent=2), 'utf-8')
        subprocess.run(['node', str(ROOT / 'scripts/agent_eval/audit_gpu_ui.mjs'), str(out)], cwd=ROOT, check=True)
        assert sha256(out / 'download.xlsx') == expected
        status = 'pass'
    finally:
        server.should_exit = True
        thread.join(30)
        (out / 'verification.json').write_text(json.dumps({
            'status': status, 'source_report': str(source / 'gpu-batch.json'),
            'real_gpu_batch_pages': report['pages'], 'real_controller': 'not_tested',
            'isolated_copy': str(out / 'workspace'), 'source_workbook_sha256': expected,
            'download_sha256': sha256(out / 'download.xlsx') if (out / 'download.xlsx').exists() else None,
            'service_closed': not thread.is_alive(), 'browser_result': 'browser-result.json',
        }, indent=2), 'utf-8')
    assert not thread.is_alive()


if __name__ == '__main__':
    main()

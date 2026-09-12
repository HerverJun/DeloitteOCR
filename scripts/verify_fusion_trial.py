"""Verify trial coordination in a scratch copy; leave human records untouched."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fusion_human_trial import trial_app
import uvicorn


@contextmanager
def serve(app, port=8883):
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError('Coordinator startup')
            time.sleep(.05)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        worker.join(15)
        assert not worker.is_alive(), 'Coordinator did not stop'


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, json=None, headers=None):
        import json as codec
        data = None if json is None else codec.dumps(json).encode('utf-8')
        request = urllib.request.Request(self.base + path, data=data, method=method,
                  headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = self.opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            value = response.read()
            return type('Response', (), {'status_code': response.status, 'json': lambda self: codec.loads(value)})()

    def get(self, path, **kwargs):
        return self.call('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.call('POST', path, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    scratch = out / 'synthetic-coordinator-check'
    shutil.copytree(args.trial, scratch)
    app, token = trial_app(scratch)
    checks = []
    with serve(app) as base:
        client = Client(base)
        headers = {'Authorization': 'Bearer ' + token}
        assert client.get('/api/trial/state').status_code == 401
        assert client.get('/api/trial/state', headers=headers).json()['completed'] == 0
        assert client.post('/api/trial/start', json={'operator': ''}, headers=headers).status_code == 400
        round_ = client.post('/api/trial/start', json={'operator': 'AUTOMATION-SCRATCH-NOT-HUMAN'}, headers=headers).json()
        assert client.post('/api/trial/start', json={'operator': 'AUTOMATION-SCRATCH-NOT-HUMAN'}, headers=headers).json() == round_
        body = {'round': round_['round'], 'pause_seconds': 0, 'mislocations': 0, 'miswrites': 0, 'whole_table_reviews': 0,
                'notes': 'Automated endpoint validation, excluded from human trial.'}
        assert client.post('/api/trial/finish', json=body, headers=headers).status_code == 400
        store = app.state.store
        result = store.result(round_['target_result'])
        store.set_review(round_['image_id'], 'confirmed', result['id'], result['revision'], round_['version_id'])
        assert client.post('/api/trial/finish', json={**body, 'pause_seconds': -1}, headers=headers).status_code == 400
        assert client.post('/api/trial/finish', json=body, headers=headers).json()['completed'] == 1
        assert json.loads((scratch/'human-records.json').read_text('utf-8'))['completed'] == 1
        checks.extend(['authentication', 'operator required', 'start resumes active round', 'finish requires explicit current confirmation',
                       'pause validation', 'saved snapshot and metrics in scratch only'])
        subprocess.run(['node', str(Path(__file__).with_suffix('.mjs')), f'{base}/trial#token={token}', '--interactive-scratch'], check=True)
        checks.append('new trial round reloads the named workbench window into its own project')
    # Browser sees the real prepared coordinator, without starting a round.
    app, token = trial_app(args.trial.resolve())
    with serve(app) as base:
        subprocess.run(['node', str(Path(__file__).with_suffix('.mjs')), f'{base}/trial#token={token}'], check=True)
        checks.append('real browser initial coordinator and script MIME, no page errors')
    with sqlite3.connect(args.trial/'timing.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM timing').fetchone()[0] == 0
    report = {'passed': True, 'checks': checks, 'human_rounds_completed': 0, 'browser_closed': True,
              'scope': 'Automated coordinator verification. Simulated rounds only in isolated scratch; real trial remains untouched.'}
    (out/'coordinator-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()

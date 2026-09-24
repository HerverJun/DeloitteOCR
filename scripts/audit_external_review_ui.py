"""Real application + loopback protocol fixture; never contacts a cloud API."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT / 'build/external-api-review/site-packages')]
from PIL import Image, ImageDraw, ImageFont
from fastapi.staticfiles import StaticFiles
import uvicorn
from test_external_review import ProviderFixture, KEY
from ocr_workbench import external_review as ext
from ocr_workbench.service import create_app
from ocr_workbench.imaging import add_image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    provider = ProviderFixture()
    token = 'isolated-external-review-ui-fixture'
    app = create_app(ROOT, out / 'workspace', token, review_only=True)
    app.state.external_connection.vault = ext.CredentialVault(out / 'user-credentials')
    store = app.state.store
    project = store.project('外部视觉审校验收')
    image = Image.new('RGB', (900, 400), 'white')
    ImageDraw.Draw(image).text((45, 40), 'Amount 001.05', fill='#253c32', font=ImageFont.load_default(size=44))
    path = out / 'fixture.png'; image.save(path)
    photo = add_image(store, project['id'], 'API视觉审校验收.png', path)
    version = photo['active_version']
    task = store.enqueue(project['id'], [version], ['ppocr'])[0]
    store.claim()
    store.complete(task, {'engine': 'ppocr', 'text': 'Amount 001.00', 'tables': [],
        'project_image_version': version,
        'image': {'width': 900, 'height': 400}, 'load_seconds': 0,
        'blocks': [{'kind': 'text', 'confidence': .9, 'text': 'Amount 001.00',
                    'polygon': [[40, 35], [600, 35], [600, 100], [40, 100]]}]})
    result = store.one('tasks', task)['result_id']
    probe = Image.new('RGB', (640, 120), 'white')
    ImageDraw.Draw(probe).text((24, 28), provider.probe, fill='black', font=ImageFont.load_default(size=44))
    buffer = io.BytesIO(); probe.save(buffer, format='PNG')
    probe_patch = patch.object(ext, 'make_probe', return_value=(buffer.getvalue(), provider.probe))
    probe_patch.start()

    @app.get('/api/audit/external-fixture')
    def fixture_state():
        return {'requests': len(provider.requests), 'methods': [r['method'] for r in provider.requests]}

    @app.post('/api/audit/external-fixture')
    def fixture_mode(body: dict):
        provider.mode = body.get('mode', 'normal')
        return fixture_state()

    app.mount('/', StaticFiles(directory=ROOT / 'frontend/dist', html=True))
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run); thread.start()
    code = 1
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline: raise TimeoutError('Service startup failed')
            time.sleep(.03)
        seed = {'base': 'http://127.0.0.1:' + str(server.servers[0].sockets[0].getsockname()[1]),
                'provider': provider.url, 'key': KEY, 'token': token, 'project': project['id'], 'result': result}
        (out / 'seed.json').write_text(json.dumps(seed), 'utf-8')
        code = subprocess.run(['node', str(ROOT / 'scripts/audit_external_review_ui.mjs'), str(out)], cwd=ROOT).returncode
    finally:
        server.should_exit = True; thread.join(20)
        provider.close(); probe_patch.stop()
        (out / 'shutdown.json').write_text(json.dumps({'service_closed': not thread.is_alive(),
            'external_worker_closed': not app.state.external_queue.status()['alive'], 'cloud_api_tested': False}), 'utf-8')
    if thread.is_alive(): raise RuntimeError('Service did not shut down')
    raise SystemExit(code)


if __name__ == '__main__':
    main()

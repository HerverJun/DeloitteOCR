"""CUDA llama.cpp server, F16 decoder and F16 visual projector, loopback only."""
import base64
import json
import secrets
import socket
import subprocess
import time
import urllib.request
import urllib.error


def recognize(bundle, image, output):
    binary = bundle / 'runtimes' / 'llama' / 'llama-server.exe'
    model = bundle / 'models' / 'HunyuanOCR-GGUF' / 'hyocr-f16.gguf'
    projector = model.with_name('mmproj-hyocr-f16.gguf')
    for path in [binary, model, projector]:
        if not path.is_file():
            raise FileNotFoundError(f'Missing offline asset: {path}')
    prompts = json.loads((bundle / 'config' / 'hunyuan-prompts.json').read_text(encoding='utf-8'))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    token = secrets.token_hex(24)
    cmd = [str(binary), '--model', str(model), '--mmproj', str(projector),
           '--host', '127.0.0.1', '--port', str(port), '--alias', 'HYVL',
           '--ctx-size', '10240', '--n-predict', '4096', '--n-gpu-layers', '99',
           '--parallel', '1', '--api-key', token, '--offline', '--log-verbosity', '4',
           '--fit', 'off', '--device', 'CUDA0', '--mmproj-device', 'CUDA0']
    start = time.perf_counter()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    proc = None
    with (output / 'llama-server.log').open('w', encoding='utf-8') as log:
        try:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            base = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f'llama-server exited with {proc.returncode}; see llama-server.log')
                try:
                    with opener.open(urllib.request.Request(base + '/health',
                            headers={'Authorization': f'Bearer {token}'}), timeout=2) as res:
                        if res.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.25)
            else:
                raise TimeoutError('llama-server did not become ready')
            loaded = time.perf_counter() - start
            request = {'model': 'HYVL', 'messages': [{'role': 'user', 'content': [
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image.read_bytes()).decode()}},
                {'type': 'text', 'text': prompts['doc_parse']}]}],
                'max_tokens': 4096, 'temperature': 0.0, 'top_p': 1.0,
                'top_k': -1, 'repeat_penalty': 1.08, 'seed': 0}
            req = urllib.request.Request(base + '/v1/chat/completions',
                    data=json.dumps(request).encode(), headers={'Content-Type': 'application/json',
                    'Authorization': f'Bearer {token}'})
            with opener.open(req, timeout=600) as response:
                raw = json.load(response)
            if raw['choices'][0]['finish_reason'] != 'stop':
                raise RuntimeError('Hunyuan output did not reach EOS; refusing truncated success')
            text = raw['choices'][0]['message']['content'] or ''
            return raw, [{'kind': 'document', 'text': text, 'confidence': None, 'polygon': None}], loaded
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)

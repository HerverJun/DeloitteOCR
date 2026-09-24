"""Real loopback HTTP fixtures; no cloud credentials or inference required."""
import base64
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw
from ocr_workbench import external_review as ext
from ocr_workbench.multimodal_runtime import ReviewCancelled, ReviewProtocolError, validate_reply
from ocr_workbench.store import Store

KEY = 'fixture-api-secret-7e318f0e'


class ProviderFixture:
    def __init__(self):
        self.requests = []
        self.mode = 'normal'
        self.entered, self.release = threading.Event(), threading.Event()
        self.probe = 'OCR-A12B34'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (ConnectionError, OSError):
                    pass

            def do_GET(self):
                owner.requests.append({'method': 'GET', 'path': self.path, 'headers': dict(self.headers)})
                if owner.mode.startswith('status:'):
                    return self.send({'error': {'message': KEY}}, int(owner.mode.split(':')[1]))
                if owner.mode == 'bad-list':
                    return self.send({'unexpected': []})
                if self.headers.get('x-api-key'):
                    after = parse_qs(urlsplit(self.path).query).get('after_id')
                    return self.send({'data': [{'id': 'vision-b' if after else 'vision-a'}],
                                      'has_more': not bool(after), 'last_id': 'vision-b' if after else 'vision-a'})
                self.send({'data': [{'id': 'vision-a'}, {'id': 'vision-b'}, {'id': 'vision-a'}]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append({'method': 'POST', 'path': self.path, 'headers': dict(self.headers), 'payload': payload})
                mode = owner.mode
                owner.entered.set()
                if mode == 'hold':
                    owner.release.wait(5)
                if mode.startswith('status:'):
                    return self.send({'error': {'message': KEY}}, int(mode.split(':')[1]))
                if mode == 'legacy-tokens' and 'max_completion_tokens' in payload:
                    return self.send({'error': {'message': 'Unsupported parameter: max_completion_tokens'}}, 400)
                if mode == 'other-400':
                    return self.send({'error': {'message': 'max_completion_tokens is too large'}}, 400)
                content = payload['messages'][-1]['content']
                structure_text = next((item['text'].split('\n',1)[1] for item in content
                    if item['type']=='text' and item['text'].startswith('STRUCTURE_CANDIDATE_DATA_JSON\n')),None)
                if structure_text is not None:
                    structure=json.loads(structure_text)
                    candidate=structure['candidates'][0]
                    answer={'version':structure['version'],'decision':'select','candidate_id':candidate['id'],
                        'token_ids':candidate['token_ids'],'reason':'表格行列与原图一致'}
                    if mode=='unknown-target':answer['candidate_id']='unknown'
                    if mode=='duplicate-target':answer['token_ids']*=2
                    if mode=='illegal-span':answer['span']=[0,0,999,999]
                    if mode=='abstain':answer.update(decision='abstain',candidate_id=None,token_ids=[])
                    result=json.dumps(answer,ensure_ascii=False)
                else:
                    text = next(item['text'].split('\n', 1)[1] for item in content
                                if item['type'] == 'text' and item['text'].startswith('OCR_TARGET_DATA_JSON\n'))
                    targets = json.loads(text)
                    items = [{'target_id': t['target_id'], 'reading': owner.probe if t['target_id'] == 'probe'
                              else t['before'].replace('001.00', '001.05'), 'reason': '原图字符'} for t in targets]
                    if mode == 'wrong-probe':
                        items[0]['reading'] = 'wrong'
                    if mode == 'missing-target':
                        items = []
                    if mode == 'unknown-target':
                        items[0]['target_id'] = 'other'
                    if mode == 'duplicate-target':
                        items *= 2
                    result = json.dumps({'items': items, 'summary': '图像复核'}, ensure_ascii=False)
                if mode == 'non-json':
                    result = 'invalid-json'
                if mode == 'fenced':
                    result = '```json\n' + result + '\n```'
                if self.headers.get('x-api-key'):
                    self.send({'stop_reason': 'max_tokens' if mode == 'truncated' else 'end_turn',
                        'content': [{'type': 'tool_use', 'id': 'x'}] if mode == 'tool' else [{'type': 'text', 'text': result}],
                        'echo': KEY})
                else:
                    message = {'content': result}
                    if mode == 'tool':
                        message['tool_calls'] = [{'id': 'x'}]
                    if mode == 'refusal':
                        message['refusal'] = 'no'
                    self.send({'choices': [{'finish_reason': 'length' if mode == 'truncated' else 'stop', 'message': message}], 'echo': KEY})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/gateway/v1'

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class ExternalReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = ProviderFixture()

    @classmethod
    def tearDownClass(cls):
        cls.provider.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.vault = ext.CredentialVault(self.root / 'user-secrets')
        self.connection = ext.ExternalConnection(self.store, self.vault)
        self.provider.requests.clear()
        self.provider.mode = 'normal'
        self.provider.entered.clear()
        self.provider.release.clear()
        image = Image.new('RGB', (200, 100), 'white')
        stream = io.BytesIO(); image.save(stream, format='PNG')
        self.probe_patch = patch.object(ext, 'make_probe', return_value=(stream.getvalue(), self.provider.probe))
        self.probe_patch.start(); self.addCleanup(self.probe_patch.stop)

    def body(self, protocol='openai'):
        return {'protocol': protocol, 'base_url': self.provider.url, 'api_key': KEY, 'model': 'vision-a'}

    def saved(self, protocol='openai'):
        return self.connection.save(self.body(protocol))

    def test_url_normalization_and_rejection(self):
        for source, expected in [('https://example.com', 'https://example.com/v1'),
                ('https://example.com/v1/', 'https://example.com/v1'),
                ('http://host/api/v1/chat/completions', 'http://host/api/v1'),
                ('http://host/gateway/messages', 'http://host/gateway'),
                ('http://[::1]:8000/v1/models', 'http://[::1]:8000/v1')]:
            self.assertEqual(ext.normalize_url(source), expected)
        for url in ['file:///tmp/x', 'http://user:secret@host', 'http://host?key=secret', 'http://host#key', 'http://host:bad', 'http://host/ bad', 'http://host\\x']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                ext.normalize_url(url)

    def test_models_both_protocols_headers_and_pagination_without_saving(self):
        for protocol in ['openai', 'anthropic']:
            self.provider.requests.clear()
            models = self.connection.models(self.body(protocol))
            self.assertEqual([x['id'] for x in models['models']], ['vision-a', 'vision-b'])
            self.assertFalse(self.connection.view()['configured'])
            request = self.provider.requests[0]
            if protocol == 'openai':
                self.assertEqual(request['headers']['Authorization'], 'Bearer ' + KEY)
                self.assertEqual(len(self.provider.requests), 1)
            else:
                self.assertEqual(request['headers']['x-api-key'], KEY)
                self.assertEqual(request['headers']['anthropic-version'], '2023-06-01')
                self.assertIn('after_id=vision-a', self.provider.requests[1]['path'])

    def test_probe_image_protocols_and_credentials_remain_private(self):
        for protocol in ['openai', 'anthropic']:
            value = self.saved(protocol)
            self.assertTrue(value['available'])
            request = self.provider.requests[-1]
            self.assertTrue(request['path'].endswith('/messages' if protocol == 'anthropic' else '/chat/completions'))
            payload = request['payload']
            self.assertNotIn(self.provider.probe, json.dumps(payload))
            content = payload['messages'][-1]['content']
            image = next(c for c in content if c['type'] in {'image_url', 'image'})
            encoded = image['source']['data'] if protocol == 'anthropic' else image['image_url']['url'].split(',')[1]
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(encoded))).size, (200, 100))
            self.assertNotIn('seed', payload); self.assertNotIn('chat_template_kwargs', payload)
            self.assertNotIn('response_format', payload)
            config, key = self.connection.resolve(value['model_id'])
            self.assertEqual(key, KEY)
            self.assertNotIn(KEY, json.dumps(config)); self.assertNotIn(KEY, json.dumps(value))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), str(path))

    def test_empty_key_reuse_change_endpoint_requires_key_and_clear(self):
        saved = self.saved()
        body = self.body(); body['api_key'] = ''
        self.connection.models(body)
        body['base_url'] += '/new'
        with self.assertRaisesRegex(ValueError, '重新填写'):
            self.connection.models(body)
        body = self.body('anthropic'); body['api_key'] = ''
        with self.assertRaisesRegex(ValueError, '重新填写'):
            self.connection.models(body)
        self.connection.clear()
        self.assertFalse(self.connection.view()['configured'])
        self.assertEqual(list(self.vault.root.glob('*')), [])
        with self.assertRaisesRegex(ValueError, '连接已变化'):
            self.connection.resolve(saved['model_id'])

    def test_failed_probe_does_not_overwrite_connection(self):
        saved = self.saved()
        self.provider.mode = 'wrong-probe'
        with self.assertRaisesRegex(ValueError, '未正确读出'):
            self.connection.save(self.body('anthropic'))
        self.assertEqual(self.connection.view(), saved)
        self.assertEqual(len(list(self.vault.root.glob('*'))), 1)

    def test_token_fallback_only_explicit_unsupported_error(self):
        self.provider.mode = 'legacy-tokens'
        saved = self.saved()
        self.assertEqual(len(self.provider.requests), 2)
        config, _ = self.connection.resolve(saved['model_id'])
        self.assertEqual(config['token_parameter'], 'max_tokens')
        self.assertIn('max_tokens', self.provider.requests[-1]['payload'])
        self.provider.requests.clear(); self.provider.mode = 'other-400'
        with self.assertRaises(ext.ProviderError):
            self.saved()
        self.assertEqual(len(self.provider.requests), 1)

    def test_errors_and_invalid_model_lists_are_sanitized(self):
        for status in [401, 403, 404, 405, 429, 500, 302]:
            self.provider.mode = 'status:' + str(status)
            with self.subTest(status=status), self.assertRaises(ext.ProviderError) as caught:
                self.connection.models(self.body())
            self.assertNotIn(KEY, str(caught.exception))
        self.provider.mode = 'bad-list'
        with self.assertRaisesRegex(ValueError, '模型列表格式'):
            self.connection.models(self.body())

    def test_malformed_or_incomplete_model_output_never_passes_probe(self):
        for protocol in ['openai', 'anthropic']:
            for mode in ['non-json', 'missing-target', 'unknown-target', 'duplicate-target', 'tool', 'truncated']:
                with self.subTest(protocol=protocol, mode=mode):
                    self.provider.mode = mode
                    with self.assertRaises(ValueError):
                        self.connection.save(self.body(protocol))
                    self.assertFalse(self.connection.view()['configured'])
        self.provider.mode = 'refusal'
        with self.assertRaises(ReviewProtocolError):
            self.saved()

    def test_optional_json_fence_is_accepted(self):
        self.provider.mode = 'fenced'
        self.assertTrue(self.saved()['available'])

    def test_no_network_on_catalog_or_config_reads(self):
        self.saved(); self.provider.requests.clear()
        self.connection.view(); self.connection.view()
        self.assertEqual(self.provider.requests, [])

    def test_missing_machine_credential_marks_unavailable(self):
        saved = self.saved()
        for path in self.vault.root.glob('*'):
            path.unlink()
        self.assertFalse(self.connection.view()['available'])
        with self.assertRaisesRegex(ValueError, 'Key 不可用'):
            self.connection.resolve(saved['model_id'])

    def test_dpapi_roundtrip_and_bad_ciphertext(self):
        ident = self.vault.save(KEY)
        self.assertEqual(self.vault.read(ident), KEY)
        self.vault._path(ident).write_text('{"dpapi":"YmFk"}', 'utf-8')
        with self.assertRaises(ValueError):
            self.vault.read(ident)

    def session_fixture(self, protocol='openai'):
        saved = self.saved(protocol)
        config, key = self.connection.resolve(saved['model_id'])
        image = Image.new('RGB', (900, 600), 'white')
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((20, 20, 400, 80), fill='navy')
        drawing.rectangle((450, 20, 800, 80), fill='red')
        path = self.root / 'source.png'; image.save(path)
        targets = [{'id': 'a', 'before': 'Amount 001.00', 'context': 'Amount',
                    'evidence': {'level': 'line', 'polygon': [[20, 20], [400, 20], [400, 80], [20, 80]]}},
                   {'id': 'b', 'before': 'Header', 'evidence': {'level': 'page', 'polygon': None}}]
        snapshot = {'targets': targets, 'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'config': config}
        return config, key, path, snapshot

    def test_shared_crops_evidence_and_no_key_in_artifacts(self):
        for protocol in ['openai', 'anthropic']:
            config, key, path, snapshot = self.session_fixture(protocol)
            out = self.root / protocol
            with ext.ExternalReviewSession(None, config, out, key=key) as session:
                result = session.review(path, snapshot)
            self.assertEqual(result['items'][0]['after'], 'Amount 001.05')
            self.assertEqual(result['items'][1]['decision'], 'keep')
            batch = result['evidence']['batches'][0]
            self.assertEqual(batch['crops'][0]['target_id'], 'a')
            crop = batch['crops'][0]
            self.assertEqual(crop['box'], [8, 8, 412, 92])
            with Image.open(path) as source, Image.open(out / batch['directory'] / crop['file']) as sent:
                self.assertEqual(sent.tobytes(), source.crop((8, 8, 412, 92)).tobytes())
            payload = next(r['payload'] for r in reversed(self.provider.requests)
                           if r['method'] == 'POST' and 'Amount' in json.dumps(r['payload']))
            blocks = payload['messages'][-1]['content']
            encoded = [b['source']['data'] if protocol == 'anthropic' else b['image_url']['url'].split(',')[1]
                       for b in blocks if b['type'] in {'image', 'image_url'}]
            self.assertEqual(hashlib.sha256(base64.b64decode(encoded[1])).hexdigest(), crop['sha256'])
            self.assertEqual(hashlib.sha256((out / batch['directory'] / crop['file']).read_bytes()).hexdigest(), crop['sha256'])
            self.assertEqual(result['evidence']['batches'][1]['crops'], [])
            last = self.provider.requests[-1]['payload']['messages'][-1]['content']
            self.assertEqual(sum(b['type'] in {'image', 'image_url'} for b in last), 1)
            self.assertIn('page (no reliable crop)', json.dumps(last))
            for file in out.rglob('*'):
                if file.is_file(): self.assertNotIn(KEY.encode(), file.read_bytes())
            self.assertEqual(session.key, '')

    def test_cancel_closes_http_and_stops_later_batches(self):
        config, key, path, snapshot = self.session_fixture()
        self.provider.requests.clear(); self.provider.entered.clear(); self.provider.mode = 'hold'
        cancel = threading.Event(); errors = []
        def work():
            try:
                with ext.ExternalReviewSession(None, config, self.root / 'cancel', cancel, key=key) as session:
                    session.review(path, snapshot)
            except BaseException as e:
                errors.append(e)
        worker = threading.Thread(target=work); worker.start()
        self.assertTrue(self.provider.entered.wait(3))
        cancel.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], ReviewCancelled)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertFalse((self.root / 'cancel/response.json').exists())
        self.provider.release.set()

    def test_changed_connection_stops_remaining_batches(self):
        config, _, path, snapshot = self.session_fixture()
        self.provider.requests.clear()
        output = self.root / 'changed'
        def progress(done, total):
            if done == 1:
                self.connection.clear()
        with self.connection.session(None, config, output, threading.Event()) as session:
            with self.assertRaisesRegex(ValueError, '剩余批次未发送'):
                session.review(path, snapshot, progress_callback=progress)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertFalse((output / 'response.json').exists())

    def test_request_timeout_is_bounded_and_does_not_retry(self):
        config, key, path, snapshot = self.session_fixture()
        config['limits']['request_timeout_seconds'] = 1
        self.provider.requests.clear(); self.provider.mode = 'hold'
        with self.assertRaisesRegex(ValueError, '超时'):
            with ext.ExternalReviewSession(None, config, self.root / 'timeout', key=key) as session:
                session.review(path, snapshot)
        self.assertEqual(len(self.provider.requests), 1)
        self.provider.release.set()


if __name__ == '__main__':
    unittest.main()

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ocr_workbench import multimodal_runtime as runtime


def reply(items=None, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps({
        'items': items or [{'target_id': 'a', 'reading': '100', 'reason': '图像为100'}],
        'summary': '已审校'})}}]}


class ProtocolTests(unittest.TestCase):
    targets = [{'id': 'a', 'before': '100'}]

    def test_literal_text_is_preserved(self):
        self.assertEqual(runtime.validate_reply(reply(), self.targets)['items'][0]['after'], '100')

    def test_truncated_json_is_not_keep(self):
        with self.assertRaisesRegex(runtime.ReviewProtocolError, '截断'):
            runtime.validate_reply(reply(finish='length'), self.targets)

    def test_duplicate_unknown_missing_and_extra_fields_rejected(self):
        good = {'target_id': 'a', 'reading': '100', 'reason': '清晰'}
        for items in ([good, good], [{**good, 'target_id': 'b'}], [{**good, 'confidence': .99}]):
            with self.subTest(items=items), self.assertRaises(runtime.ReviewProtocolError):
                runtime.validate_reply(reply(items), self.targets)
        raw = reply()
        raw['choices'][0]['message']['content'] = '{"items":[],"summary":""}'
        with self.assertRaises(runtime.ReviewProtocolError):
            runtime.validate_reply(raw, self.targets)

    def test_literal_comparison_preserves_spaces_zeroes_and_null_abstention(self):
        for reading, decision, after in [('100', 'keep', '100'), ('0100', 'replace', '0100'),
                                         ('100 ', 'replace', '100 '), ('', 'replace', ''),
                                         (None, 'uncertain', '100')]:
            with self.subTest(reading=reading):
                result = runtime.validate_reply(reply([{'target_id': 'a', 'reading': reading, 'reason': '图像依据'}]), self.targets)
                self.assertEqual(result['items'][0], {'target_id': 'a', 'decision': decision, 'after': after, 'reason': '图像依据'})
                self.assertEqual(result['readings'][0]['reading'], reading)

    def test_invalid_reading_types_and_legacy_labels_are_not_repaired(self):
        for reading in [100, True, [], {}, 'x' * 4001]:
            with self.subTest(reading=type(reading).__name__), self.assertRaises(runtime.ReviewProtocolError):
                runtime.validate_reply(reply([{'target_id': 'a', 'reading': reading, 'reason': '图像依据'}]), self.targets)
        with self.assertRaises(runtime.ReviewProtocolError):
            runtime.validate_reply(reply([{'target_id': 'a', 'decision': 'keep', 'after': '101', 'reason': '自相矛盾'}]), self.targets)

    def test_tools_markdown_and_non_json_rejected(self):
        for content in ['```json\n{}\n```', 'not JSON', 'null']:
            raw = reply()
            raw['choices'][0]['message']['content'] = content
            with self.assertRaises(runtime.ReviewProtocolError):
                runtime.validate_reply(raw, self.targets)
        raw = reply()
        raw['choices'][0]['message']['tool_calls'] = [{'name': 'exec'}]
        with self.assertRaises(runtime.ReviewProtocolError):
            runtime.validate_reply(raw, self.targets)

    def test_blank_reason_rejected_before_store(self):
        with self.assertRaises(runtime.ReviewProtocolError):
            runtime.validate_reply(reply([{'target_id': 'a', 'reading': '100', 'reason': ' \n\t'}]), self.targets)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_file = Path(__file__).resolve().parents[1] / 'config/multimodal-review.json'

    def tearDown(self):
        self.temp.cleanup()

    def test_locked_profiles_and_missing_assets_never_download(self):
        c = runtime.load_config(self.root, config_path=self.config_file)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network prohibited')):
            ready = runtime.review_readiness(self.root, c)
        self.assertFalse(ready['ready'])
        self.assertTrue(ready['missing'])
        self.assertEqual(len(c['profile']['revision']), 40)
        c36 = runtime.load_config(self.root, 'qwen36-35b-a3b-q4-hybrid', self.config_file)
        self.assertEqual(c36['profile']['gpu_layers'], 16)

    def test_old_queued_snapshot_cannot_silently_use_a_new_prompt(self):
        config = runtime.load_config(self.root, config_path=self.config_file)
        with patch.object(runtime, 'PROMPT_VERSION', 'future-incompatible-prompt'):
            ready = runtime.review_readiness(self.root, config)
        self.assertFalse(ready['ready'])
        self.assertIn('提示词版本', ready['reason'])

    def test_malformed_optional_profile_does_not_break_default_catalog(self):
        raw = json.loads(self.config_file.read_text('utf-8'))
        for broken in [{'model_id': 'optional'}, {'label': 'optional'}, None, 23]:
            raw['profiles']['broken'] = broken
            path = self.root / 'optional.json'
            path.write_text(json.dumps(raw), 'utf-8')
            config = runtime.load_config(self.root, config_path=path)
            status = runtime.review_readiness(self.root, config)
            self.assertEqual(status['profile_id'], 'qwen35-4b-q4')
            self.assertTrue(status['missing'])
            self.assertIn('broken', [entry['id'] for entry in status['profiles']])
            with self.assertRaises(ValueError):
                runtime.load_config(self.root, 'broken', path)

    def test_malformed_top_level_and_types_are_configuration_errors(self):
        path = self.root / 'bad-type.json'
        for raw in [None, [], {'profiles': []}, {'profiles': {'x': None}, 'schema_version': 1, 'prompt_version': runtime.PROMPT_VERSION, 'default_profile': 'x'}]:
            path.write_text(json.dumps(raw), 'utf-8')
            with self.assertRaises(ValueError):
                runtime.load_config(self.root, config_path=path)
        raw = json.loads(self.config_file.read_text('utf-8'))
        raw['profiles']['qwen35-4b-q4']['revision'] = 123
        path.write_text(json.dumps(raw), 'utf-8')
        with self.assertRaises(ValueError):
            runtime.load_config(self.root, config_path=path)

    def test_config_change_invalidates_identity(self):
        first = runtime.load_config(self.root, config_path=self.config_file)
        raw = json.loads(self.config_file.read_text('utf-8'))
        raw['limits']['batch_size'] = 2
        path = self.root / 'changed.json'
        path.write_text(json.dumps(raw), 'utf-8')
        second = runtime.load_config(self.root, config_path=path)
        self.assertNotEqual(runtime._identity(first)['config_sha256'], runtime._identity(second)['config_sha256'])

    def test_mutation_after_config_load_cannot_reuse_identity(self):
        config = runtime.load_config(self.root, config_path=self.config_file)
        config['profile']['gpu_layers'] = 0
        self.assertIn('加载后发生变化', runtime.review_readiness(self.root, config)['reason'])

    def test_unlocked_runtime_plugin_is_rejected(self):
        config = runtime.load_config(self.root, config_path=self.config_file)
        folder = self.root / 'runtimes/llama'
        folder.mkdir(parents=True)
        (folder / 'ggml-unreviewed.dll').write_bytes(b'plugin')
        self.assertIn('未锁定', runtime.review_readiness(self.root, config)['reason'])

    def test_relative_paths_and_limits_reject_unsafe_config(self):
        for path in ['../escape.gguf', 'C:/escape.gguf', '//host/x', 'models/../../x', 'https://server/model']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                runtime._relative(self.root, path)
        raw = json.loads(self.config_file.read_text('utf-8'))
        raw['limits']['context_tokens'] = 999999
        path = self.root / 'bad.json'
        path.write_text(json.dumps(raw), 'utf-8')
        with self.assertRaises(ValueError):
            runtime.load_config(self.root, config_path=path)

    def test_full_digest_detects_same_size_tampering_and_cache_invalidation(self):
        path = self.root / 'model.gguf'
        path.write_bytes(b'original')
        asset = {'bytes': 8, 'sha256': hashlib.sha256(b'original').hexdigest()}
        runtime._check_file(path, asset)
        original_stat = path.stat()
        time.sleep(.01)
        path.write_bytes(b'changed!')
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            runtime._check_file(path, asset)

    def test_digest_cancelled_before_publish(self):
        path = self.root / 'large.gguf'
        path.write_bytes(b'x' * 1024)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(runtime.ReviewCancelled):
            runtime._check_file(path, {'bytes': 1024, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, cancel)


class CroppingTests(unittest.TestCase):
    def test_valid_crop_and_unreliable_geometry(self):
        poly = [[10, 20], [30, 20], [30, 40], [10, 40]]
        self.assertEqual(runtime._crop_box({'level': 'exact', 'polygon': poly}, 100, 100), [4, 14, 36, 46])
        for level in ['approximate', 'missing', 'unreliable']:
            self.assertIsNone(runtime._crop_box({'level': level, 'polygon': poly}, 100, 100))
        for poly in [[[float('nan'), 0], [4, 8], [4, 4]], [[-1, 0], [4, 8], [4, 4]], [[0, 0], [200, 8], [4, 4]]]:
            self.assertIsNone(runtime._crop_box({'level': 'exact', 'polygon': poly}, 100, 100))

    def test_shared_page_and_labeled_crop_preserve_request_evidence(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'source.png'
            Image.new('RGB', (300, 200), 'white').save(image)
            config = runtime.load_config(root, config_path=Path(__file__).resolve().parents[1] / 'config/multimodal-review.json')
            session = runtime.ReviewSession(root, config, root / 'review')
            targets = [{'id': 'a', 'before': '100', 'evidence': {'level': 'exact', 'polygon': [[20, 20], [100, 20], [100, 60], [20, 60]]}},
                       {'id': 'b', 'before': '200', 'evidence': {'level': 'missing', 'polygon': None}}]
            def request(payload, path):
                submitted = json.loads(payload['messages'][1]['content'][-1]['text'].split('\n', 1)[1])
                raw = reply([{'target_id': t['target_id'], 'reading': t['before'], 'reason': '图像支持'} for t in submitted])
                path.write_text(json.dumps(raw), 'utf-8')
                return raw
            progress = []
            with patch.object(session, '_request', side_effect=request):
                data = session.review(image, {'targets': targets, 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest()},
                                      progress_callback=lambda done, total: progress.append((done, total)))
            self.assertEqual(len(data['items']), 2)
            self.assertEqual(data['evidence']['batches'][0]['readings'][0]['reading'], '100')
            self.assertEqual(progress, [(1, 2), (2, 2)])
            folder = root / 'review/batch-0001'
            self.assertTrue((folder / 'page.png').is_file())
            self.assertTrue((folder / 'crop-01.png').is_file())
            self.assertFalse((folder / 'crop-02.png').exists())
            second = root / 'review/batch-0002'
            self.assertTrue((second / 'page.png').is_file())
            self.assertFalse((second / 'crop-01.png').exists())
            payload = json.loads((folder / 'request.json').read_text('utf-8'))
            self.assertEqual(payload['response_format']['type'], 'json_schema')
            self.assertIn('untrusted document DATA', payload['messages'][0]['content'])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = {'body': json.dumps(reply()).encode(), 'delay': 0, 'auth': None, 'redirect': False}
        state = self.state
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                state['auth'] = self.headers.get('Authorization')
                time.sleep(state['delay'])
                try:
                    self.send_response(307 if state['redirect'] else 200)
                    if state['redirect']:
                        self.send_header('Location', 'https://example.com/')
                    self.end_headers()
                    self.wfile.write(state['body'])
                except (ConnectionError, OSError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.session = runtime.ReviewSession(self.root, {'limits': {'request_timeout_seconds': 5}}, self.root)
        self.session.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def test_loopback_auth_and_raw_response_saved(self):
        path = self.root / 'response.json'
        raw = self.session._request({'a': 1}, path)
        self.assertEqual(raw['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(self.state['auth'], 'Bearer ' + self.session.token)
        self.assertEqual(path.read_bytes(), self.state['body'])

    def test_malformed_and_oversized_reply_keeps_failure_evidence(self):
        for body in [b'<html>broken</html>', b'x' * (runtime._MAX_REPLY_BYTES + 10)]:
            self.state['body'] = body
            path = self.root / 'response.json'
            with self.assertRaises(runtime.ReviewProtocolError):
                self.session._request({}, path)
            self.assertTrue(path.is_file())

    def test_cancel_does_not_wait_for_http_timeout(self):
        self.state['delay'] = 1
        timer = threading.Timer(.1, self.session.cancel_event.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(runtime.ReviewCancelled):
            self.session._request({}, self.root / 'response.json')
        self.assertLess(time.monotonic() - started, .7)
        timer.join()
        self.assertFalse((self.root / 'response.json').exists())

    def test_server_cannot_redirect_document_data(self):
        self.state['redirect'] = True
        with self.assertRaisesRegex(RuntimeError, '重定向'):
            self.session._request({}, self.root / 'response.json')


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'scripts/prepare_multimodal_models.py'
        spec = importlib.util.spec_from_file_location('review_model_preparation', path)
        cls.preparer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.preparer)

    def test_partial_download_is_resumed_and_verified_before_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'model.gguf'
            target.with_suffix('.gguf.part').write_bytes(b'abcd')
            expected = {'bytes': 8, 'sha256': hashlib.sha256(b'abcdefgh').hexdigest()}
            response = io.BytesIO(b'efgh')
            response.status = 206
            response.headers = {'Content-Range': 'bytes 4-7/8'}
            with patch.object(self.preparer.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                self.preparer.fetch('https://example.com/model', target, expected)
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.headers['Range'], 'bytes=4-')
            self.assertEqual(target.read_bytes(), b'abcdefgh')
            self.assertFalse(target.with_suffix('.gguf.part').exists())

    def test_wrong_hash_does_not_publish_or_replace_existing_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'model.gguf'
            expected = {'bytes': 8, 'sha256': hashlib.sha256(b'abcdefgh').hexdigest()}
            response = io.BytesIO(b'wrong!!!')
            response.status = 200
            response.headers = {}
            with patch.object(self.preparer.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.assertRaisesRegex(ValueError, 'SHA256'):
                    self.preparer.fetch('https://example.com/model', target, expected)
            self.assertFalse(target.exists())
            target.write_bytes(b'existing')
            with self.assertRaisesRegex(ValueError, 'left unchanged'):
                self.preparer.fetch('https://example.com/model', target, expected)
            self.assertEqual(target.read_bytes(), b'existing')


class BundlePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'scripts/prepare_multimodal_bundle.py'
        spec = importlib.util.spec_from_file_location('review_bundle_preparation', path)
        cls.preparer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.preparer)

    def test_copy_checks_digest_and_preserves_conflicting_existing_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / 'source', root / 'new/model'
            source.write_bytes(b'locked bytes')
            expected = {'bytes': 12, 'sha256': hashlib.sha256(b'locked bytes').hexdigest()}
            self.assertTrue(self.preparer.copy_checked(source, target, expected))
            self.assertFalse(self.preparer.copy_checked(source, target, expected))
            target.write_bytes(b'conflicting!')
            with self.assertRaisesRegex(ValueError, 'left untouched'):
                self.preparer.copy_checked(source, target, expected)
            self.assertEqual(target.read_bytes(), b'conflicting!')

    def test_runtime_package_runs_directory_is_not_mistaken_for_user_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / 'runtime/openai/resources/runs/__init__.py'
            expected.parent.mkdir(parents=True)
            expected.write_text('value=1', 'utf-8')
            unwanted = root / 'runtime/__pycache__/temporary.pyc'
            unwanted.parent.mkdir(parents=True)
            unwanted.write_bytes(b'cache')
            self.assertEqual(list(self.preparer.files(root)), [expected])

    def test_unowned_existing_delivery_and_escaping_destination_are_refused(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, delivery = root / 'source', root / 'existing-delivery'
            (source / 'runtimes/service').mkdir(parents=True)
            (source / 'runtimes/service/python.exe').write_bytes(b'exe')
            (source / 'manifest.json').write_text('{"files":[]}', 'utf-8')
            delivery.mkdir()
            sentinel = delivery / 'user-data'
            sentinel.write_bytes(b'preserve')
            args = SimpleNamespace(source_bundle=source, delivery=delivery, review_models=root / 'models', profile='test')
            with self.assertRaisesRegex(ValueError, 'ownership'):
                self.preparer.ownership(args, create=True)
            self.assertEqual(sentinel.read_bytes(), b'preserve')
            with self.assertRaises(ValueError):
                self.preparer.inside(delivery, '../outside')


if __name__ == '__main__':
    unittest.main()

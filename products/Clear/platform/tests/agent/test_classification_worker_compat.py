"""Real sealed-worker processes over schema 5, without changing sealed assets."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid

from scripts.agent_native_preview import WORKER, _classification_worker_assets, _native_inputs, _resource_support
from scripts.classification_worker_compat import validate


SOURCE = Path(__file__).resolve().parents[2]
RELEASE = Path('F:/OCR/release/Clear/versions/0.10.0')
ASSET = RELEASE / 'program/bundles/classification'
RUNTIME = ASSET / 'runtimes/control/python.exe'
ROOT = Path('F:/OCR/output/clear-platform-tools-20261016/evidence/classification-compat-tests')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ClassificationCompatibilityTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.api = self.root / 'api'
        for name in ('__init__.py', 'store.py', 'settings.py'):
            target = self.api / 'src/image_platform' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(RELEASE / 'classification/src/image_platform' / name, target)
        self.asset = self.root / 'asset'
        for name in ('src', 'schemas', 'resources/third-party'):
            shutil.copytree(ASSET / name, self.asset / name, ignore=shutil.ignore_patterns('__pycache__'))
        (self.asset / 'resources/weights').mkdir(parents=True)
        shutil.copyfile(ASSET / 'resources/weights/manifest.json', self.asset / 'resources/weights/manifest.json')
        shutil.copyfile(ASSET / 'bundle.manifest.json', self.asset / 'bundle.manifest.json')
        self.adapter = self.root / 'adapter.py'
        shutil.copyfile(SOURCE / 'scripts/classification_worker_compat.py', self.adapter)
        spec = importlib.util.spec_from_file_location('compat_' + uuid.uuid4().hex, self.adapter)
        self.compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.compat)
        self.launcher = self.root / 'worker.py'
        self.launcher.write_bytes(WORKER.encode('utf-8'))
        worker_source, worker_files = _classification_worker_assets(self.asset)
        self.binding = {'app_id': 'classification', 'data_root': str(self.root / 'data'),
                        'resource_support': _resource_support(),
                        'source': str(self.api), 'asset': str(self.asset),
                        'asset_declaration_sha256': digest(self.asset / 'bundle.manifest.json'),
                        'worker_source': str(worker_source), 'worker_source_files': worker_files,
                        'worker_entry_sha256': digest(self.launcher),
                        'store_compatibility': self.compat.validate(self.api, self.asset)}
        self.save_binding()
        self.data = self.root / 'data/native/classification'
        self.data.mkdir(parents=True)
        self.env = dict(os.environ)
        for name in ('WORKBENCH_APP_ID', 'WORKBENCH_INSTANCE_ID', 'WORKBENCH_LAUNCH_ID', 'WORKBENCH_GPU_LEDGER'):
            self.env.pop(name, None)
        for key, suffix in {'TEMP': 'temp', 'TMP': 'temp', 'TMPDIR': 'temp', 'LOCALAPPDATA': 'local',
                            'APPDATA': 'appdata', 'HF_HOME': 'cache/hf', 'TORCH_HOME': 'cache/torch'}.items():
            path = self.root / suffix
            path.mkdir(parents=True, exist_ok=True)
            self.env[key] = str(path)
        self.env.update(ICP_DATA_DIR=str(self.data), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
                        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
        self.package_name = 'clear_compat_api_' + uuid.uuid4().hex
        package = types.ModuleType(self.package_name)
        package.__path__ = [str(self.api / 'src/image_platform')]
        sys.modules[self.package_name] = package
        self.api_store = __import__(self.package_name + '.store', fromlist=['Store'])
        self.api_settings = __import__(self.package_name + '.settings', fromlist=['Settings'])

    def tearDown(self):
        for name in list(sys.modules):
            if name == self.package_name or name.startswith(self.package_name + '.'):
                sys.modules.pop(name, None)
        self.temporary.cleanup()

    def save_binding(self):
        (self.root / 'binding.json').write_text(json.dumps(self.binding), encoding='utf-8')

    def store(self):
        return self.api_store.Store(self.api_settings.Settings(self.data))

    def run_worker(self, *arguments, payload=None):
        return subprocess.run([str(RUNTIME), '-X', 'utf8', '-I', '-B', '-u', str(self.launcher), *arguments],
                              cwd=self.root, env=self.env, input=payload, capture_output=True, text=True, timeout=60)

    def import_job(self, store):
        project = store.create_project('隔离适配验证', '', [{'label_id': 'receipt', 'display_name': '票据'}], 'createProject')
        images = self.root / 'inputs'
        images.mkdir(exist_ok=True)
        # Fixed valid PNG; the sealed decoder and importer process this file.
        (images / 'one.png').write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
        params = {'path': str(images), 'infer_labels': False, 'source_id': 'compatibility-proof'}
        job = store.submit(project['id'], 'import', params, key='originalImportIdentity')
        self.assertEqual(store.submit(project['id'], 'import', params, key='originalImportIdentity'), job)
        return project, job

    def test_sealed_cpu_worker_completes_schema5_import_and_preserves_manual_review(self):
        store = self.store()
        project, job = self.import_job(store)
        review_job = store.submit(project['id'], 'inference', {'device': 'cpu'}, key='reviewIdentity')
        with store.connect(write=True) as con:
            con.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (review_job,))
            con.execute("INSERT INTO job_items(id,job_id,path,sha256,state,prediction) VALUES(?,?,'review-fixture','fixed','succeeded',?)",
                        ('itemManual', review_job, json.dumps({'label_id': 'receipt', 'score': 0.94})))
        receipt = store.dispose_results(review_job, ['itemManual'], 'confirmed', 'receipt', '人工测试记录',
                    {'itemManual': 0}, operation_id='reviewOperation', request_id='reviewRequest',
                    actor={'id': 'userTest', 'display_name': '复核人', 'source': 'local'}, instance_id='instanceTest')
        result = self.run_worker('--once')
        self.assertEqual(result.returncode, 0, result.stderr)
        completed = store.job(job)
        self.assertEqual(completed['state'], 'succeeded', completed)
        self.assertEqual(completed['result']['imported'], 1)
        self.assertEqual((completed['total'], completed['done'], completed['failed']), (1, 1, 0))
        self.assertEqual(store.disposition_operation(project['id'], 'reviewOperation'), receipt)
        self.assertEqual(store.one('SELECT revision FROM result_dispositions WHERE item_id=?', ('itemManual',))['revision'], 1)
        self.assertEqual(store.one('SELECT COUNT(*) n FROM jobs WHERE id=?', (job,))['n'], 1)
        self.assertEqual(store.one('PRAGMA user_version')['user_version'], 5)

    def test_native_gated_child_uses_same_store_and_lease_checks_without_gpu_model(self):
        store = self.store()
        _, job_id = self.import_job(store)
        job = store.claim('compatibility-test-parent')
        self.assertEqual(job['id'], job_id)
        parent = self.root / 'gated_parent.py'
        parent.write_text('''import importlib.util,json,sys
from pathlib import Path
root=Path(__file__).resolve().parent
binding=json.loads((root/'binding.json').read_text('utf-8'))
sys.path.insert(0,binding['worker_source'])
spec=importlib.util.spec_from_file_location('compat',binding['store_compatibility']['adapter_path'])
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
adapter.install(binding,root/'worker.py')
from image_platform.platform_resources import run_isolated
result=run_isolated(json.loads(sys.argv[1]))
print(json.dumps({'returncode':result.returncode,'stderr':result.stderr.decode('utf-8')}))
raise SystemExit(result.returncode)
''', encoding='utf-8')
        payload = {'mode': 'job', 'data_dir': str(self.data), 'job_id': job_id, 'lease_token': job['lease_token']}
        result = subprocess.run([str(RUNTIME), '-I', '-B', '-u', str(parent), json.dumps(payload)],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(store.job(job_id)['state'], 'succeeded')
        self.assertEqual(store.job(job_id)['result']['imported'], 1)
        # Fresh verified bootstrap refuses a superseded lease before run_job.
        payload['lease_token'] = 'superseded-lease'
        result = self.run_worker('--gpu-child', '--binding-sha256', digest(self.root / 'binding.json'),
                                 payload=json.dumps(payload) + '\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('分类任务租约已更替', result.stderr)
        self.assertEqual(store.job(job_id)['state'], 'succeeded')

    def test_gpu_child_requires_payload_and_parent_binding_identity(self):
        result = self.run_worker('--gpu-child', '--binding-sha256', '0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('binding identity changed', result.stderr)
        result = self.run_worker('--gpu-child', '--binding-sha256', digest(self.root / 'binding.json'), payload='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('GPU 子进程未收到受管启动载荷', result.stderr)
        self.assertFalse((self.data / 'platform.sqlite3').exists())

    def test_schema6_is_refused_before_database_writes(self):
        database = self.data / 'platform.sqlite3'
        with sqlite3.connect(database) as con:
            con.execute('CREATE TABLE future_records(value TEXT)')
            con.execute("INSERT INTO future_records VALUES('preserved')")
            con.execute('PRAGMA user_version=6')
        con.close()
        before = database.read_bytes()
        result = self.run_worker('--once')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unsupported database version 6', result.stderr)
        self.assertEqual(database.read_bytes(), before)
        self.assertFalse((self.data / 'platform.sqlite3-wal').exists())

    def test_tampered_api_settings_adapter_and_sealed_worker_refuse_without_database(self):
        targets = [(self.api / 'src/image_platform/store.py', 'compatibility identity changed'),
                   (self.api / 'src/image_platform/settings.py', 'settings contract differs'),
                   (self.adapter, 'adapter identity changed'),
                   (self.asset / 'src/image_platform/worker.py', 'worker payload identity changed')]
        for target, reason in targets:
            with self.subTest(target=target.name):
                original = target.read_bytes()
                target.write_bytes(original + b'\n# changed identity\n')
                try:
                    result = self.run_worker('--once')
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(reason, result.stderr)
                    self.assertFalse((self.data / 'platform.sqlite3').exists())
                finally:
                    target.write_bytes(original)

    def test_compatibility_rejects_changed_worker_methods_unknown_migration_and_schema_bypass(self):
        target = self.api / 'src/image_platform/store.py'
        original = target.read_text('utf-8')
        for changed in (original.replace('def claim(self, owner):', 'def claim(self, owner):\n        return None'),
                        original.replace('ADD COLUMN actor_json TEXT', 'ADD COLUMN unexpected TEXT'),
                        original.replace('elif version != 5:', 'elif version != 6:')):
            target.write_text(changed, encoding='utf-8')
            with self.assertRaises(ValueError):
                self.compat.validate(self.api, self.asset)
        target.write_text(original, encoding='utf-8')

    def test_preview_and_package_startup_run_the_same_compatibility_preflight(self):
        before = digest(ASSET / 'bundle.manifest.json')
        inputs = _native_inputs(RELEASE / 'platform', RELEASE / 'program')
        self.assertEqual(len(inputs), 2)
        receipt = validate(RELEASE / 'classification', ASSET)
        self.assertEqual((receipt['sealed_schema_version'], receipt['schema_version']), (4, 5))
        self.assertEqual(digest(ASSET / 'bundle.manifest.json'), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)

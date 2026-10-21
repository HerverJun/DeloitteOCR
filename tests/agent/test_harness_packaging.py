"""Packaging lock closure and read-only SQLite/native smoke guard regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from scripts.harness_io_guard import io_path
from scripts.package_harness_candidate import javascript_input
from scripts.package_harness_candidate import harness_compatibility, shared_worker_identity, sha
from scripts.product_compatibility import DATA_COMPATIBILITY, check
from workbench_platform.agent.store import AgentStore
from copy import deepcopy


SOURCE = Path(__file__).resolve().parents[2]
ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/packaging-tests')
NODE = Path('F:/OCR/output/agent-harness-upgrade-20261015/dependencies/node-v24.22.0-win-x64/node.exe')


class PackagingTests(unittest.TestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_sqlite_uri_real_f_path_and_single_percent_decode(self):
        path, readonly = io_path('file:///F:/OCR/output/space%20name/percent%2520literal.sqlite3?mode=ro', sqlite=True)
        self.assertEqual(path, Path('F:/OCR/output/space name/percent%20literal.sqlite3'))
        self.assertTrue(readonly)
        self.assertTrue(io_path('file:///F:/OCR/output/read.sqlite3?immutable=1', sqlite=True)[1])
        self.assertFalse(io_path('file:///F:/OCR/output/write.sqlite3?mode=rw', sqlite=True)[1])
        with self.assertRaises(ValueError):
            io_path('file://server/share/database.sqlite3?mode=ro', sqlite=True)

    def test_python_guard_accepts_readonly_source_uri_and_rejects_source_writes(self):
        source = self.root / 'readonly source %20'
        output = self.root / 'output'
        source.mkdir(); output.mkdir()
        with sqlite3.connect(source / 'existing.sqlite3') as db:
            db.execute('CREATE TABLE values_saved(value INTEGER)')
            db.execute('INSERT INTO values_saved VALUES(42)')
        db.close()
        script = '''import json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from scripts.harness_io_guard import install_guard
source,output=map(Path,sys.argv[2:])
violations=[]
install_guard(write_roots=(output,),read_roots=(source,),violations=violations)
with sqlite3.connect((source/'existing.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
 assert db.execute('SELECT value FROM values_saved').fetchone()[0]==42
(output/'allowed.txt').write_text('allowed')
try:(source/'blocked.txt').write_text('blocked')
except RuntimeError:pass
else:raise AssertionError('Source write was not blocked')
try:sqlite3.connect(source/'blocked.sqlite3')
except RuntimeError:pass
else:raise AssertionError('Source SQLite write was not blocked')
assert len(violations)==2
print(json.dumps({'read_only_sqlite':True,'blocked_writes':len(violations)}))
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-I', '-B', '-c', script, str(SOURCE), str(source), str(output)],
                                cwd=self.root, env=dict(os.environ), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['blocked_writes'], 2)
        self.assertFalse((source / 'blocked.txt').exists())
        self.assertFalse((source / 'blocked.sqlite3').exists())

    def test_javascript_guard_denies_network_and_outside_writes_without_disabling_fsync(self):
        output = self.root / 'output'
        output.mkdir()
        script = '''const assert=require('node:assert/strict'),fs=require('node:fs'),net=require('node:net');
const output=process.argv[1],outside=process.argv[2];
assert.throws(()=>net.connect(12345,'127.0.0.1'),{code:'OFFLINE_NETWORK_BLOCKED'});
assert.throws(()=>fs.writeFileSync(outside,'blocked'),{code:'OFFLINE_WRITE_BLOCKED'});
const fd=fs.openSync(output,'wx');fs.writeSync(fd,'durable');fs.fsyncSync(fd);fs.closeSync(fd);
console.log(JSON.stringify({network_denied:true,outside_write_denied:true,fsync_preserved:true}));'''
        env = {**os.environ, 'CLEAR_HARNESS_SMOKE_OUTPUT': str(output)}
        result = subprocess.run([str(NODE), '--import', (SOURCE / 'scripts/harness_offline_guard.mjs').as_uri(), '-e', script,
                                 str(output / 'durable.txt'), str(self.root / 'blocked.txt')],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['fsync_preserved'])
        self.assertFalse((self.root / 'blocked.txt').exists())

    def test_npm_lock_rejects_changed_or_undeclared_installed_package_versions(self):
        root = self.root / 'harness'
        package = root / 'node_modules/@deepseek-ai/dsh-agent-loop'
        package.mkdir(parents=True)
        declaration = {'name': 'clear-test', 'engines': {'node': '24.22.0'},
                       'dependencies': {'@deepseek-ai/dsh-agent-loop': '0.2.1-alpha.4'}}
        lock = {'lockfileVersion': 3, 'packages': {'': {'dependencies': declaration['dependencies']},
            'node_modules/@deepseek-ai/dsh-agent-loop': {'version': '0.2.1-alpha.4', 'integrity': 'sha512-test', 'license': 'MIT'}}}
        (root / 'package.json').write_text(json.dumps(declaration), encoding='utf-8')
        (root / 'package-lock.json').write_text(json.dumps(lock), encoding='utf-8')
        (package / 'package.json').write_text(json.dumps({'name': '@deepseek-ai/dsh-agent-loop', 'version': '0.2.1-alpha.4'}), encoding='utf-8')
        # A non-package internal directory may carry a format marker; it is
        # not an extra installed dependency.
        (package / 'dist').mkdir()
        (package / 'dist/package.json').write_text('{"type":"module"}', encoding='utf-8')
        self.assertEqual(len(javascript_input(root)), 1)
        (package / 'package.json').write_text(json.dumps({'name': '@deepseek-ai/dsh-agent-loop', 'version': '0.2.1-alpha.3'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            javascript_input(root)
        (package / 'package.json').write_text(json.dumps({'name': '@deepseek-ai/dsh-agent-loop', 'version': '0.2.1-alpha.4'}), encoding='utf-8')
        (root / 'node_modules/undeclared').mkdir()
        (root / 'node_modules/undeclared/package.json').write_text('{"name":"undeclared","version":"1.0.0"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            javascript_input(root)

    def test_shared_worker_requires_pinned_sources_adjacent_provenance_and_no_extra_modules(self):
        program = self.root / 'program'
        asset = program / 'bundles/classification'
        required = ['src/image_platform/__init__.py', 'src/image_platform/settings.py',
                    'src/image_platform/cli.py', 'src/image_platform/worker.py',
                    'src/image_platform/training.py', 'resources/third-party/APACHE-2.0.txt',
                    'schemas/model-metadata.schema.json']
        for relative in required:
            path = asset / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture', encoding='utf-8')
        declaration = asset / 'bundle.manifest.json'
        declaration.write_text(json.dumps({'app_id': 'classification', 'files':
            [{'path': relative, 'sha256': sha(asset / relative)} for relative in required]}), encoding='utf-8')
        pinned = {str(declaration): sha(declaration)}
        self.assertEqual(len(shared_worker_identity(program, pinned)), 7)
        with self.assertRaises(ValueError):
            shared_worker_identity(program, {})
        (asset / required[-1]).write_text('changed', encoding='utf-8')
        with self.assertRaises(ValueError):
            shared_worker_identity(program, pinned)
        (asset / required[-1]).write_text('fixture', encoding='utf-8')
        (asset / 'src/image_platform/undeclared.py').write_text('unexpected', encoding='utf-8')
        with self.assertRaises(ValueError):
            shared_worker_identity(program, pinned)

    def test_harness_reader_and_wire_format_are_required_without_marker(self):
        sealed = {'data_compatibility': deepcopy(DATA_COMPATIBILITY)}
        sealed['data_compatibility']['reader_features'].remove('agent-harness-v1')
        sealed['data_compatibility']['records']['request_snapshot_formats'].remove('clear-harness-input-v1')
        candidate = harness_compatibility(sealed)
        self.assertNotIn('agent-harness-v1', sealed['data_compatibility']['reader_features'])
        self.assertIn('agent-harness-v1', candidate['reader_features'])
        self.assertIn('clear-harness-input-v1', candidate['records']['request_snapshot_formats'])
        store = AgentStore(self.root / 'agent')
        session = store.create_session('wfixture', 'Fixture', [], 'sessionFixture')
        store.event(session['id'], None, 'requestFixture', 'model_request',
                    {'request_snapshot': {'format': 'clear-harness-input-v1'}})
        before = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertTrue(check(self.root, candidate)['compatible'])
        no_feature = deepcopy(candidate)
        no_feature['reader_features'].remove('agent-harness-v1')
        with self.assertRaises(ValueError):
            check(self.root, no_feature)
        no_format = deepcopy(candidate)
        no_format['records']['request_snapshot_formats'].remove('clear-harness-input-v1')
        with self.assertRaises(ValueError):
            check(self.root, no_format)
        after = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_persisted_harness_state_requires_reader_before_any_model_request(self):
        store = AgentStore(self.root / 'agent')
        with store.transaction() as db:
            db.execute('CREATE TABLE harness_runs(run_id TEXT PRIMARY KEY,state TEXT,updated TEXT)')
            db.execute("INSERT INTO harness_runs VALUES('runFixture','{}','fixture')")
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
        old = deepcopy(DATA_COMPATIBILITY)
        old['reader_features'].remove('agent-harness-v1')
        with self.assertRaisesRegex(ValueError, 'Harness 会话'):
            check(self.root, old)

    def test_native_job_records_require_reader_even_without_feature_marker(self):
        records = self.root / 'gpu-requests'
        records.mkdir()
        receipt = records / 'original.json'
        receipt.write_text('{"format":"clear-native-gpu-request-v1"}', encoding='utf-8')
        before = receipt.read_bytes()
        self.assertTrue(check(self.root, DATA_COMPATIBILITY)['compatible'])
        old = deepcopy(DATA_COMPATIBILITY)
        old['reader_features'].remove('agent-multimodal-v1')
        with self.assertRaisesRegex(ValueError, '原生 GPU'):
            check(self.root, old)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse((self.root / 'product-reader-requirements.json').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Official import-only JSONL round trips, restart windows and corruption gates."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbench_platform.agent.harness_materialize import ImportProcess, materialize_legacy
from workbench_platform.agent.harness_migration import migrate_legacy
from workbench_platform.agent.providers import ProviderFault
from workbench_platform.agent.store import AgentStore, Conflict, canonical, digest
from workbench_platform.agent.tools.history import HistoryQuery, read

ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/materialization-tests')


class MaterializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.store = AgentStore(self.root / 'agent')
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE harness_runs(run_id TEXT PRIMARY KEY,state TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE harness_migrations(session_id TEXT PRIMARY KEY,history TEXT NOT NULL,manifest TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE harness_events(session_id TEXT NOT NULL,seq INTEGER NOT NULL,run_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(session_id,seq));
            ''')
        self.runtime = SimpleNamespace(store=self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self, suffix='one', status='failed'):
        session = self.store.create_session('w' + suffix, 'Legacy ' + suffix, [], 'session' + suffix)
        run, _ = self.store.start_run(session['id'], '保留原目标和约束 ' + suffix, 'request' + suffix, 1, 20, 60)
        call = {'call_id': 'call' + suffix, 'tool': 'data_export', 'arguments': {'session_id': 'dataOriginal'}}
        model, _ = self.store.begin_model(run['id'], 1, 0, digest(suffix))
        response = {'id': 'model' + suffix, 'role': 'assistant', 'content': '原过程描述', 'calls': [call],
            'private': {'reasoning_content': '原提供商续接字段'}, 'blocks': None,
            'usage': {'input_tokens': 1234, 'output_tokens': 56}, 'returned_model': 'reported-original-model'}
        self.store.finish_model(model['id'], response)
        operation, _ = self.store.begin_operation(run['id'], 1, call)
        result = {'status': 'pending' if status == 'waiting_job' else 'success', 'summary': '原成果回执',
            'data': {'artifact_id': 'artifactOriginal'}, 'artifact_refs': [{'kind': 'artifact', 'object_id': 'artifactOriginal', 'sha256': 'a' * 64}]}
        self.store.finish_operation(operation['id'], result)
        with self.store.transaction() as db:
            db.execute('UPDATE runs SET status=? WHERE id=?', (status, run['id']))
        return session, self.store.run(run['id']), response, result

    def original_records(self):
        return {table: self.store.rows('SELECT * FROM ' + table + ' ORDER BY 1') for table in
                ('sessions', 'runs', 'operations', 'model_requests', 'events', 'decisions', 'inbox')}

    async def test_all_sessions_import_and_page_without_model_tools_or_status_changes(self):
        fixtures = [self.fixture(status, status) for status in ('succeeded', 'failed', 'waiting_job')]
        empty = self.store.create_session('wempty', 'Empty old conversation', [], 'emptyLegacy')
        await migrate_legacy(self.runtime)
        originals = self.original_records()
        report = await materialize_legacy(self.runtime, page_size=2)
        self.assertEqual(report['session_count'], 4)
        self.assertEqual(report['materialized_count'], 4)
        self.assertEqual(originals, self.original_records())
        for session, run, response, result in fixtures:
            rows = self.store.rows('SELECT * FROM harness_materialized WHERE session_id=?', (session['id'],))
            self.assertEqual(len(rows), 1)
            manifest = json.loads(rows[0]['manifest'])
            self.assertTrue(manifest['materialized'])
            self.assertEqual(manifest['model_requests'], 0)
            self.assertEqual(manifest['tool_calls'], 0)
            self.assertEqual(self.store.run(run['id'])['status'], run['status'])
            model_event = self.store.rows("SELECT payload FROM harness_events WHERE session_id=? AND json_extract(payload,'$.type')='assistant/message'", (session['id'],))[0]
            original_model = json.loads(model_event['payload'])['data']['message']['source']['replayState']['response']['migration_source']['original_response']
            self.assertEqual(original_model, response)
            tool_event = self.store.rows("SELECT seq,run_id FROM harness_events WHERE session_id=? AND json_extract(payload,'$.type')='tool/result'", (session['id'],))[0]
            self.assertEqual(tool_event['run_id'], run['id'])
            queried = read(HistoryQuery(harness_seq=tool_event['seq']), SimpleNamespace(store=self.store, session_id=session['id']))
            self.assertIn('artifactOriginal', queried['data']['content'])
        self.assertEqual(len(self.store.rows('SELECT * FROM harness_materialized WHERE session_id=?', (empty['id'],))), 1)
        def no_child(*_):
            raise AssertionError('Verified imports must not spawn another child')
        repeated = await materialize_legacy(self.runtime, process_factory=no_child)
        self.assertEqual(report, repeated)

    async def test_import_only_host_refuses_agent_prompt(self):
        session, *_ = self.fixture()
        process = ImportProcess(self.runtime, session['id'])
        try:
            await process.start({'model': 'import-test', 'protocol': 'openai_chat_completions',
                'profile': {'safety_margin_tokens': 2048, 'output_reserve': 2048}, 'system': 'Read only', 'history': []})
            with self.assertRaisesRegex(ProviderFault, 'cannot execute'):
                await process.call('prompt', {'content': 'Must never run'}, timeout=5)
            self.assertEqual(process.callbacks, [])
        finally:
            await process.close()

    async def test_interrupted_paging_resumes_existing_log_without_execution(self):
        session, *_ = self.fixture()
        await migrate_legacy(self.runtime)
        originals = self.original_records()
        class Interrupted(ImportProcess):
            async def call(self, method, params, timeout=None):
                if method == 'history_events' and params['after'] > 0:
                    raise ConnectionError('Injected paging interruption')
                return await super().call(method, params, timeout)
        with self.assertRaisesRegex(ConnectionError, 'paging interruption'):
            await materialize_legacy(self.runtime, process_factory=Interrupted, page_size=2)
        self.assertEqual(self.store.rows('SELECT * FROM harness_materialized'), [])
        self.assertEqual(self.store.rows('SELECT * FROM harness_events'), [])
        self.assertEqual(originals, self.original_records())
        report = await materialize_legacy(self.runtime, page_size=2)
        self.assertEqual(report['materialized_count'], 1)
        self.assertEqual(originals, self.original_records())

    async def test_verified_page_backfill_resumes_after_database_failure(self):
        self.fixture()
        await migrate_legacy(self.runtime)
        transaction, calls = self.store.transaction, 0
        @contextmanager
        def interrupted_write():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('Injected backfill write interruption')
            with transaction() as db:
                yield db
        with patch.object(self.store, 'transaction', interrupted_write):
            with self.assertRaisesRegex(OSError, 'backfill write interruption'):
                await materialize_legacy(self.runtime, page_size=2)
        self.assertEqual(len(self.store.rows('SELECT * FROM harness_events')), 2)
        self.assertEqual(self.store.rows('SELECT * FROM harness_materialized'), [])
        committed = self.store.rows('SELECT * FROM harness_events ORDER BY seq')
        await materialize_legacy(self.runtime, page_size=2)
        self.assertEqual(self.store.rows('SELECT * FROM harness_events ORDER BY seq LIMIT 2'), committed)

    async def test_lost_provenance_is_rejected_before_backfill(self):
        self.fixture()
        await migrate_legacy(self.runtime)
        class MissingProvenance(ImportProcess):
            async def call(self, method, params, timeout=None):
                result = await super().call(method, params, timeout)
                if method == 'history_events':
                    result = deepcopy(result)
                    for event in result['items']:
                        if event['type'] == 'user/message':
                            event['data']['source'].pop('migration_source', None)
                return result
        with self.assertRaisesRegex(Conflict, 'provenance'):
            await materialize_legacy(self.runtime, process_factory=MissingProvenance)
        self.assertEqual(self.store.rows('SELECT * FROM harness_events'), [])
        self.assertEqual(self.store.rows('SELECT * FROM harness_materialized'), [])
        self.assertEqual((await materialize_legacy(self.runtime))['materialized_count'], 1)

    async def test_registered_jsonl_and_event_corruption_fail_closed(self):
        self.fixture()
        await migrate_legacy(self.runtime)
        report = await materialize_legacy(self.runtime)
        path = self.store.root / report['sessions'][0]['jsonl']['path']
        original = path.read_bytes()
        path.write_bytes(original.replace(b'legacy-import', b'legacy-tamper', 1))
        with self.assertRaisesRegex(Conflict, 'prefix hash'):
            await materialize_legacy(self.runtime)
        path.write_bytes(original)
        with self.store.transaction() as db:
            db.execute("UPDATE harness_events SET payload='{}' WHERE seq=0")
        with self.assertRaisesRegex(Conflict, 'projection was changed'):
            await materialize_legacy(self.runtime)

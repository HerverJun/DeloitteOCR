"""Real SQLite/WAL evidence conversion and scoped immutable history retrieval."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbench_platform.agent.harness_migration import (
    checkpoint_states, legacy_snapshot, migrate_legacy, paired_history, validate_migration, _sha)
from workbench_platform.agent.store import AgentStore, Conflict, canonical, digest, now
from workbench_platform.agent.tools.history import HistoryQuery, read

ROOT = Path('F:/OCR/output/agent-harness-upgrade-20261015/evidence/migration-tests')


def tool_result(status='success', label='已核实'):
    return {'status': status, 'summary': label, 'data': {'rows': 100000},
            'artifact_refs': [{'kind': 'artifact', 'object_id': 'artifactExisting', 'sha256': 'a' * 64}],
            'job_refs': [{'kind': 'job', 'object_id': 'jobExisting'}] if status == 'pending' else []}


def call(identifier='callExisting'):
    return {'call_id': identifier, 'tool': 'data_export', 'arguments': {'session_id': 'dataExisting'}}


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.store = AgentStore(self.root)
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE harness_runs(run_id TEXT PRIMARY KEY,state TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE harness_migrations(session_id TEXT PRIMARY KEY,history TEXT NOT NULL,manifest TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE harness_events(session_id TEXT NOT NULL,seq INTEGER NOT NULL,run_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(session_id,seq));
            ''')
        self.runtime = SimpleNamespace(store=self.store)
        self.keepers = []

    def tearDown(self):
        for db in self.keepers:
            db.close()
        self.temporary.cleanup()

    def run_fixture(self, suffix, status='succeeded', *, response=True, budget=(20, 60)):
        session = self.store.create_session('w' + suffix, 'Original ' + suffix, [], 'session' + suffix)
        run, _ = self.store.start_run(session['id'], '原目标 ' + suffix, 'request' + suffix, 1,
                                      round_limit=budget[0], tool_limit=budget[1])
        original_call = call('call' + suffix)
        message = {'id': 'response' + suffix, 'role': 'assistant', 'content': '原模型输出 ' + suffix,
                   'calls': [original_call], 'usage': {'input_tokens': 1000, 'output_tokens': 20}}
        if response:
            model, _ = self.store.begin_model(run['id'], 1, 0, digest(suffix))
            self.store.finish_model(model['id'], message)
        with self.store.transaction() as db:
            db.execute('UPDATE runs SET status=? WHERE id=?', (status, run['id']))
        return session, self.store.run(run['id']), message

    def checkpoint(self, run, state, *, identifier='0001', namespace=''):
        from workbench_platform.agent.dependencies import load_support_dependencies
        load_support_dependencies()
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        kind, payload = JsonPlusSerializer(pickle_fallback=False).dumps_typed({'channel_values': state})
        db = sqlite3.connect(self.root / 'checkpoints.sqlite3')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE IF NOT EXISTS checkpoints(thread_id TEXT,checkpoint_ns TEXT,checkpoint_id TEXT,type TEXT,checkpoint BLOB)')
        db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)', (run['id'], namespace, identifier, kind, payload))
        db.commit()
        self.keepers.append(db)

    def migration(self, session):
        return self.store.rows('SELECT * FROM harness_migrations WHERE session_id=?', (session['id'],))[0]

    def test_pairs_real_receipts_and_keeps_original_immutable(self):
        original_call = call()
        pending = tool_result('pending')
        settled = tool_result()
        messages = [{'id': 'model', 'role': 'assistant', 'content': '', 'calls': [original_call]},
                    {'id': 'tool', 'role': 'tool', 'call_id': original_call['call_id'], 'result': pending}]
        untouched = deepcopy(messages)
        operations = [{'call_id': original_call['call_id'], 'tool': original_call['tool'],
                       'arguments': canonical(original_call['arguments']), 'result': canonical(settled)}]
        converted = paired_history(messages, operations, 'rOriginal')
        self.assertEqual(messages, untouched)
        self.assertEqual(converted[-1]['result'], settled)
        self.assertEqual(converted[-1]['id'], 'tool')
        self.assertEqual(converted[-1]['migration_source']['original_result_sha256'], digest(pending))
        self.assertFalse(converted[-1]['migration_source']['replayed'])
        self.assertEqual(paired_history(messages[:1], operations, 'rOriginal')[-1]['result'], settled)

    def test_unanswered_calls_are_not_fabricated_successes_or_reissued(self):
        first, second = call('intent'), call('unsubmitted')
        messages = [{'id': 'model', 'role': 'assistant', 'content': '', 'calls': [first, second]}]
        operation = {'call_id': first['call_id'], 'tool': first['tool'], 'arguments': canonical(first['arguments']), 'result': None}
        paired = paired_history(messages, [operation], 'rOriginal')
        self.assertEqual([item['result']['status'] for item in paired[1:]], ['needs_verification', 'error'])
        self.assertEqual([item['result']['error']['code'] for item in paired[1:]], ['response_unknown', 'not_started'])
        recorded_rejection = {'status': 'error', 'summary': 'Original batch validation rejected'}
        self.assertEqual(paired_history(messages, [], 'rOriginal', {'intent': recorded_rejection})[1]['result'], recorded_rejection)
        with self.assertRaises(Conflict):
            paired_history([*messages, {'role': 'tool', 'call_id': 'other', 'result': tool_result()}], [], 'rOriginal')
        conflict = deepcopy(operation)
        conflict['arguments'] = '{}'
        with self.assertRaises(Conflict):
            paired_history(messages, [conflict], 'rOriginal')

    async def test_all_states_ids_hashes_wal_and_default_budget_migrate_once(self):
        fixtures = []
        for status in ('succeeded', 'answered', 'failed', 'cancelled', 'running', 'waiting_job', 'waiting_user', 'needs_verification', 'paused'):
            session, run, message = self.run_fixture(status, status, budget=(21, 61) if status == 'failed' else (20, 60))
            state = {'messages': [{'id': run['request_id'], 'role': 'user', 'content': '原目标 ' + status}, message],
                     'step': 1, 'visible': ['data_export'], 'calls': message['calls'], 'cursor': 0}
            self.checkpoint(run, state)
            fixtures.append((session, run, message))
        checkpoint_path = self.root / 'checkpoints.sqlite3'
        checkpoint_before = _sha(checkpoint_path)
        await migrate_legacy(self.runtime)
        snapshot_root = self.root / 'harness/legacy/snapshot-v2'
        snapshot_bytes = {path.name: path.read_bytes() for path in snapshot_root.iterdir() if path.is_file()}
        self.assertTrue(json.loads(snapshot_bytes['snapshot.json'])['databases']['checkpoints.sqlite3']['source_wal_present'])
        for session, run, message in fixtures:
            history, manifest = validate_migration(self.migration(session))
            self.assertEqual(manifest['run_statuses'][run['id']], run['status'])
            self.assertTrue(any(item.get('id') == message['id'] for item in history))
            self.assertEqual(self.store.run(run['id'])['status'], run['status'])
            projected = self.store.rows('SELECT state FROM harness_runs WHERE run_id=?', (run['id'],))
            if run['status'] in {'running', 'waiting_job', 'waiting_user', 'needs_verification', 'paused'}:
                self.assertEqual(len(projected), 1)
                self.assertTrue(json.loads(projected[0]['state'])['entered'])
            else:
                self.assertEqual(projected, [])
            self.assertEqual(self.store.run(run['id'])['round_limit'], 21 if run['status'] == 'failed' else None)
        self.assertEqual(_sha(checkpoint_path), checkpoint_before)
        self.assertEqual(list(snapshot_root.glob('*.sqlite3-wal')), [])
        self.assertEqual(list(snapshot_root.glob('*.sqlite3-shm')), [])
        fresh = self.store.create_session('wfreshHarness', 'Fresh Harness', [], 'freshHarnessSession')
        await migrate_legacy(self.runtime)
        self.assertEqual(self.store.rows('SELECT * FROM harness_migrations WHERE session_id=?', (fresh['id'],)), [])
        self.assertEqual(snapshot_bytes, {path.name: path.read_bytes() for path in snapshot_root.iterdir() if path.is_file()})
        report = json.loads((self.root / 'harness/legacy/migration-v2.json').read_text('utf-8'))
        self.assertEqual(report['session_count'], 9)
        self.assertEqual(report['converted_count'], 9)

    async def test_no_checkpoint_reconstructs_model_and_real_operation_history(self):
        session, run, message = self.run_fixture('noCheckpoint', 'running')
        operation, _ = self.store.begin_operation(run['id'], run['generation'], message['calls'][0])
        pending = tool_result('pending')
        self.store.finish_operation(operation['id'], pending)
        unknown, _ = self.store.begin_model(run['id'], run['generation'], 1, digest('unknown-model'))
        await migrate_legacy(self.runtime)
        history, manifest = validate_migration(self.migration(session))
        imported = next(item for item in history if item['role'] == 'assistant')
        self.assertEqual(imported['migration_source']['original_response'], message)
        self.assertEqual({key: value for key, value in imported.items() if key != 'migration_source'}, message)
        self.assertEqual(next(item for item in history if item['role'] == 'tool')['result'], pending)
        self.assertEqual(manifest['runs'][0]['unanswered_model_requests'], [unknown['id']])
        projected = json.loads(self.store.rows('SELECT state FROM harness_runs WHERE run_id=?', (run['id'],))[0]['state'])
        self.assertEqual(projected['calls'], message['calls'])
        self.assertEqual(projected['results'], [pending])
        self.assertEqual(len(self.store.rows('SELECT * FROM operations')), 1)
        self.assertEqual(self.store.rows('SELECT state FROM model_requests WHERE id=?', (unknown['id'],))[0]['state'], 'sent')

    async def test_first_snapshot_excludes_existing_harness_sessions(self):
        legacy, _, _ = self.run_fixture('legacyOnly', 'failed')
        owned, owned_run, _ = self.run_fixture('nativeHarness', 'running')
        marked = self.store.create_session('wMarkedHarness', 'Empty Harness', [], 'emptyHarnessSession')
        with self.store.transaction() as db:
            db.execute('CREATE TABLE harness_sessions(session_id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO harness_sessions VALUES(?)', (marked['id'],))
            db.execute('INSERT INTO harness_runs VALUES(?,?,?)', (owned_run['id'], canonical({'run_id': owned_run['id']}), now()))
        await migrate_legacy(self.runtime)
        self.assertEqual([row['session_id'] for row in self.store.rows('SELECT session_id FROM harness_migrations')], [legacy['id']])
        self.assertEqual(self.store.rows('SELECT history FROM harness_migrations WHERE session_id=?', (owned['id'],)), [])
        self.assertEqual(self.store.run(owned_run['id'])['round_limit'], 20)

    async def test_orphan_operation_keeps_authoritative_refs_without_invented_model_call(self):
        session, run, _ = self.run_fixture('orphan', 'running', response=False)
        operation, _ = self.store.begin_operation(run['id'], run['generation'], call('orphanCall'))
        self.store.finish_operation(operation['id'], tool_result())
        await migrate_legacy(self.runtime)
        history, _ = validate_migration(self.migration(session))
        self.assertFalse(any(item['role'] == 'assistant' for item in history))
        evidence = next(item for item in history if item.get('migration_source', {}).get('kind') == 'orphan_operation')
        self.assertIn('artifactExisting', evidence['content'])
        self.assertEqual(evidence['migration_source']['operation_id'], operation['id'])

    async def test_corrupted_archive_and_import_cannot_be_silently_replaced(self):
        session, _, _ = self.run_fixture('corruption')
        await migrate_legacy(self.runtime)
        with self.store.transaction() as db:
            db.execute("UPDATE harness_migrations SET history='[]' WHERE session_id=?", (session['id'],))
        with self.assertRaisesRegex(Conflict, 'history hash'):
            await migrate_legacy(self.runtime)
        record = self.root / 'harness/legacy/snapshot-v2/operations.sqlite3'
        with record.open('ab') as stream:
            stream.write(b'corruption')
        corrupt_hash = _sha(record)
        with self.assertRaisesRegex(Conflict, 'snapshot hash'):
            await migrate_legacy(self.runtime)
        self.assertEqual(_sha(record), corrupt_hash)

    async def test_resume_after_partial_conversion_and_detect_changed_active_run(self):
        first, _, _ = self.run_fixture('first', 'failed')
        second, run, _ = self.run_fixture('second', 'running')
        legacy_snapshot(self.store)
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET status='paused' WHERE id=?", (run['id'],))
        with self.assertRaisesRegex(Conflict, 'changed after'):
            await migrate_legacy(self.runtime)
        original = self.migration(first)
        self.assertEqual(self.store.rows('SELECT * FROM harness_migrations WHERE session_id=?', (second['id'],)), [])
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET status='running' WHERE id=?", (run['id'],))
        await migrate_legacy(self.runtime)
        self.assertEqual(self.migration(first), original)
        self.assertEqual(validate_migration(self.migration(second))[1]['run_count'], 1)

    async def test_draft_migration_upgrades_from_original_evidence(self):
        session, _, message = self.run_fixture('draftUpgrade', 'failed')
        incomplete = [{'id': 'draft-only', 'role': 'user', 'content': 'Draft projection'}]
        draft = {'version': 'clear-harness-migration-v1', 'session_id': session['id'],
                 'message_count': 1, 'history_sha256': digest(incomplete)}
        with self.store.transaction() as db:
            db.execute('INSERT INTO harness_migrations VALUES(?,?,?,?)', (session['id'], canonical(incomplete), canonical(draft), now()))
        await migrate_legacy(self.runtime)
        history, manifest = validate_migration(self.migration(session))
        self.assertEqual(manifest['version'], 'clear-harness-migration-v2')
        self.assertTrue(any(item['id'] == message['id'] for item in history))
        archived = self.root / 'harness/legacy/snapshot-v2/operations.sqlite3'
        from workbench_platform.agent.harness_migration import readonly
        with closing(readonly(archived)) as db:
            self.assertEqual(json.loads(db.execute('SELECT history FROM harness_migrations').fetchone()[0]), incomplete)

    async def test_manual_return_keeps_references_between_its_original_runs(self):
        session, first, message = self.run_fixture('manualChronology', 'failed')
        refs = [{'kind': 'artifact', 'object_id': 'originalManualArtifact', 'sha256': 'b' * 64}]
        self.store.event(session['id'], None, 'manualReturnOriginal', 'message',
                         {'role': 'user', 'content': '人工补充结果', 'references': refs})
        second, _ = self.store.start_run(session['id'], '继续原任务', 'afterManualOriginal', 1, round_limit=20, tool_limit=60)
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET created='2026-01-01T00:00:00Z' WHERE id=?", (first['id'],))
            db.execute("UPDATE events SET created='2026-01-01T00:00:00Z' WHERE run_id=?", (first['id'],))
            db.execute("UPDATE model_requests SET created='2026-01-01T00:00:01Z' WHERE run_id=?", (first['id'],))
            db.execute("UPDATE events SET created='2026-01-02T00:00:00Z' WHERE identity='manualReturnOriginal'")
            db.execute("UPDATE runs SET status='failed',created='2026-01-03T00:00:00Z' WHERE id=?", (second['id'],))
            db.execute("UPDATE events SET created='2026-01-03T00:00:00Z' WHERE run_id=?", (second['id'],))
        await migrate_legacy(self.runtime)
        history, _ = validate_migration(self.migration(session))
        identities = [item['id'] for item in history]
        self.assertLess(identities.index(message['id']), identities.index('manualReturnOriginal'))
        self.assertLess(identities.index('manualReturnOriginal'), identities.index('afterManualOriginal'))
        imported = next(item for item in history if item['id'] == 'manualReturnOriginal')
        self.assertEqual(imported['migration_source']['original_payload']['references'], refs)

    async def test_bound_user_reply_resolves_legacy_stale_needs_user_receipt(self):
        session, run, message = self.run_fixture('answeredDecision', 'running')
        original = {'status': 'needs_user', 'summary': '原问题', 'data': {'decision_id': 'dOriginal'}}
        operation, _ = self.store.begin_operation(run['id'], run['generation'], message['calls'][0])
        self.store.finish_operation(operation['id'], original)
        settled = {**original, 'status': 'success', 'data': {**original['data'], 'reply': '原明确回答'}}
        with self.store.transaction() as db:
            db.execute('INSERT INTO decisions(id,run_id,question,revision,reply,request_id,kind) VALUES(?,?,?,?,?,?,?)',
                ('dOriginal', run['id'], '原问题', 1, '原明确回答', 'originalReplyRequest', 'clarification'))
        self.checkpoint(run, {'messages': [message, {'id': 'originalReplyTool', 'role': 'tool',
            'call_id': message['calls'][0]['call_id'], 'result': settled}]})
        await migrate_legacy(self.runtime)
        history, _ = validate_migration(self.migration(session))
        imported = next(item for item in history if item['role'] == 'tool')
        self.assertEqual(imported['result'], settled)
        self.assertEqual(imported['migration_source']['decision']['request_id'], 'originalReplyRequest')
        self.assertEqual(json.loads(self.store.rows('SELECT result FROM operations WHERE id=?', (operation['id'],))[0]['result']), original)

    def test_snapshot_rejects_concurrent_source_change_and_retains_rejected_evidence(self):
        session, _, _ = self.run_fixture('changingSource', 'failed')
        from workbench_platform.agent import harness_migration
        original = harness_migration._logical_digest
        calls = 0
        def concurrent_change(path, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.store.event(session['id'], None, 'concurrentUser', 'message', {'role': 'user', 'content': 'Concurrent update'})
            return original(path, **kwargs)
        with patch.object(harness_migration, '_logical_digest', side_effect=concurrent_change):
            with self.assertRaisesRegex(Conflict, 'changed during snapshot'):
                legacy_snapshot(self.store)
        self.assertFalse((self.root / 'harness/legacy/snapshot-v2').exists())
        self.assertEqual(len(list((self.root / 'harness/legacy').glob('snapshot-pending-*/rejected.json'))), 1)
        self.assertEqual(self.store.rows('SELECT * FROM harness_migrations'), [])

    def test_checkpoint_reader_selects_latest_default_namespace_without_execution(self):
        _, run, _ = self.run_fixture('namespaces')
        self.checkpoint(run, {'messages': [{'role': 'user', 'content': 'Old'}]}, identifier='0001')
        latest = {'messages': [{'role': 'user', 'content': 'Latest'}]}
        self.checkpoint(run, latest, identifier='0002')
        self.checkpoint(run, {'messages': []}, identifier='9999', namespace='other:subgraph')
        self.assertEqual(checkpoint_states(self.root / 'checkpoints.sqlite3')[run['id']], latest)

    def harness_event(self, session, seq, kind, data):
        event = {'seq': seq, 'type': kind, 'data': data}
        with self.store.transaction() as db:
            db.execute('INSERT INTO harness_events VALUES(?,?,?,?,?)', (session['id'], seq, 'rArchived', canonical(event), digest(event)))
        return event

    def test_model_tool_history_pages_exact_original_with_scoping_and_hash(self):
        session, run, _ = self.run_fixture('history')
        foreign = self.store.create_session('wforeign', 'Foreign', [], 'foreignSession')
        self.harness_event(session, 1, 'assistant/message', {'message': {'id': 'modelOriginal', 'content': ['原文' * 4000]}})
        original = self.harness_event(session, 2, 'tool/result', {'message': {'id': 'toolOriginal', 'result': tool_result()}})
        self.harness_event(session, 3, 'compaction/committed', {'summary': '摘要不替换完整原文'})
        self.harness_event(foreign, 88, 'tool/result', {'secret': '其他会话机密'})
        ctx = SimpleNamespace(store=self.store, session_id=session['id'])
        public = read(HistoryQuery(limit=1), ctx)['data']
        self.assertEqual(public['items'][0]['event_seq'], self.store.rows('SELECT seq FROM events WHERE run_id=?', (run['id'],))[0]['seq'])
        self.assertEqual(public['harness_history']['items'][0]['harness_seq'], 1)
        self.assertEqual(public['harness_history']['next_after_harness'], 1)
        directory = read(HistoryQuery(after_harness=1), ctx)['data']
        self.assertEqual([item['harness_seq'] for item in directory['items']], [2, 3])
        chunks, offset, hashes = [], 0, []
        while True:
            data = read(HistoryQuery(harness_seq=2, offset=offset, chars=100), ctx)['data']
            chunks.append(data['content'])
            hashes.append(data['content_sha256'])
            offset = data['next_offset']
            if offset is None:
                break
        self.assertEqual(''.join(chunks), canonical(original))
        self.assertEqual(set(hashes), {digest(original)})
        with self.assertRaisesRegex(ValueError, '不属于'):
            read(HistoryQuery(harness_seq=88), ctx)
        with self.store.transaction() as db:
            db.execute("UPDATE harness_events SET payload='{}' WHERE session_id=? AND seq=2", (session['id'],))
        with self.assertRaisesRegex(Conflict, '校验失败'):
            read(HistoryQuery(harness_seq=2), ctx)
        with self.assertRaises(ValueError):
            HistoryQuery(event_seq=1, harness_seq=2)

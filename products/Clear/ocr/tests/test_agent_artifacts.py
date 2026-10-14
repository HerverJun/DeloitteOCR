import json
from pathlib import Path
from types import SimpleNamespace
import time
import unittest

import test_agent_policy
from ocr_workbench.agent.artifacts import Artifacts, file_hash
from ocr_workbench.agent.contracts import ExportResults
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.policy import PolicyDenied
from ocr_workbench.application_services import ApplicationServices
from ocr_workbench.store import Conflict


class ArtifactTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def prepare(self):
        task = self.store.enqueue(self.project, [self.photo["active_version"]], ["glm"])[0]
        self.store.claim()
        self.store.complete(task, {"text": "原始金额 42", "tables": [], "blocks": [], "engine": "glm"})
        result_id = self.store.one("tasks", task)["result_id"]
        args = ExportResults(source="explicit_results", format="txt", results=[{"result_id": result_id, "revision": 0, "version_id": self.photo["active_version"]}])
        self.policy.grant(self.project, "scoped_new_artifact", {"document_ids": [self.photo["id"]], "page_ids": [self.photo["id"]],
            "version_ids": [self.photo["active_version"]], "result_ids": [result_id], "format": "txt", "partial_policy": "ask"},
            source="user_request", expires=time.time() + 60, run_id=self.run["id"])
        self.agent.record_calls(self.project, self.run["id"], 1, 0, [{"call_id": "export", "tool": "export_results", "arguments": args.model_dump(exclude_none=True)}])
        context = {"run": self.run, "generation": 1, "step": 0, "call_id": "export"}
        services = ApplicationServices(self.store, None, SimpleNamespace(guard=self.store.file_lock))
        artifacts = Artifacts(self.agent, services, Operations(self.agent, self.policy))
        return artifacts, args, context

    def partial(self):
        artifacts, args, context = self.prepare()
        ref = args.results[0].model_dump()
        with self.store.transaction() as db:
            db.execute("INSERT INTO agent_operations VALUES('source',?,?,?, ?,1,'finished',?,NULL,'now','now')",
                (self.project, self.run['id'], 'source', 'hash', json.dumps({'results': [ref], 'coverage': {'uncovered': [
                    {'page_id': self.photo['id'], 'document_id': self.photo['id'], 'page_number': 1}]}})))
        self.policy.grant(self.project, 'scoped_new_artifact', {'source_run_id': self.run['id'], 'format': 'txt', 'partial_policy': 'ask'},
            source='user_request', expires=time.time() + 60, run_id=self.run['id'])
        args = ExportResults(source='run_results', source_run_id=self.run['id'], format='txt')
        return artifacts, args, {**context, 'step': 1, 'call_id': 'partial'}

    def test_empty_workbook_is_business_error_and_text_fallback_remains_usable(self):
        artifacts, args, context = self.prepare()
        with self.store.transaction() as db:
            scope = json.loads(db.execute("SELECT scope FROM agent_grants WHERE permission='scoped_new_artifact'").fetchone()[0])
        scope['format'] = 'xlsx'
        self.policy.grant(self.project, 'scoped_new_artifact', scope, source='user_request', expires=time.time() + 60, run_id=self.run['id'])
        workbook = args.model_copy(update={'format': 'xlsx'})
        self.agent.record_calls(self.project, self.run['id'], 1, 1, [{'call_id': 'workbook', 'tool': 'export_results', 'arguments': workbook.model_dump(exclude_none=True)}])
        with self.assertRaises(PolicyDenied) as caught:
            artifacts.export(workbook, {**context, 'step': 1, 'call_id': 'workbook'})
        self.assertEqual(caught.exception.code, 'business_failed')
        self.assertIn('没有结构化表格', str(caught.exception))
        self.assertEqual(self.store.rows('SELECT state FROM agent_artifacts')[0]['state'], 'failed')
        result = artifacts.export(args, context)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(self.store.rows('SELECT state FROM agent_operations WHERE id=(SELECT operation_id FROM agent_artifacts WHERE state=\'ready\')')[0]['state'], 'finished')
        self.assertIn('42', artifacts.verify(self.project, result['artifact_refs'][0]['artifact_id']).read_text('utf-8-sig'))

    def test_partial_export_waits_for_bound_decision_and_rejects_changed_manifest(self):
        artifacts, args, context = self.partial()
        self.agent.record_calls(self.project, self.run['id'], 1, 1, [{'call_id': 'partial', 'tool': 'export_results', 'arguments': args.model_dump(exclude_none=True)}])
        result = artifacts.export(args, context)
        self.assertEqual(result['status'], 'needs_user')
        self.assertFalse(self.store.rows('SELECT * FROM agent_artifacts'))
        decision = self.store.rows('SELECT * FROM agent_decisions')[0]
        self.agent.reply_decision(self.project, self.run['id'], decision['id'], {'client_request_id': 'yes', 'payload_hash': decision['payload_hash'], 'option_id': 'allow'})
        # Changing the missing-page set invalidates the prior consent, even with
        # the same result revision and provider call ID.
        with self.store.transaction() as db:
            old = json.loads(db.execute("SELECT result FROM agent_operations WHERE id='source'").fetchone()[0])
            old['coverage']['uncovered'].append({'page_id': 'another-page', 'document_id': self.photo['id'], 'page_number': 2})
            db.execute("UPDATE agent_operations SET result=? WHERE id='source'", (json.dumps(old),))
        result = artifacts.export(args, context)
        self.assertEqual(result['status'], 'needs_user')
        self.assertNotEqual(result['data']['decision_id'], decision['id'])
        self.assertFalse(self.store.rows('SELECT * FROM agent_artifacts'))

    def test_partial_export_consent_does_not_survive_result_revision_change(self):
        artifacts, args, context = self.partial()
        self.agent.record_calls(self.project, self.run['id'], 1, 1, [{'call_id': 'partial', 'tool': 'export_results', 'arguments': args.model_dump(exclude_none=True)}])
        artifacts.export(args, context)
        decision = self.store.rows('SELECT * FROM agent_decisions')[0]
        result_id = self.store.rows('SELECT id FROM results')[0]['id']
        self.store.save(result_id, {'text': 'edited', 'tables': []}, 0)
        with self.assertRaises(Conflict):
            self.agent.reply_decision(self.project, self.run['id'], decision['id'], {'client_request_id': 'yes', 'payload_hash': decision['payload_hash'], 'option_id': 'allow'})
        self.agent.reply_decision(self.project, self.run['id'], decision['id'], {'client_request_id': 'no', 'payload_hash': decision['payload_hash'], 'option_id': 'return'})

    def test_partial_file_and_downloadable_coverage_manifest_match_published_hash(self):
        artifacts, args, context = self.partial()
        self.agent.record_calls(self.project, self.run['id'], 1, 1, [{'call_id': 'partial', 'tool': 'export_results', 'arguments': args.model_dump(exclude_none=True)}])
        self.assertEqual(artifacts.export(args, context)['status'], 'needs_user')
        decision = self.store.rows('SELECT * FROM agent_decisions')[0]
        self.agent.reply_decision(self.project, self.run['id'], decision['id'],
                                  {'client_request_id': 'allow', 'payload_hash': decision['payload_hash'], 'option_id': 'allow'})
        def crash(window):
            if window == 'after_publish':
                raise RuntimeError('synthetic interruption before ready commit')
        with self.assertRaises(RuntimeError):
            artifacts.export(args, context, fault=crash)
        result = artifacts.export(args, context)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('仅包含成功页', result['summary'])
        key = result['artifact_refs'][0]['artifact_id']
        target = artifacts.verify(self.project, key)
        self.assertEqual(target.name, 'export-partial.txt')
        self.assertIn('42', target.read_text('utf-8-sig'))
        lease, sidecar = artifacts.coverage_manifest(self.project, key)
        try:
            receipt = json.loads(sidecar.read_text('utf-8'))
            row = artifacts.get(self.project, key)
            self.assertEqual(receipt['file'], target.name)
            self.assertEqual(receipt['sha256'], file_hash(target))
            self.assertEqual(receipt['sha256'], row['sha256'])
            self.assertEqual(receipt['bytes'], target.stat().st_size)
            self.assertEqual(receipt['manifest'], json.loads(row['manifest']))
            self.assertEqual(receipt['manifest']['partial_authorization'], decision['id'])
            self.assertEqual(len(receipt['manifest']['coverage']['failed_pages']), 1)
            self.assertEqual(artifacts.export(args, context)['artifact_refs'][0]['artifact_id'], key)
        finally:
            artifacts.release(lease)
        with self.assertRaises(KeyError):
            artifacts.coverage_manifest(self.other, key)
        target.write_bytes(b'changed after publication')
        with self.assertRaises(PolicyDenied):
            artifacts.coverage_manifest(self.project, key)
        self.assertEqual(artifacts.get(self.project, key)['state'], 'corrupt')

    def test_complete_file_remains_original_format_without_partial_sidecar(self):
        artifacts, args, context = self.prepare()
        result = artifacts.export(args, context)
        self.assertEqual(result['status'], 'success')
        key = result['artifact_refs'][0]['artifact_id']
        target = artifacts.verify(self.project, key)
        self.assertEqual(target.name, 'export.txt')
        self.assertEqual(artifacts.get(self.project, key)['sha256'], file_hash(target))
        with self.assertRaises(PolicyDenied):
            artifacts.coverage_manifest(self.project, key)
        with self.store.transaction() as db:
            db.execute('UPDATE agent_artifacts SET relative_path=? WHERE id=?', ('agent-artifacts/wrong/export.txt', key))
        with self.assertRaises(PolicyDenied):
            artifacts.verify(self.project, key)
        self.assertEqual(artifacts.get(self.project, key)['state'], 'corrupt')

    def test_publish_crash_reconciles_without_duplicate_and_download_lease(self):
        artifacts, args, context = self.prepare()
        def crash(window):
            if window == "after_publish":
                raise RuntimeError("lost ready commit")
        with self.assertRaises(RuntimeError):
            artifacts.export(args, context, fault=crash)
        row = self.store.rows("SELECT * FROM agent_artifacts")[0]
        self.assertEqual(row["state"], "staging")
        self.assertEqual(self.store.rows('SELECT state FROM agent_operations WHERE id=?', (row['operation_id'],))[0]['state'], 'submitted')
        output = artifacts.export(args, context)
        key = output["artifact_refs"][0]["artifact_id"]
        self.assertEqual(key, row["id"])
        self.assertEqual(len(self.store.rows("SELECT * FROM agent_artifacts")), 1)
        self.assertEqual(self.store.rows('SELECT state FROM agent_operations WHERE id=?', (row['operation_id'],))[0]['state'], 'finished')
        # Simulate a ready record left by an earlier version/crash gap. A
        # verified replay repairs only the export linked to this provider call.
        with self.store.transaction() as db:
            db.execute("UPDATE agent_operations SET state='submitted' WHERE id=?", (row['operation_id'],))
        self.assertEqual(artifacts.export(args, context)['artifact_refs'][0]['artifact_id'], row['id'])
        self.assertEqual(self.store.rows('SELECT state FROM agent_operations WHERE id=?', (row['operation_id'],))[0]['state'], 'finished')
        lease, path = artifacts.lease(self.project, key)
        self.assertIn("42", path.read_text("utf-8-sig"))
        with self.assertRaises(Conflict):
            artifacts.delete(self.project, key)
        artifacts.release(lease)
        with self.store.transaction() as db:
            db.execute("UPDATE agent_artifacts SET pinned=1 WHERE id=?", (key,))
        with self.assertRaises(Conflict):
            artifacts.delete(self.project, key)
        with self.store.transaction() as db:
            db.execute("UPDATE agent_artifacts SET pinned=0 WHERE id=?", (key,))
        with self.assertRaises(Conflict):
            artifacts.delete(self.project, key)
        self.agent.transition(self.project, self.run['id'], 1, 'cancelled', event_key='stopped')
        self.assertTrue(artifacts.delete(self.project, key))
        with self.assertRaises(PolicyDenied):
            artifacts.lease(self.project, key)

    def test_failed_publish_not_ready_and_corrupt_file_not_downloadable(self):
        artifacts, args, context = self.prepare()
        def crash(window):
            if window == "before_publish":
                raise OSError("disk full")
        with self.assertRaises(OSError):
            artifacts.export(args, context, fault=crash)
        row = self.store.rows("SELECT * FROM agent_artifacts")[0]
        self.assertEqual(row["state"], "staging")
        self.assertEqual(self.store.rows('SELECT state FROM agent_operations WHERE id=?', (row['operation_id'],))[0]['state'], 'submitted')
        self.assertFalse(artifacts.directory(self.project, row["id"]).exists())
        output = artifacts.export(args, context)
        key = output["artifact_refs"][0]["artifact_id"]
        path = artifacts.verify(self.project, key)
        path.write_text("corrupted", encoding="utf-8")
        with self.assertRaises(PolicyDenied):
            artifacts.lease(self.project, key)
        self.assertNotEqual(artifacts.get(self.project, key)["state"], "ready")
        with self.assertRaises(KeyError):
            artifacts.get(self.other, key)

    def test_revision_changed_during_export_snapshot_is_rejected(self):
        artifacts, args, context = self.prepare()
        original = artifacts.services.export
        def concurrent(body):
            self.store.save(args.results[0].result_id, {"text": "new", "tables": []}, 0)
            return original(body)
        artifacts.services.export = concurrent
        with self.assertRaises(Conflict):
            artifacts.export(args, context)
        self.assertFalse(self.store.rows("SELECT * FROM agent_artifacts WHERE state='ready'"))

    def test_expiry_pin_lease_and_delete_crash_recovery(self):
        from unittest.mock import patch
        artifacts, args, context = self.prepare()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        lease, target = artifacts.lease(self.project, key)
        with self.store.transaction() as db:
            db.execute('UPDATE agent_artifact_leases SET expires=? WHERE id=?', (time.time() + 5, lease))
            db.execute('UPDATE agent_artifacts SET expires=1 WHERE id=?', (key,))
        artifacts.renew(lease)
        self.assertGreater(self.store.rows('SELECT expires FROM agent_artifact_leases')[0]['expires'], time.time() + 3500)
        self.assertEqual(artifacts.cleanup(), [])
        self.agent.transition(self.project, self.run['id'], 1, 'cancelled', event_key='stop')
        self.assertEqual(artifacts.cleanup(), [], 'Live download pins the expired file')
        artifacts.release(lease)
        artifacts.pin(self.project, key, True)
        self.assertEqual(artifacts.cleanup(), [])
        self.assertEqual(artifacts.verify(self.project, key), target)
        artifacts.pin(self.project, key, False)
        with self.assertRaises(PolicyDenied):
            artifacts.lease(self.project, key)
        with patch('ocr_workbench.agent.artifacts.shutil.rmtree', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                artifacts.cleanup()
        self.assertEqual(artifacts.get(self.project, key)['state'], 'expiring')
        self.assertEqual(artifacts.cleanup(), [key])
        self.assertEqual(artifacts.get(self.project, key)['state'], 'expired')
        self.assertFalse(target.exists())

    def test_scope_change_filters_previous_run_manifest(self):
        artifacts, args, context = self.prepare()
        ref = args.results[0].model_dump()
        # Existing submitted work remains in history, but a new narrowed export
        # cannot silently include its out-of-scope result.
        with self.store.transaction() as db:
            self.store._enqueue_document_stage(db, self.photo['id'], 'process', {'mode': 'native'}, force=True)
            db.execute("INSERT INTO agent_operations VALUES('synthetic',?,?,?, ?,1,'finished',?,NULL,'now','now')",
                (self.project, self.run['id'], 'synthetic', 'hash', json.dumps({'results': [ref], 'coverage': {}})))
            db.execute('UPDATE agent_runs SET context=? WHERE id=?', (json.dumps({'selection': {'pages': [], 'permissions': {}}, 'scope_revision': 1}), self.run['id']))
        export = ExportResults(source='run_results', source_run_id=self.run['id'], format='txt')
        with self.store.transaction() as db:
            with self.assertRaises(PolicyDenied):
                artifacts._manifest(db, context, export)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_disconnect_releases_lease(self):
        import asyncio
        from ocr_workbench.agent.downloads import LeasedFileResponse
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'export.txt'
            path.write_text('hello')
            released = []
            artifacts = SimpleNamespace(renew=lambda key: None, release=released.append)
            response = LeasedFileResponse(artifacts, 'lease', path)
            async def send(message):
                if message['type'] == 'http.response.body':
                    raise OSError('synthetic client disconnected')
            async def receive():
                await asyncio.Event().wait()
            with self.assertRaises(OSError):
                await response({'type': 'http', 'method': 'GET', 'headers': [], 'extensions': {}}, receive, send)
            self.assertEqual(released, ['lease'])


class PartialExportGraphTests(unittest.IsolatedAsyncioTestCase):
    setUp = ArtifactTests.setUp
    prepare = ArtifactTests.prepare
    partial = ArtifactTests.partial

    async def test_permission_reply_reenters_export_and_publishes_once(self):
        from ocr_workbench.agent.checkpoints import Checkpoints
        from ocr_workbench.agent.graph import GraphServices, build_graph
        from ocr_workbench.agent.state import initial_state
        from ocr_workbench.agent.registry import ToolRegistry
        from ocr_workbench.agent.providers import normalize_response
        from langgraph.types import Command
        artifacts, args, _ = self.partial()
        with self.store.transaction() as db:
            db.execute('DELETE FROM agent_calls WHERE run_id=?', (self.run['id'],))
        registry = ToolRegistry(self.policy)
        async def export(body, context):
            return artifacts.export(body, context)
        registry.register('export_results', export)
        async def model(state, run):
            message = {'role': 'assistant', 'content': '已导出部分结果'}
            if state['step'] == 0:
                message = {'role': 'assistant', 'tool_calls': [{'id': 'partial', 'type': 'function', 'function': {'name': 'export_results', 'arguments': args.model_dump_json(exclude_none=True)}}]}
            return normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if state['step'] == 0 else 'stop'}]}, str(state['step']))
        cp = await Checkpoints(self.store).open()
        try:
            graph = build_graph(GraphServices(self.agent, registry, model), cp.saver)
            config = {'configurable': {'thread_id': self.run['graph_thread_id']}}
            await graph.ainvoke(initial_state(self.run, '导出'), config)
            self.assertEqual(self.agent.run(self.project, self.run['id'])['status'], 'waiting_user')
            self.assertFalse(self.store.rows('SELECT * FROM agent_artifacts'))
            decision = self.store.rows("SELECT * FROM agent_decisions WHERE status='pending'")[0]
            self.agent.reply_decision(self.project, self.run['id'], decision['id'], {'client_request_id': 'yes', 'payload_hash': decision['payload_hash'], 'option_id': 'allow'})
            snapshot = await graph.aget_state(config)
            result = await graph.ainvoke(Command(resume={snapshot.interrupts[0].id: {'notification': True}}), config)
            self.assertEqual(result['outcome'], 'partial')
            rows = self.store.rows('SELECT * FROM agent_artifacts')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['state'], 'ready')
            self.assertEqual(json.loads(rows[0]['manifest'])['partial_authorization'], decision['id'])
        finally:
            await cp.close()

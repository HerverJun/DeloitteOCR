import asyncio
import json
import threading
import time
import unittest
from types import SimpleNamespace

import test_agent_policy
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.registry import ToolRegistry
from ocr_workbench.agent.retry import register_retry_tool
from ocr_workbench.agent.jobs import JobBridge
from ocr_workbench.agent.contracts import ProviderCall
from ocr_workbench.application_services import ApplicationServices


class RetryTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def prepare(self):
        context = {'selection': {'pages': [], 'permissions': {'allow_retry_failed': True}}}
        with self.store.transaction() as db:
            db.execute('UPDATE agent_runs SET context=? WHERE id=?', (json.dumps(context), self.run['id']))
        self.run = self.agent.run(self.project, self.run['id'])
        self.policy.grant(self.project, 'scoped_processing', {'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']],
            'mode': 'native', 'force': False}, source='user_request', expires=time.time() + 60, run_id=self.run['id'])
        args = {'document_id': self.photo['id'], 'page_numbers': [1], 'mode': 'native'}
        self.agent.record_calls(self.project, self.run['id'], 1, 0, [{'call_id': 'source', 'tool': 'process_pages', 'arguments': args}])
        operations = Operations(self.agent, self.policy)
        source_context = {'run': self.run, 'generation': 1, 'step': 0, 'call_id': 'source'}
        def effect(db, operation_id):
            ids = self.store._enqueue_document_stages(db, [self.photo['id']], 'process', {'mode': 'native', 'engine': 'ppocr'})
            return {'stage_ids': ids}, [{'kind': 'pdf_stage', 'job_id': key, 'ownership': 'created', 'state': 'queued'} for key in ids]
        source = operations.submit(source_context, 'process_pages', args, effect)
        stage = self.store.claim_document_stage()
        self.store.finish_document_stage(stage['id'], error='synthetic failure')
        services = ApplicationServices(self.store, SimpleNamespace(wake=threading.Event()), SimpleNamespace(guard=self.store.file_lock))
        registry = ToolRegistry(self.policy)
        jobs = JobBridge(self.agent, services)
        register_retry_tool(registry, services, operations, jobs)
        args = {'previous_run_id': self.run['id'], 'operation_id': source['operation_id'], 'action': 'retry',
                'jobs': [{'kind': 'pdf_stage', 'job_id': stage['id']}]}
        return registry, args, stage

    def execute(self, registry, args):
        call = ProviderCall(call_id='retry', tool='retry_failed_jobs', arguments=args)
        self.agent.record_calls(self.project, self.run['id'], 1, 1, [call.model_dump()])
        return asyncio.run(registry.execute(self.run, 1, call, {'run': self.run, 'generation': 1, 'step': 1, 'call_id': 'retry'}))

    def test_explicit_failed_subset_replays_without_second_requeue(self):
        registry, args, stage = self.prepare()
        result = self.execute(registry, args)
        self.assertTrue(result['data']['waiting_jobs'])
        self.assertEqual(result['job_refs'][0]['ownership'], 'created')
        self.store.claim_document_stage()
        repeated = self.execute(registry, args)
        self.assertEqual(result['data']['operation_id'], repeated['data']['operation_id'])
        self.assertEqual(self.store.one('document_stages', stage['id'])['status'], 'running')
        self.assertEqual(len(self.store.rows('SELECT * FROM document_stages')), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 2)

    def test_success_or_stale_version_never_requeued(self):
        registry, args, stage = self.prepare()
        with self.store.transaction() as db:
            db.execute("UPDATE document_stages SET status='succeeded' WHERE id=?", (stage['id'],))
        with self.assertRaises(ValueError):
            self.execute(registry, args)
        self.assertEqual(len(self.store.rows('SELECT * FROM agent_operations')), 1)
        with self.store.transaction() as db:
            db.execute("UPDATE document_stages SET status='failed',version_id=? WHERE id=?", (self.other_photo['active_version'], stage['id']))
        with self.assertRaises(ValueError):
            self.execute(registry, args)
        self.assertEqual(self.store.one('document_stages', stage['id'])['status'], 'failed')

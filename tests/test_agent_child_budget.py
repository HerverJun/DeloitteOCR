import asyncio
import json
import unittest
from unittest.mock import patch

import test_agent_jobs
from ocr_workbench.agent.budgets import reserve_stage_children, increase
from ocr_workbench.agent.operations import Operations
from ocr_workbench.agent.selection import create_selection
from ocr_workbench.documents import Documents
from ocr_workbench.page_processing import process_stage
from ocr_workbench.coordinates import box_polygon
from ocr_workbench.store import Conflict
from pathlib import Path
import test_agent_policy


class ChildBudgetTests(unittest.TestCase):
    setUp = test_agent_policy.PolicyTests.setUp

    def test_actual_region_creation_is_atomic_and_replay_does_not_recharge(self):
        arguments = {'document_id': self.photo['id'], 'page_numbers': [1], 'mode': 'auto', 'engine': 'glm', 'force': False}
        import time
        self.policy.grant(self.project, 'scoped_processing', {'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']],
            'mode': 'auto', 'engine': 'glm', 'force': False}, source='user_request', expires=time.time() + 60, run_id=self.run['id'])
        with self.store.transaction() as db:
            db.execute('UPDATE agent_runs SET limits=? WHERE id=?', (json.dumps({'engine_jobs_per_run': 2}), self.run['id']))
        self.agent.record_calls(self.project, self.run['id'], 1, 0, [{'call_id': 'process', 'tool': 'process_pages', 'arguments': arguments}])
        def effect(db, key):
            stage_id = self.store._enqueue_document_stage(db, self.photo['id'], 'process', {'mode': 'auto', 'engine': 'glm'}, force=False)
            return {'stage_id': stage_id}, [{'kind': 'pdf_stage', 'job_id': stage_id, 'ownership': 'created', 'state': 'queued'}]
        context = {'run': self.run, 'generation': 1, 'step': 0, 'call_id': 'process'}
        operation = Operations(self.agent, self.policy).submit(context, 'process_pages', arguments, effect, cost={'pages': 1, 'jobs': 1})
        manager = Documents(self.store, Path(self.temp.name) / 'bundle')
        regions = [{'id': None, 'kind': 'ocr-needed', 'polygon': box_polygon([n * 5, 0, n * 5 + 4, 10])} for n in range(3)]
        def prepared(manager, page, doc, version, native, mode, cancelled):
            native['table_tool'] = {}
        with patch('ocr_workbench.table_tool.prepare', side_effect=prepared), patch.object(self.store, 'page_regions', return_value=regions):
            process_stage(manager, self.store.claim_document_stage(), lambda: False)
            stage = self.store.one('document_stages', operation['stage_id'])
            self.assertEqual(stage['status'], 'paused')
            self.assertEqual(json.loads(stage['output'])['agent_budget_wait']['required'], 4)
            self.assertEqual(self.store.rows('SELECT * FROM page_ocr_inputs'), [])
            self.assertEqual(self.agent.run(self.project, self.run['id'])['usage']['engine_jobs'], 1)
            increase(self.agent, self.project, self.run['id'], {'client_request_id': 'budget', 'generation': 1, 'limits': {'engine_jobs_per_run': 4}})
            with self.store.transaction() as db:
                db.execute("UPDATE document_stages SET status='queued' WHERE id=?", (stage['id'],))
            process_stage(manager, self.store.claim_document_stage(), lambda: False)
            self.assertEqual(len(self.store.rows('SELECT * FROM page_ocr_inputs')), 3)
            self.assertEqual(self.agent.run(self.project, self.run['id'])['usage']['engine_jobs'], 4)
            # Replay after the task transaction committed, before the worker receipt.
            with self.store.transaction() as db:
                db.execute("UPDATE document_stages SET status='queued' WHERE id=?", (stage['id'],))
            process_stage(manager, self.store.claim_document_stage(), lambda: False)
            self.assertEqual(len(self.store.rows('SELECT * FROM page_ocr_inputs')), 3)
            self.assertEqual(self.agent.run(self.project, self.run['id'])['usage']['engine_jobs'], 4)


class ChildBudgetGraphTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_jobs.JobTests.setUp
    runtime = test_agent_jobs.JobTests.runtime
    wait_status = test_agent_jobs.JobTests.wait_status

    async def test_budget_wait_requires_increase_and_reply_before_worker_resume(self):
        runtime = await self.runtime()
        try:
            with self.store.transaction() as db:
                db.execute('UPDATE agent_runs SET limits=? WHERE id=?', (json.dumps({'engine_jobs_per_run': 2}), self.run['id']))
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            stage = self.store.claim_document_stage()
            with self.store.transaction() as db:
                db.execute('BEGIN IMMEDIATE')
                self.assertFalse(reserve_stage_children(db, stage['id'], 3))
            await self.wait_status('waiting_user')
            decision = self.store.rows("SELECT * FROM agent_decisions WHERE status='pending'")[0]
            self.assertEqual(decision['kind'], 'child_job_budget')
            response = {'client_request_id': 'reply', 'payload_hash': decision['payload_hash'], 'option_id': 'continue'}
            with self.assertRaises(Conflict):
                self.agent.reply_decision(self.project, self.run['id'], decision['id'], response)
            current = increase(self.agent, self.project, self.run['id'], {'client_request_id': 'increase', 'generation': 1, 'limits': {'engine_jobs_per_run': 4}})
            self.assertEqual(self.store.one('document_stages', stage['id'])['status'], 'paused')
            self.agent.reply_decision(self.project, self.run['id'], decision['id'], response)
            if self.run['id'] in runtime.tasks:
                await runtime.tasks[self.run['id']]
            runtime.schedule(current, resume=True)
            await self.wait_status('waiting_jobs')
            self.assertEqual(self.store.one('document_stages', stage['id'])['status'], 'queued')
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_model_requests')), 1)
            self.store.claim_document_stage()
            with self.store.transaction() as db:
                self.assertTrue(reserve_stage_children(db, stage['id'], 3))
            self.store.finish_document_stage(stage['id'], {'synthetic': True})
            await self.wait_status('completed')
        finally:
            await runtime.close()

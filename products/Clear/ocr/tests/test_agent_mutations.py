import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

import test_agent_store
import test_agent_recovery
from ocr_workbench.store import Conflict


class SessionMutationTests(unittest.TestCase):
    setUp = test_agent_store.AgentStoreTests.setUp

    def test_duplicate_title_and_archive_do_not_overwrite_later_updates(self):
        first = {'client_request_id': 'title-1', 'title': 'First'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.agent.update_session(self.project, self.session['id'], first), range(8)))
        self.assertEqual(len({r['updated'] for r in results}), 1)
        second = {'client_request_id': 'title-2', 'title': 'Second', 'status': 'archived'}
        self.agent.update_session(self.project, self.session['id'], second)
        self.assertEqual(self.agent.update_session(self.project, self.session['id'], first), results[0])
        self.assertEqual(self.agent.session(self.project, self.session['id'])['title'], 'Second')
        self.assertEqual(self.agent.session(self.project, self.session['id'])['status'], 'archived')
        with self.assertRaises(Conflict):
            self.agent.update_session(self.project, self.session['id'], {**first, 'title': 'Changed'})


class RuntimeMutationTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_recovery.RecoveryTests.setUp
    runtime = test_agent_recovery.RecoveryTests.runtime
    wait_status = test_agent_recovery.RecoveryTests.wait_status
    config_revision = test_agent_recovery.RecoveryTests.config_revision
    reopen = test_agent_recovery.RecoveryTests.reopen

    async def test_late_cancel_cannot_overwrite_completed_outcome(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            stage = self.store.claim_document_stage()
            self.store.finish_document_stage(stage['id'], {'synthetic': True})
            before = await self.wait_status('completed')
            with self.assertRaises(Conflict):
                await runtime.cancel(self.project, self.run['id'], before['generation'], 'stop_agent', request_id='late')
            after = self.agent.run(self.project, self.run['id'])
            self.assertEqual((after['status'], after['outcome'], after['generation']), (before['status'], before['outcome'], before['generation']))
            self.assertFalse(self.store.rows("SELECT * FROM agent_mutations WHERE request_id='late'"))
        finally:
            await runtime.close()

    async def test_cancel_replay_reconciles_jobs_after_interrupted_acknowledgment(self):
        runtime = await self.runtime()
        try:
            runtime.schedule(self.run)
            await self.wait_status('waiting_jobs')
            runtime.inbox.enqueue(self.project, self.session['id'], {'client_request_id': 'inbox', 'content': 'Later'})
            with patch.object(runtime.jobs, 'cancel_owned', side_effect=OSError('synthetic cancellation interruption')):
                with self.assertRaises(OSError):
                    await runtime.cancel(self.project, self.run['id'], 1, 'cancel_owned_jobs', request_id='cancel')
            self.assertEqual(self.store.rows('SELECT state FROM agent_inbox')[0]['state'], 'discarded')
            self.assertEqual(self.store.rows('SELECT state FROM agent_mutations')[0]['state'], 'pending')
            results = await asyncio.gather(*(runtime.cancel(self.project, self.run['id'], 1, 'cancel_owned_jobs', request_id='cancel') for _ in range(4)))
            self.assertEqual(results, [results[0]] * 4)
            self.assertEqual(self.store.rows('SELECT status FROM document_stages')[0]['status'], 'cancelled')
            self.assertEqual(self.store.rows('SELECT state FROM agent_mutations')[0]['state'], 'applied')
            with self.assertRaises(Conflict):
                await runtime.cancel(self.project, self.run['id'], 1, 'stop_agent', request_id='cancel')
        finally:
            await runtime.close()

    async def test_resume_duplicates_and_expired_lease_fence_old_generation(self):
        first = await self.runtime()
        self.config_revision()
        first.schedule(self.run)
        await self.wait_status('waiting_jobs')
        await first.close()
        second = await self.reopen(first)
        try:
            current = self.agent.run(self.project, self.run['id'])
            await asyncio.gather(*(second.resume_interrupted(self.project, current['id'], current['generation'], request_id='resume') for _ in range(5)))
            self.assertEqual(len(self.store.rows('SELECT * FROM agent_mutations')), 1)
            with self.store.transaction() as db:
                db.execute('UPDATE agent_runs SET lease_expires=1 WHERE id=?', (current['id'],))
            await second._observe_once()
            expired = self.agent.run(self.project, current['id'])
            self.assertEqual(expired['status'], 'interrupted')
            self.assertEqual(expired['generation'], current['generation'] + 1)
            with self.assertRaises(Conflict):
                second.agent.begin_model_request(self.project, current['id'], current['generation'], 9, 'stale')
            await second.resume_interrupted(self.project, current['id'], current['generation'], request_id='resume')
            self.assertEqual(self.agent.run(self.project, current['id'])['status'], 'interrupted', 'Old acknowledgment cannot resume a new generation')
        finally:
            await second.close()

    async def test_observer_survives_outer_database_error(self):
        runtime = await self.runtime()
        try:
            runtime.observer.cancel()
            await asyncio.gather(runtime.observer, return_exceptions=True)
            tick = runtime._observe_once
            recovered = asyncio.Event()
            count = 0
            async def flaky():
                nonlocal count
                count += 1
                if count == 1:
                    raise OSError('synthetic database unavailable')
                await tick()
                recovered.set()
            with patch.object(runtime, '_observe_once', side_effect=flaky):
                runtime.observer = asyncio.create_task(runtime._observe_jobs())
                await asyncio.wait_for(recovered.wait(), 3)
                # The successful tick also awaits artifact maintenance before
                # clearing its health error; wait for that observable completion.
                for _ in range(100):
                    if runtime.observer_error is None:
                        break
                    await asyncio.sleep(.01)
                self.assertFalse(runtime.observer.done())
                self.assertIsNone(runtime.observer_error)
        finally:
            await runtime.close()

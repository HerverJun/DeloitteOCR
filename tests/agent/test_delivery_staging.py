"""Small model outputs stage real immutable CSVs; final readback keeps every condition."""
import asyncio
from dataclasses import replace
import json
import unittest

import test_harness_delivery_checks as fixtures
from test_graph import response
from workbench_platform.agent.context import TokenEstimator
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import AgentStore, canonical
from workbench_platform.agent.tasks import AuditTasks


class DeliveryStagingTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.DeliveryChecksTests.setUp
    tearDown = fixtures.DeliveryChecksTests.tearDown
    publish = fixtures.DeliveryChecksTests.publish
    check = fixtures.DeliveryChecksTests.check

    async def call(self, arguments, call_id=None):
        self.sequence += 1
        call = {'call_id': call_id or 'stagedelivery' + str(self.sequence), 'tool': 'delivery_publish', 'arguments': arguments}
        self.registry.validate_batch([call], self.registry.tools)
        self.assertLess(TokenEstimator('').count(canonical(call)), 2048)
        return await self.registry.dispatch(call, self.ctx)

    def requirements(self):
        tasks = AuditTasks(type('Runtime', (), {'store': self.store})())
        return tasks.get(self.ctx.session_id)['delivery_requirements']

    def five_checks(self, ref):
        return [self.check(ref, 'row_count', expected_rows=2), self.check(ref, 'column_trimmed', column='supplier'),
            self.check(ref, 'column_prefix', column='id', prefix='000'),
            self.check(ref, 'column_text_equals', column='amount', expected_value='10.00'),
            self.check(ref, 'column_decimal_places', column='amount', places=2)]

    async def test_partial_final_keeps_fixed_real_file_and_checks_for_same_goal_continue(self):
        ref, artifact = self.publish(b'id\n000001\n000002\n')
        stage = await self.call({'summary': 'First completed', 'finalize': False, 'artifact_refs': [ref],
            'checks': [self.check(ref, 'row_count', expected_rows=2)]})
        self.assertEqual(stage['status'], 'success')
        failed, _ = self.store.begin_operation(self.ctx.run_id, self.ctx.generation,
            {'call_id': 'remaining-native-failure', 'tool': 'data_import', 'arguments': {'asset_id': 'third'}})
        self.store.finish_operation(failed['id'], {'status': 'error', 'summary': 'Third not submitted',
            'data': {'accepted_write': False}, 'error': {'code': 'native_import_failure', 'message': 'Third not submitted', 'retryable': False},
            'artifact_refs': [], 'result_refs': [], 'job_refs': [], 'warnings': [], 'coverage': {}, 'next_offset': None})
        partial = await self.call({'summary': 'Keep first, remaining input blocked'}, 'partial-final')
        self.assertEqual(partial['status'], 'success', partial)
        self.assertEqual(partial['data']['completeness'], 'partial')
        req = self.requirements()
        self.assertEqual(len(req['stages']), 1)
        self.assertEqual(req['stages'][0]['checks'], req['history'][-1]['checks'])
        self.assertEqual(req['stages'][0]['checks'][0]['expected_rows'], 2)
        self.assertTrue(any(file['sha256'] == artifact.blob_hash for file in req['stages'][0]['files']))
        self.assertEqual(req['history'][-1]['completeness'], 'partial')
        replay = await self.call({'summary': 'Keep first, remaining input blocked'}, 'partial-final')
        self.assertEqual(replay, partial)
        self.assertEqual(self.requirements(), req)

    async def test_two_real_csv_stages_survive_restart_and_summary_only_final_rechecks_all_requirements(self):
        refs, hashes = [], []
        for index in range(2):
            ref, artifact = self.publish(b'id,supplier,amount\n000001,ACME,10.00\n000002,OTHER,10.00\n')
            refs.append(ref); hashes.append(artifact.blob_hash)
            args = {'summary': f'核验第{index + 1}件', 'finalize': False, 'artifact_refs': [ref], 'checks': self.five_checks(ref)}
            result = await self.call(args, 'stable-stage-' + str(index))
            self.assertEqual(result['status'], 'success', result)
            self.assertTrue(result['data']['verified']); self.assertFalse(result['data']['finalized'])
            replay = await self.call(args, 'stable-stage-' + str(index))
            self.assertEqual(replay, result)
        self.assertEqual(len(self.requirements()['stages']), 2)
        self.store = AgentStore(self.store.root)
        self.ctx = replace(self.ctx, store=self.store)
        result = await self.call({'summary': '已核验全部成果'}, 'stable-final')
        self.assertEqual(result['status'], 'success', result)
        self.assertTrue(result['data']['finalized']); self.assertEqual(result['data']['completeness'], 'complete')
        self.assertEqual({ref['object_id'] for ref in result['artifact_refs']}, {ref['object_id'] for ref in refs})
        self.assertTrue(all(ref['sha256'] for ref in result['artifact_refs']))
        file_checks = [outcome for outcome in result['data']['delivery_checks'] if outcome['name'] != 'native_job_status']
        self.assertEqual(len(file_checks), 12)
        self.assertEqual({outcome['evidence']['sha256'] for outcome in file_checks}, set(hashes))
        requirements = self.requirements()
        self.assertEqual(requirements['stages'], [])
        self.assertEqual(len(requirements['history'][0]['consumed_stages']), 2)
        self.assertEqual(len(requirements['history'][0]['checks']), 10)
        # Simulate a crash after the atomic task receipt/consumption but before
        # the general operation acknowledgement commits.
        with self.store.transaction() as db:
            db.execute("UPDATE operations SET state='intent',result=NULL WHERE run_id=? AND call_id='stable-final'", (self.ctx.run_id,))
        replay = await self.call({'summary': '已核验全部成果'}, 'stable-final')
        self.assertEqual(replay, result)
        self.assertEqual(len(self.requirements()['history']), 1)

    async def test_omitted_batch_is_inherited_and_lowered_condition_is_rejected(self):
        first, _ = self.publish(b'id\n000001\n000002\n')
        second, _ = self.publish(b'id\n000003\n000004\n')
        for ref in (first, second):
            result = await self.call({'summary': '固定两行要求', 'finalize': False, 'artifact_refs': [ref],
                                     'checks': [self.check(ref, 'row_count', expected_rows=2)]})
            self.assertEqual(result['status'], 'success')
        weakened = await self.call({'summary': '不能降低', 'artifact_refs': [second],
                                   'checks': [self.check(first, 'row_count', expected_rows=1)]})
        self.assertEqual(weakened['status'], 'error')
        self.assertEqual(weakened['error']['code'], 'delivery_requirements_conflict')
        self.assertEqual(len(self.requirements()['stages']), 2)
        result = await self.call({'summary': '只提交第二份引用也不能遗漏第一份', 'artifact_refs': [second]})
        self.assertTrue(result['data']['finalized'])
        self.assertEqual(len(result['artifact_refs']), 2)
        self.assertEqual(sum(check['name'] == 'row_count' for check in result['data']['delivery_checks']), 2)
        self.assertEqual(result['data']['completeness'], 'complete')
        # The rejected request stays auditable; every original condition passed
        # during the final readback and no native coverage failed.

    async def test_stage_allows_unrelated_pending_job_but_final_still_requires_authoritative_settlement(self):
        ref, _ = self.publish(b'id\n000001\n')
        record, _ = self.store.begin_operation(self.ctx.run_id, self.ctx.generation,
            {'call_id': 'pending-native', 'tool': 'data_import', 'arguments': {}})
        pending = {'status': 'pending', 'summary': 'Native job still running', 'data': {}, 'result_refs': [],
                   'artifact_refs': [], 'job_refs': [], 'coverage': {}, 'warnings': [], 'error': None, 'next_offset': None}
        self.store.finish_operation(record['id'], pending)
        result = await self.call({'summary': '单件先核验', 'finalize': False, 'artifact_refs': [ref],
                                 'checks': [self.check(ref, 'row_count', expected_rows=1)]})
        self.assertEqual(result['status'], 'success', result)
        self.assertFalse(result['data']['finalized'])
        with self.store.transaction() as db:
            self.store.cancel_in_transaction(db, run_id=self.ctx.run_id)
        next_run, _ = self.store.start_run(self.ctx.session_id, 'Continue the same task', 'new-goal', 1)
        self.ctx = replace(self.ctx, run_id=next_run['id'], generation=next_run['generation'])
        blocked = await self.call({'summary': '最终核验'})
        self.assertEqual(blocked['status'], 'error')
        self.assertFalse(blocked.get('data', {}).get('finalized', False))
        self.assertEqual(len(self.requirements()['stages']), 1)
        self.store.finish_operation(record['id'], {**pending, 'status': 'success', 'summary': 'Authoritative native completion'})
        final = await self.call({'summary': '最终核验'})
        self.assertTrue(final['data']['finalized'], final)
        self.assertEqual(final['data']['completeness'], 'complete')

    async def test_old_run_null_write_intent_blocks_final_without_replay_or_changing_the_original_record(self):
        record, _ = self.store.begin_operation(self.ctx.run_id, self.ctx.generation,
            {'call_id': 'uncertain-old-write', 'tool': 'external_write', 'arguments': {'name': 'unknown original submission'}})
        with self.store.transaction() as db:
            self.store.cancel_in_transaction(db, run_id=self.ctx.run_id)
        next_run, _ = self.store.start_run(self.ctx.session_id, 'Continue without repeating unknown write', 'after-unknown-write', 1)
        self.ctx = replace(self.ctx, run_id=next_run['id'], generation=next_run['generation'])
        before = self.store.rows('SELECT * FROM operations WHERE id=?', (record['id'],))[0]
        ref, _ = self.publish(b'id\n000001\n')
        staged = await self.call({'summary': '已知成果可先验收', 'finalize': False, 'artifact_refs': [ref],
                                 'checks': [self.check(ref, 'row_count', expected_rows=1)]})
        self.assertEqual(staged['status'], 'success', staged)
        final = await self.call({'summary': '不能把未知写操作当作完成'})
        self.assertEqual(final['status'], 'error', final)
        self.assertFalse(final.get('data', {}).get('verified', False))
        self.assertEqual(self.store.rows('SELECT * FROM operations WHERE id=?', (record['id'],))[0], before)
        self.assertEqual(len(self.requirements()['stages']), 1)

    async def test_final_reopens_saved_bytes_and_does_not_consume_stages_after_corruption(self):
        ref, artifact = self.publish(b'id\n000001\n')
        staged = await self.call({'summary': '固定原文件', 'finalize': False, 'artifact_refs': [ref],
                                 'checks': [self.check(ref, 'row_count', expected_rows=1)]})
        self.assertEqual(staged['status'], 'success')
        self.center.store._blob(artifact.blob_hash).write_bytes(b'id\n000002\n')
        final = await self.call({'summary': '必须重新读回'})
        self.assertEqual(final['status'], 'error')
        self.assertFalse(final.get('data', {}).get('finalized', False))
        self.assertEqual(len(self.requirements()['stages']), 1)

    async def test_eight_artifacts_and_forty_checks_need_only_small_per_file_calls_and_one_summary(self):
        for index in range(8):
            ref, _ = self.publish(b'id,supplier,amount\n000001,ACME,10.00\n000002,OTHER,10.00\n')
            staged = await self.call({'summary': f'第{index + 1}件', 'finalize': False,
                                     'artifact_refs': [ref], 'checks': self.five_checks(ref)})
            self.assertEqual(staged['status'], 'success', staged)
        final = await self.call({'summary': '八件成果验收完成'})
        self.assertEqual(final['status'], 'success', final)
        self.assertEqual(len(final['artifact_refs']), 8)
        self.assertEqual(len([item for item in final['data']['delivery_checks'] if item['status'] == 'passed' and item['name'] != 'native_job_status']), 40)
        self.assertTrue(final['data']['finalized'])

    async def test_final_uses_latest_native_export_state_and_preserves_failed_coverage_as_partial(self):
        ref, _ = self.publish(b'id\n000001\n')
        self.assertEqual((await self.call({'summary': '先验单件', 'finalize': False, 'artifact_refs': [ref],
            'checks': [self.check(ref, 'row_count', expected_rows=1)]}))['status'], 'success')
        native = next(iter(self.services.data_workbench.jobs.values()))
        native['status'] = 'running'
        blocked = await self.call({'summary': '核验原生终态'})
        self.assertEqual(blocked['error']['code'], 'native_delivery_unverified', blocked)
        self.assertFalse(blocked['data']['finalized'])
        self.assertEqual(len(self.requirements()['stages']), 1)
        native['status'] = 'failed'
        partial = await self.call({'summary': '核验原生终态'})
        self.assertEqual(partial['status'], 'success', partial)
        self.assertTrue(partial['data']['finalized'])
        self.assertEqual(partial['data']['completeness'], 'partial')
        self.assertTrue(partial['data']['limitations'])

    async def test_harness_stage_does_not_end_task_or_allow_discussion_to_skip_final_acceptance(self):
        ref, _ = self.publish(b'id\n000001\n')
        calls = []
        async def provider(state, schemas):
            calls.append(state['step'])
            if state['step'] == 0:
                return response(state, 'delivery_publish', {'summary': '先分批验收', 'finalize': False,
                    'artifact_refs': [ref], 'checks': [self.check(ref, 'row_count', expected_rows=1)]})
            previous = next(message['result'] for message in reversed(state['messages']) if message['role'] == 'tool')
            if state['step'] == 1:
                self.assertFalse(previous['data']['finalized'])
                return response(state, 'conversation_answer', {'content': '分批核验不能充当交付'})
            self.assertEqual(previous['error']['code'], 'delivery_required')
            return response(state, 'delivery_publish', {'summary': '最终核验交付'})
        runtime = AgentRuntime(self.root / 'integration-agent', self.services, model_provider=provider)
        self.services.agent = runtime
        await runtime.start()
        session = runtime.store.create_session('wtest', 'Stage integration', [self.source.asset_id], 'stage-integration-session')
        try:
            run = await runtime.submit(session['id'], '分批核验后最终交付', 'stage-integration-run')
            async with asyncio.timeout(60):
                while runtime.store.run(run['id'])['status'] in {'running', 'waiting_job'}:
                    await asyncio.sleep(.1)
            final = runtime.store.run(run['id'])
            self.assertIn(final['status'], {'succeeded', 'partial'}, runtime.store.events(session['id'], limit=10000))
            self.assertEqual(calls, [0, 1, 2])
            deliveries = [event for event in runtime.store.events(session['id'], limit=10000)['items'] if event['kind'] == 'delivery']
            self.assertEqual(len(deliveries), 1)
            self.assertTrue(deliveries[0]['payload']['delivery']['data']['finalized'])
        finally:
            await runtime.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)

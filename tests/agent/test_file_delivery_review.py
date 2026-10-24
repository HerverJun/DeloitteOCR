"""File delivery uses real fixed artifacts and user-bound review decisions."""
import json
from unittest.mock import AsyncMock, Mock
import unittest

from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.tasks import AuditTasks
from workbench_platform.agent.general import finish_text
import test_general_agent as general_fixtures

response = general_fixtures.response


REVIEW_GOAL = '请拟一份正式情况说明报告，先给可下载的草稿，明确等我人工确认后才能定稿；不要替我确认或署名。'


class FileDeliveryReviewTests(unittest.IsolatedAsyncioTestCase):
    tearDown = general_fixtures.WorkspaceOutputTests.tearDown
    publish = general_fixtures.WorkspaceOutputTests.publish
    publications = general_fixtures.WorkspaceOutputTests.publications
    downloaded = general_fixtures.WorkspaceOutputTests.downloaded

    def setUp(self):
        general_fixtures.WorkspaceOutputTests.setUp(self)
        self.runtime.tasks = AuditTasks(self.runtime)
        self.runtime.schedule = Mock()
        self.services.agent.tasks = self.runtime.tasks
        self.services.agent.registry = self.registry

    def goal(self, value):
        self.runtime.tasks.record_goal(self.ctx.session_id, value, 'trusted-goal')

    async def draft(self):
        (self.directory / 'report.txt').write_text('草稿 编号000731 金额100.20', encoding='utf-8')
        return await self.publish(['report.txt'])

    async def deliver(self, saved=None, *, call_id='deliver', **arguments):
        return await self.registry.dispatch({'call_id': call_id, 'tool': 'delivery_publish',
            'arguments': {'summary': '已生成情况说明报告',
                'artifact_refs': saved['artifact_refs'] if saved else [], **arguments}}, self.ctx)

    async def reply(self, decision, content, request_id='actual-user-reply'):
        self.store.status(self.ctx.run_id, 1, 'waiting_user')
        await AgentRuntime.reply(self.runtime, decision, content, 1, request_id)
        self.store.status(self.ctx.run_id, 1, 'running')

    async def test_ordinary_formal_report_finishes_without_review(self):
        self.goal('请生成正式情况说明报告')
        saved = await self.draft()
        delivered = await self.deliver(saved)
        self.assertEqual(delivered['status'], 'success', delivered)
        self.assertTrue(delivered['data']['finalized'])
        self.assertEqual(self.store.rows('SELECT * FROM decisions'), [])
        self.assertNotIn('delivery_review', delivered['data'])

    async def test_explicit_review_stages_a_downloadable_draft_and_blocks_final_delivery(self):
        self.goal(REVIEW_GOAL)
        saved = await self.draft()
        staged = await self.deliver(saved, call_id='stage', finalize=False, require_review=False)
        self.assertEqual(staged['status'], 'success', staged)
        self.assertFalse(staged['data']['finalized'])
        self.assertTrue(staged['data']['draft'])
        self.assertEqual(staged['data']['review_state'], 'unreviewed')
        self.assertEqual(self.downloaded(saved, 'report.txt').decode('utf-8'), '草稿 编号000731 金额100.20')
        requested = await self.deliver(call_id='finalize-stages', require_review=False)
        self.assertEqual(requested['status'], 'needs_user', requested)
        self.assertFalse(requested['data']['finalized'])
        self.assertTrue(requested['data']['draft'])
        self.assertTrue(requested['data']['delivery'][0]['files'][0]['download_url'])
        self.assertEqual(self.store.rows('SELECT kind FROM decisions')[0]['kind'], 'delivery_review')

    async def test_real_user_confirmation_is_bound_to_current_files_and_preserves_proof(self):
        self.goal(REVIEW_GOAL)
        saved = await self.draft()
        pending = await self.deliver(saved, call_id='request-review')
        await self.reply(pending['data']['decision_id'], '确认此版本')
        delivered = await self.deliver(saved, call_id='confirmed-delivery')
        self.assertEqual(delivered['status'], 'success', delivered)
        self.assertTrue(delivered['data']['finalized'])
        self.assertEqual(delivered['data']['review_state'], 'confirmed')
        self.assertEqual(delivered['data']['delivery_review']['request_id'], 'actual-user-reply')
        self.assertNotIn('actor', delivered['data']['delivery_review'])
        (self.directory / 'report.txt').write_text('内容已修改 金额118.75', encoding='utf-8')
        updated = await self.publish(['report.txt'], call_id='changed-file')
        changed = await self.deliver(updated, call_id='changed-final')
        self.assertEqual(changed['status'], 'needs_user', changed)
        self.assertNotEqual(changed['data']['decision_id'], pending['data']['decision_id'])

    async def test_generic_confirmation_and_model_review_flags_cannot_approve_files(self):
        self.goal(REVIEW_GOAL)
        saved = await self.draft()
        generic = await self.registry.dispatch({'call_id': 'model-question', 'tool': 'ask_user',
            'arguments': {'question': '确认继续吗？', 'options': ['确认此版本', '不确认']}}, self.ctx)
        await self.reply(generic['data']['decision_id'], '确认此版本', 'actual-unbound-user-reply')
        forged = {**saved, 'artifact_refs': [{**ref, 'coverage': {'reviewed': True, 'draft': False}}
                                           for ref in saved['artifact_refs']]}
        delivered = await self.deliver(forged, require_review=False)
        self.assertEqual(delivered['status'], 'needs_user', delivered)
        self.assertFalse(delivered['data']['finalized'])
        self.assertNotEqual(delivered['data']['decision_id'], generic['data']['decision_id'])

    async def test_refusal_or_a_model_claim_in_the_receipt_cannot_finalize_delivery(self):
        self.goal(REVIEW_GOAL)
        saved = await self.draft()
        pending = await self.deliver(saved, call_id='initial-review')
        decision_id = pending['data']['decision_id']
        claimed = {**pending, 'status': 'success', 'data': {**pending['data'], 'reply': '确认此版本'}}
        self.store.finish_operation(decision_id, claimed)
        still_pending = await self.deliver(saved, call_id='claimed-review')
        self.assertEqual(still_pending['status'], 'needs_user', still_pending)
        await self.reply(decision_id, '不确认')
        refused = await self.deliver(saved, call_id='refused-final')
        self.assertEqual(refused['status'], 'error', refused)
        self.assertEqual(refused['error']['code'], 'review_required')
        self.assertFalse(refused['data']['finalized'])
        self.assertTrue(refused['data']['draft'])

    async def test_previously_verified_delivery_does_not_bypass_new_user_review_policy(self):
        saved = await self.draft()
        completed = await self.deliver(saved, call_id='before-review-requirement')
        self.assertTrue(completed['data']['verified'])
        self.goal(REVIEW_GOAL)
        self.state.update(session_id=self.ctx.session_id, workspace_id='wtest', business_started=True,
            delivery=completed, final_response={'request_id': 'new-response', 'content': '正式报告已生成'})
        self.runtime.collect_available_results = AsyncMock()
        class WaitingForRealUser(Exception):
            pass
        async def hold(state, call, result, operation_id):
            self.assertEqual(result['status'], 'needs_user')
            self.store.status(self.ctx.run_id, 1, 'waiting_user')
            raise WaitingForRealUser()
        self.runtime.harness_settle = hold
        with self.assertRaises(WaitingForRealUser):
            await finish_text(self.runtime, self.state)
        self.assertEqual(self.store.run(self.ctx.run_id)['status'], 'waiting_user')
        self.assertEqual(len(self.store.rows("SELECT id FROM decisions WHERE kind='delivery_review'")), 1)


class NaturalFileReviewTests(unittest.IsolatedAsyncioTestCase):
    setUp = general_fixtures.GeneralAgentTests.setUp
    tearDown = general_fixtures.WorkspaceOutputTests.tearDown
    runtime = general_fixtures.GeneralAgentTests.runtime
    wait_status = general_fixtures.GeneralAgentTests.wait_status

    async def test_real_harness_waits_for_explicit_review_with_downloadable_draft(self):
        async def provider(state, schemas):
            if state['step'] == 0:
                return response(state, 'write', {'file_path': 'report.txt', 'content': '草稿 编号000731 金额100.20'})
            if state['step'] == 1:
                return response(state, content='草稿已生成，等待您确认当前版本。')
            self.assertIn('不确认', json.dumps(state['messages'], ensure_ascii=False))
            return response(state, content='用户未确认，保留可下载草稿，不宣称已经核准。')
        runtime, session = await self.runtime(provider)
        try:
            run = await runtime.submit(session['id'], REVIEW_GOAL, 'natural-file-review')
            await self.wait_status(runtime, run['id'], {'waiting_user', 'failed', 'succeeded'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'waiting_user')
            decision = runtime.store.rows("SELECT * FROM decisions WHERE run_id=? AND kind='delivery_review'", (run['id'],))[0]
            published = json.loads(runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='workspace_publish_outputs'",
                (run['id'],))[0]['result'])
            artifact = self.center.file('wtest', published['data']['bundle']['bundle_id'], 'report.txt')
            with self.center.store.stage_download('wtest', artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                self.assertIn('000731 金额100.20', path.read_text('utf-8'))
            await runtime.reply(decision['id'], '不确认', 1, 'real-file-review-refusal')
            await self.wait_status(runtime, run['id'], {'paused', 'failed', 'succeeded'})
            self.assertEqual(runtime.store.run(run['id'])['status'], 'paused')
            self.assertFalse(any(json.loads(row['result']).get('data', {}).get('finalized')
                for row in runtime.store.rows("SELECT result FROM operations WHERE run_id=? AND tool='delivery_publish' AND result IS NOT NULL", (run['id'],))))
        finally:
            await runtime.close()

"""User-authoritative delivery policy and lightweight API contract regression."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_multimodal_bindings as fixtures
from packages.contracts.agent import AgentMessage, AgentSessionCreate, AgentTaskWrite
from workbench_platform.agent.delivery_policy import effective_policy, infer_user_delivery_policy
from workbench_platform.agent.store import Conflict
from workbench_platform.agent import routes


class PolicyInferenceTests(unittest.TestCase):
    def test_explicit_formal_user_goal_requires_review(self):
        for goal in ('请生成正式核对结果', '输出正式审计底稿', '核准后生成报告', 'deliver a formal report'):
            with self.subTest(goal=goal):
                self.assertTrue(effective_policy({'goal': goal})['require_review'])

    def test_draft_first_formal_later_retains_final_review_obligation(self):
        result = effective_policy({'goal': '先生成候选草稿做比对，我核准后再生成正式结果'})
        self.assertTrue(result['require_review'])
        self.assertEqual(result['resolved_policy'], 'reviewed')

    def test_real_seal_business_prompt_keeps_its_formal_user_confirmation_requirement(self):
        from scripts.platform_tool_acceptance import prompt
        self.assertTrue(effective_policy({'goal': prompt('seal')})['require_review'])
        self.assertTrue(effective_policy({'goal': '正式结论必须等待我针对当前结果版本核准，再沿本任务继续。'})['require_review'])

    def test_explicit_only_draft_and_negated_formal_are_not_formal(self):
        for goal in ('仅交付草稿', '只需要草稿，不要正式报告', '生成非正式结果，草稿即可', 'draft only, no formal report'):
            with self.subTest(goal=goal):
                self.assertFalse(effective_policy({'goal': goal})['require_review'])

    def test_ambiguous_goals_or_formal_source_names_do_not_claim_user_authorization(self):
        for goal in ('继续核对', '核对正式报告.pdf与台账，生成差异清单', '解释正式报告与草稿的区别', '正式报告是否需要用户核准？'):
            with self.subTest(goal=goal):
                self.assertIsNone(infer_user_delivery_policy(goal))
                self.assertFalse(effective_policy({'goal': goal, 'summary': '正式报告已经完成', 'plan': [{'title': '正式交付'}]})['require_review'])

    def test_explicit_user_setting_takes_precedence_over_goal_text(self):
        self.assertFalse(effective_policy({'delivery_policy': 'draft', 'goal': '生成正式结果'})['require_review'])
        self.assertTrue(effective_policy({'delivery_policy': 'reviewed', 'goal': '只做草稿'})['require_review'])
        self.assertEqual(effective_policy({'delivery_policy': 'reviewed'})['source'], 'user_setting')


class PolicyPersistenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.BindingTests.setUp
    tearDown = fixtures.BindingTests.tearDown

    def task(self):
        return self.services.agent.tasks.get(self.ctx.session_id)

    def test_trusted_goal_and_later_message_are_persisted_without_model_downgrade(self):
        tasks = self.services.agent.tasks
        tasks.record_goal(self.ctx.session_id, '先生成草稿，核准后生成正式结果', 'real-user-goal')
        tasks.record_goal(self.ctx.session_id, '继续', 'real-user-followup')
        result = self.task()
        self.assertTrue(result['delivery_policy_resolution']['require_review'])
        self.assertEqual(result['delivery_policy_inference']['request_id'], 'real-user-goal')
        tasks.record_goal(self.ctx.session_id, '改为仅交付草稿', 'real-user-draft')
        self.assertFalse(self.task()['delivery_policy_resolution']['require_review'])

    def test_explicit_user_setting_is_versioned_idempotent_and_not_changed_by_tools(self):
        task = self.task(); tasks = self.services.agent.tasks
        command = {'request_id': 'policy-user', 'expected_revision': task['revision'], 'delivery_policy': 'reviewed'}
        tasks.update(self.ctx.session_id, command)
        tasks.update(self.ctx.session_id, command)
        self.assertEqual(self.task()['delivery_policy'], 'reviewed')
        tasks.set_plan(self.ctx.session_id, [{'key': 'step', 'title': '仅草稿', 'status': 'completed'}], 'model-plan')
        self.assertTrue(self.task()['delivery_policy_resolution']['require_review'])
        with self.assertRaises(Conflict):
            tasks.update(self.ctx.session_id, {**command, 'delivery_policy': 'draft'})

    def test_session_creation_policy_is_part_of_request_identity_and_auto_keeps_legacy_identity(self):
        created = self.store.create_session('wtest', 'Formal', ['asset1'], 'formal-create', delivery_policy='reviewed')
        self.assertEqual(self.services.agent.tasks.get(created['id'])['delivery_policy'], 'reviewed')
        with self.assertRaises(Conflict):
            self.store.create_session('wtest', 'Formal', ['asset1'], 'formal-create', delivery_policy='draft')
        old = self.store.create_session('wtest', 'Legacy', ['asset1'], 'legacy-create')
        new = self.store.create_session('wtest', 'Legacy', ['asset1'], 'legacy-create', delivery_policy='auto')
        self.assertEqual(old['id'], new['id'])

    def test_policy_writes_require_reader_feature_before_persisting_new_semantics(self):
        task = self.task()
        with patch('workbench_platform.resources.reader_features.require_features') as feature:
            self.services.agent.tasks.update(self.ctx.session_id, {'request_id': 'protected-policy',
                'expected_revision': task['revision'], 'delivery_policy': 'reviewed'})
            feature.assert_called_with(self.store.root.parent, 'agent-multimodal-v1')

    def test_draft_inference_marks_new_reader_semantics_before_writing(self):
        self.task()
        with patch('workbench_platform.resources.reader_features.require_features') as feature:
            self.services.agent.tasks.record_goal(self.ctx.session_id, '本轮仅交付草稿', 'draft-user')
            feature.assert_called_with(self.store.root.parent, 'agent-multimodal-v1')
        task = self.task()
        with patch('workbench_platform.resources.reader_features.require_features') as feature:
            self.services.agent.tasks.update(self.ctx.session_id, {'request_id': 'draft-goal', 'expected_revision': task['revision'], 'goal': '只需草稿即可'})
            feature.assert_called_with(self.store.root.parent, 'agent-multimodal-v1')

    def test_legacy_auto_read_does_not_write_a_policy_marker(self):
        with patch('workbench_platform.resources.reader_features.require_features') as feature:
            task = self.task()
            self.assertEqual(task['delivery_policy'], 'auto')
            self.assertFalse(task['delivery_policy_resolution']['require_review'])
            feature.assert_not_called()

    def test_legacy_formal_goal_is_marked_before_a_new_write(self):
        self.task()
        with self.store.transaction() as db:
            body, revision = self.services.agent.tasks._body(db, self.ctx.session_id)
            body['goal'] = '请生成正式结果'
            with patch('workbench_platform.resources.reader_features.require_features') as feature:
                self.services.agent.tasks._save(db, self.ctx.session_id, body, revision)
                feature.assert_called_with(self.store.root.parent, 'agent-multimodal-v1')

    async def test_session_create_route_passes_user_policy(self):
        runtime = SimpleNamespace(store=self.store, services=self.services)
        with patch.object(routes, 'runtime', return_value=runtime), patch.object(routes, 'library', return_value=self.services.asset_library), \
             patch.object(routes, 'parsed', AsyncMock(return_value=AgentSessionCreate(name='Route policy', request_id='route-create', delivery_policy='reviewed'))):
            value = await routes.create_session('wtest', Mock())
        self.assertEqual(self.services.agent.tasks.get(value['id'])['delivery_policy'], 'reviewed')

    async def test_accepted_queued_user_message_updates_policy_from_user_text(self):
        runtime = SimpleNamespace(tasks=self.services.agent.tasks, submit=AsyncMock(return_value={'id': self.run['id']}))
        with patch.object(routes, 'runtime', return_value=runtime), patch.object(routes, 'session_scope'), \
             patch.object(routes, 'parsed', AsyncMock(return_value=AgentMessage(content='请生成正式核准成果', request_id='queued-user'))):
            await routes.message(self.ctx.session_id, Mock())
        self.assertTrue(self.task()['delivery_policy_resolution']['require_review'])
        self.assertEqual(self.task()['delivery_policy_inference']['request_id'], 'queued-user')

    def test_policy_contract_accepts_only_three_explicit_modes(self):
        self.assertEqual(AgentSessionCreate(request_id='request').delivery_policy, 'auto')
        with self.assertRaises(ValueError):
            AgentTaskWrite(request_id='request', expected_revision=1, delivery_policy='model-approved')

    def test_trusted_manual_return_summary_can_require_formal_review(self):
        tasks = self.services.agent.tasks
        task = self.task()
        tasks.control(self.ctx.session_id, {'request_id': 'manual-user', 'expected_revision': task['revision'], 'action': 'take_over'})
        task = self.task()
        tasks.return_results(self.ctx.session_id, {'request_id': 'return-user', 'expected_revision': task['revision'],
            'summary': '请沿本任务生成正式结果', 'references': [], 'include_asset_ids': []})
        self.assertTrue(self.task()['delivery_policy_resolution']['require_review'])
        self.assertEqual(self.task()['delivery_policy_inference']['source'], 'user_manual_return')

    def test_replaying_an_old_user_message_cannot_replace_a_newer_user_policy(self):
        tasks = self.services.agent.tasks
        tasks.record_goal(self.ctx.session_id, '仅交付草稿', 'old-draft-user')
        tasks.record_goal(self.ctx.session_id, '请生成正式结果', 'new-formal-user')
        tasks.record_goal(self.ctx.session_id, '仅交付草稿', 'old-draft-user')
        self.assertTrue(self.task()['delivery_policy_resolution']['require_review'])
        with self.assertRaises(Conflict):
            tasks.record_goal(self.ctx.session_id, '只要草稿就好', 'old-draft-user')

    def test_legacy_message_replay_cannot_override_newer_user_goal_setting(self):
        tasks = self.services.agent.tasks
        self.store.event(self.ctx.session_id, self.ctx.run_id, 'legacy-draft-user', 'message', {'role': 'user', 'content': '仅交付草稿'})
        task = self.task()
        tasks.update(self.ctx.session_id, {'request_id': 'new-formal-goal', 'expected_revision': task['revision'], 'goal': '请生成正式结果'})
        tasks.record_goal(self.ctx.session_id, '仅交付草稿', 'legacy-draft-user')
        self.assertTrue(self.task()['delivery_policy_resolution']['require_review'])

    def test_policy_replay_identity_is_reader_gated_before_its_first_insert(self):
        events = []
        connect = self.store.connect
        def traced_connect():
            db = connect()
            db.set_trace_callback(events.append)
            return db
        tasks = self.services.agent.tasks
        with patch.object(self.store, 'connect', side_effect=traced_connect), \
             patch('workbench_platform.resources.reader_features.require_features', side_effect=lambda *_: events.append('READER_FEATURE')):
            tasks.record_goal(self.ctx.session_id, '继续核对', 'first-policy-input')
        insertion = next(index for index, sql in enumerate(events) if sql.startswith('INSERT INTO audit_policy_user_inputs'))
        self.assertLess(events.index('READER_FEATURE'), insertion)
        events.clear()
        with patch.object(self.store, 'connect', side_effect=traced_connect), \
             patch('workbench_platform.resources.reader_features.require_features', side_effect=RuntimeError('reader feature refused')):
            with self.assertRaisesRegex(RuntimeError, 'reader feature refused'):
                tasks.record_goal(self.ctx.session_id, '继续核对', 'rejected-policy-input')
        self.assertFalse(any(sql.startswith('INSERT INTO audit_policy_user_inputs') for sql in events))
        self.assertEqual(self.store.rows('SELECT request_id FROM audit_policy_user_inputs WHERE request_id=?', ('rejected-policy-input',)), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)

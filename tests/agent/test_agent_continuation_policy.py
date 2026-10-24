"""Automatic progress stays within the original goal and recorded effects."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from workbench_platform.agent.delivery_policy import effective_policy, infer_user_delivery_policy
from workbench_platform.agent.goal_progress import observe, REPEAT_STOP_AT
from workbench_platform.agent.general_outputs import remaining_items
from workbench_platform.agent.task_recovery import retry_delay
from workbench_platform.agent.providers import ProviderFault
from workbench_platform.agent.harness_service import HarnessService
import test_user_flow_recovery_results as recovery


def receipt(**data):
    return {'status': 'success', 'summary': 'observed', 'data': data, 'result_refs': [], 'artifact_refs': []}


class ContinuationPolicyTests(unittest.TestCase):
    def test_formal_format_is_not_an_approval_request_for_new_tasks(self):
        for goal in ('请生成正式报告', '输出正式审计底稿', 'deliver a formal report', '生成正式报告，无需人工核准'):
            self.assertFalse(effective_policy({'goal': goal, 'delivery_policy_version': 2})['require_review'])
            self.assertIsNone(infer_user_delivery_policy(goal))
        for goal in ('人工确认后生成报告', '请生成正式报告，必须等待用户确认', 'after human approval', '人工复核通过后生成报告'):
            self.assertTrue(effective_policy({'goal': goal, 'delivery_policy_version': 2})['require_review'], goal)

    def test_saved_inference_setting_and_unmarked_legacy_keep_review(self):
        for task in ({'goal': '请生成正式报告'}, {'delivery_policy': 'reviewed', 'delivery_policy_version': 2},
                     {'delivery_policy_version': 2, 'delivery_policy_inference': {'policy': 'reviewed', 'source': 'user_goal'}}):
            self.assertTrue(effective_policy(task)['require_review'])

    def test_distinct_successful_exploration_is_not_a_repeated_loop(self):
        state = {}
        for i in range(30):
            observe(state, {'tool': 'tools_search', 'arguments': {'query': str(i)}}, receipt(tools=[str(i)]))
            self.assertLess(state.get('repeated_action', 0), REPEAT_STOP_AT)
        self.assertNotIn('progress_repair', state)

    def test_third_identical_call_warns_before_fourth_stops(self):
        state = {}
        call = {'tool': 'read_result', 'arguments': {'id': 'same'}}
        for i in range(1, 5):
            value = observe(state, call, receipt(text='same result'))
            self.assertEqual(state['repeated_action'], i)
            self.assertEqual('continuation_guard' in value['data'], i >= 3)
        observe(state, {'tool': 'read_result', 'arguments': {'id': 'different'}}, receipt(text='other'))
        self.assertEqual(state['repeated_action'], 1)

    def test_completed_asset_removes_only_its_previous_failure(self):
        results = [receipt(failures=[{'asset_id': 'a'}, {'asset_id': 'b'}]), receipt(completed_asset_ids=['a'])]
        self.assertEqual(remaining_items(results), [{'asset_id': 'b'}])

    def test_backoff_honors_retry_after_and_defaults_to_one_four(self):
        fault = ProviderFault('rate_limited', 'wait')
        self.assertEqual([retry_delay(fault, i) for i in (0, 1)], [1, 4])
        fault.response_diagnostics = {'retry_after': '7'}
        self.assertEqual(retry_delay(fault, 0), 7)
        fault.response_diagnostics = {'retry_after': 'invalid'}
        self.assertEqual(retry_delay(fault, 1), 4)

    def test_general_facts_do_not_read_or_copy_legacy_history(self):
        runtime = HarnessService()
        runtime.tasks = SimpleNamespace(profile=lambda _: {'rules': 'preserve identifiers'})
        runtime.session_facts = Mock(side_effect=AssertionError('legacy snapshot'))
        facts = runtime.general_facts({'workspace_id': 'w'}, {'scope_revision': 1, 'manual': False,
            'goal': 'x' * 10000, 'plan': [{'key': 'done', 'title': 'done', 'status': 'completed', 'result_refs': ['large']},
                                      {'key': 'next', 'title': 'next', 'status': 'pending', 'result_refs': ['large']}],
            'delivery_requirements': {'stages': ['large']}, 'delivery_policy_version': 2})
        self.assertNotIn('goal', facts['task'])
        self.assertNotIn('delivery_requirements', facts['task'])
        self.assertEqual(facts['task']['plan'], [{'key': 'next', 'title': 'next', 'status': 'pending'}])
        runtime.session_facts.assert_not_called()

    def test_general_initial_state_skips_audit_history_and_keeps_selected_scope(self):
        from workbench_platform.agent.general import CONTRACT
        from workbench_platform.agent.runtime import AgentRuntime
        session = {'id': 'session', 'workspace_id': 'w', 'selected_assets': ['asset'],
                   'initial_refs': [{'object_id': 'fixed-source'}]}
        runtime = object.__new__(AgentRuntime)
        runtime.store = SimpleNamespace(session=Mock(return_value=session),
            rows=Mock(side_effect=AssertionError('legacy history query')))
        runtime.registry = SimpleNamespace(tools={})
        runtime.session_facts = Mock(side_effect=AssertionError('legacy snapshot'))
        run = {'id': 'run', 'session_id': 'session', 'request_id': 'request', 'generation': 1,
               'driver_state': json.dumps({'execution_contract': CONTRACT})}
        state = runtime.initial_state(run, 'Only the current goal')
        self.assertEqual(state['execution_contract'], CONTRACT)
        self.assertFalse(state['business_started'])
        self.assertEqual([message['id'] for message in state['messages']], ['run:scope', 'request'])
        self.assertEqual(state['messages'][-1]['content'], 'Only the current goal')
        scope = json.loads(state['messages'][0]['content'].split('\n', 1)[1])
        self.assertEqual(scope['selected_assets'], ['asset'])
        self.assertEqual(scope['initial_refs'], session['initial_refs'])
        runtime.session_facts.assert_not_called()
        runtime.store.rows.assert_not_called()

    def test_legacy_initial_state_preserves_audit_history_and_previous_delivery(self):
        from workbench_platform.agent.runtime import AgentRuntime
        session = {'id': 'session', 'workspace_id': 'w', 'selected_assets': [], 'initial_refs': []}
        history = {'task': {'delivery_requirements': {'stages': ['fixed-stage']}}}
        runtime = object.__new__(AgentRuntime)
        runtime.store = SimpleNamespace(session=Mock(return_value=session),
            rows=Mock(return_value=[{'payload': '{"delivery":"fixed"}'}]))
        runtime.registry = SimpleNamespace(tools={})
        runtime.session_facts = Mock(return_value=history)
        run = {'id': 'run', 'session_id': 'session', 'request_id': 'request', 'generation': 1,
               'driver_state': '{}'}
        state = runtime.initial_state(run, 'Legacy goal')
        self.assertIsNone(state['execution_contract'])
        self.assertTrue(state['business_started'])
        self.assertEqual([message['id'] for message in state['messages']],
            ['run:scope', 'run:prior-delivery', 'run:session-facts', 'request'])
        self.assertIn('fixed-stage', state['messages'][2]['content'])
        self.assertIn('"delivery":"fixed"', state['messages'][1]['content'])
        runtime.session_facts.assert_called_once_with(run)


class RuntimeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.directory = tempfile.TemporaryDirectory()
        self.runtime, self.run, self.state = recovery.runtime_for(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    async def test_automatic_publication_reuses_unchanged_files_but_detects_edits(self):
        from pathlib import Path
        from workbench_platform.agent.workspace_outputs import automatic
        directory = Path(self.directory.name) / 'work'
        directory.mkdir()
        output = directory / 'answer.txt'
        output.write_text('first', encoding='utf-8')
        state = {'working_directory': str(directory)}
        runtime = SimpleNamespace(context=lambda _: None, registry=SimpleNamespace(dispatch=AsyncMock(return_value={})))
        await automatic(runtime, state, 'response-one')
        await automatic(runtime, state, 'response-two')
        calls = runtime.registry.dispatch.call_args_list
        self.assertEqual(calls[0].args[0]['call_id'], calls[1].args[0]['call_id'])
        output.write_text('changed', encoding='utf-8')
        await automatic(runtime, state, 'response-three')
        self.assertNotEqual(calls[0].args[0]['call_id'], runtime.registry.dispatch.call_args.args[0]['call_id'])

    async def test_provider_retries_twice_then_saves_original_goal(self):
        with patch('workbench_platform.agent.task_recovery.retry_delay', return_value=0):
            await self.runtime.drive(self.run['id'], None)
        self.assertEqual(self.runtime.graph.ainvoke.await_count, 3)
        self.assertEqual(self.runtime.store.run(self.run['id'])['status'], 'paused')
        self.assertEqual(len(self.runtime.store.rows('SELECT id FROM runs')), 1)

    async def test_rejected_invalid_request_does_not_retry(self):
        self.runtime.graph.ainvoke.side_effect = ProviderFault('provider_unavailable', 'invalid request', http_status=400)
        await self.runtime.drive(self.run['id'], None)
        self.assertEqual(self.runtime.graph.ainvoke.await_count, 1)

    async def test_unknown_write_prevents_automatic_model_retry(self):
        self.runtime.registry.tools['unsafe'] = SimpleNamespace(effect='versioned', idempotent=False, lookup_operation=None)
        self.runtime.store.begin_operation(self.run['id'], 1, {'call_id': 'uncertain', 'tool': 'unsafe', 'arguments': {}})
        await self.runtime.drive(self.run['id'], None)
        self.assertEqual(self.runtime.graph.ainvoke.await_count, 1)
        self.assertEqual(self.runtime.store.run(self.run['id'])['status'], 'needs_verification')

    async def test_automatic_retry_keeps_user_time_budget(self):
        from workbench_platform.agent.task_recovery import prepare_resume
        self.state['started_active_seconds'] = 18
        await prepare_resume(self.runtime, self.run, automatic=True)
        self.assertEqual(self.runtime.graph.load(self.run['id'])['started_active_seconds'], 18)

    async def test_pause_during_backoff_prevents_automatic_wakeup(self):
        original_sleep = asyncio.sleep
        async def stop(_):
            self.runtime.store.status(self.run['id'], 1, 'paused', 'user pause')
            await original_sleep(0)
        with patch('workbench_platform.agent.runtime.asyncio.sleep', side_effect=stop):
            await self.runtime.drive(self.run['id'], None)
        self.assertEqual(self.runtime.graph.ainvoke.await_count, 1)

    async def test_cancelled_generation_cannot_resume_resource_wait(self):
        self.runtime.wait_for_resource(self.run['id'], 1, 'busy')
        with self.runtime.store.transaction() as db:
            self.runtime.store.cancel_in_transaction(db, run_id=self.run['id'])
        self.assertFalse(self.runtime.resource_ready(self.runtime.store.run(self.run['id'])))
        self.runtime.schedule.assert_not_called()

    async def test_resource_wait_rechecks_claim_and_resumes_only_when_available(self):
        from workbench_platform.application_services.errors import ResourceBusy
        self.runtime.wait_for_resource(self.run['id'], 1, 'busy')
        with patch.object(self.runtime.store, 'claim_run_scope', side_effect=ResourceBusy('held')):
            self.assertFalse(self.runtime.resource_ready(self.runtime.store.run(self.run['id'])))
        with patch.object(self.runtime.store, 'claim_run_scope') as claim:
            self.assertTrue(self.runtime.resource_ready(self.runtime.store.run(self.run['id'])))
            claim.assert_called_once_with(self.run['id'], 1)
        self.assertEqual(self.runtime.store.run(self.run['id'])['status'], 'running')
        self.assertNotIn('resource_wait_generation', json.loads(self.runtime.store.run(self.run['id'])['driver_state']))

"""CPU control-flow regressions; fixture receipts are not real-chain scores."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest

from packages.contracts.agent import AgentToolResult
from workbench_platform.agent.graph import terminal_workflow_delivery
import test_specialized_runtime as hooks
from test_graph import response


def ref(kind, object_id, application='data', parent_id='current'):
    return {'kind': kind, 'object_id': object_id, 'workspace_id': 'wtest', 'revision': 1,
            'sha256': 'a' * 64 if kind != 'artifact' else None,
            'application': application, 'parent_id': parent_id}


def fixture(workflow='existing_ocr_reconciliation'):
    messages = []
    def pair(tool, arguments=None, data=None, result_refs=(), artifact_refs=()):
        call = {'call_id': 'fixture-' + str(len(messages)), 'tool': tool, 'arguments': arguments or {}}
        result = AgentToolResult(status='success', summary='CPU fixture', data=data or {},
            result_refs=list(result_refs), artifact_refs=list(artifact_refs)).model_dump()
        messages.extend([{'role': 'assistant', 'calls': [call], 'content': '', 'protocol': 'openai_chat_completions'},
                         {'role': 'tool', 'call_id': call['call_id'], 'result': result}])
        return call, result
    actual, evidence, artifact = ref('result', 'result'), ref('evidence', 'evidence', 'platform'), ref('artifact', 'bundle')
    if workflow == 'existing_ocr_reconciliation':
        pair('native_attach_result'); pair('results_describe'); pair('results_to_input')
        pair('data_create_session', data={'data_session_id': 'current'})
        for asset in ('left', 'right'):
            pair('data_inspect_input', {'asset_id': asset})
        for side in ('left', 'right'):
            pair('data_import', {'data_session_id': 'current', 'side': side})
            pair('data_read_table', {'data_session_id': 'current', 'side': side})
        pair('data_compare', {'data_session_id': 'current'})
        pair('data_read_result', {'data_session_id': 'current'}, result_refs=[actual])
    else:
        actual['application'] = 'classification'
        pair('classification_prepare'); pair('assets_create_set')
        pair('classification_infer', data={'handoff_id': 'current'})
        pair('classification_read_results', {'handoff_id': 'current'}, result_refs=[actual])
    pair('audit_bind_evidence')
    pair('audit_read_evidence', data={'stale': False}, result_refs=[evidence])
    pair('data_export' if workflow == 'existing_ocr_reconciliation' else 'classification_export',
         {'data_session_id' if workflow == 'existing_ocr_reconciliation' else 'handoff_id': 'current'}, artifact_refs=[artifact])
    delivery = AgentToolResult(status='success', summary='CPU verified delivery',
        result_refs=[actual, evidence], artifact_refs=[artifact],
        data={'verified': True, 'completeness': 'complete', 'continuing_job_refs': [],
              'delivery': [{'bundle_id': 'bundle', 'files': [{'name': 'results.csv'}]}]}).model_dump()
    call = {'call_id': 'terminal', 'tool': 'delivery_publish', 'arguments': {}}
    return {'messages': messages, 'calls': [call], 'results': [delivery], 'background_jobs': []}, delivery


class TerminalBindingTests(unittest.TestCase):
    def test_latest_exact_export_result_and_evidence_can_end_both_workflows(self):
        for workflow in ('existing_ocr_reconciliation', 'classification'):
            with self.subTest(workflow=workflow):
                state, delivery = fixture(workflow)
                self.assertTrue(terminal_workflow_delivery(state, delivery,
                    {'workflow': workflow, 'phase': 'verified_delivery'}))

    def test_unrelated_stale_partial_pending_and_paged_receipts_do_not_end(self):
        state, delivery = fixture()
        route = {'workflow': 'existing_ocr_reconciliation', 'phase': 'verified_delivery'}
        cases = []
        for field, value in (('verified', False), ('completeness', 'partial'), ('continuing_job_refs', [ref('job', 'pending')])):
            changed = deepcopy(delivery); changed['data'][field] = value
            changed_state = deepcopy(state); changed_state['results'] = [changed]
            cases.append((changed_state, changed, route))
        changed = deepcopy(state); changed['background_jobs'] = [{'operation_id': 'pending'}]; cases.append((changed, delivery, route))
        for field in ('artifact_refs', 'result_refs'):
            changed = deepcopy(delivery); changed[field][0]['object_id'] = 'old'
            changed_state = deepcopy(state); changed_state['results'] = [changed]; cases.append((changed_state, changed, route))
        for tool, field, value in (('audit_read_evidence', 'data', {'stale': True}),
                                   ('data_read_result', 'next_offset', 50), ('data_export', 'status', 'pending')):
            changed = deepcopy(state)
            call_ids = {c['call_id'] for m in changed['messages'] for c in m.get('calls', ()) if c['tool'] == tool}
            for message in changed['messages']:
                if message.get('role') == 'tool' and message['call_id'] in call_ids:
                    message['result'][field] = value
            cases.append((changed, delivery, route))
        cases.append((state, delivery, {'workflow': 'classification', 'phase': 'verified_delivery'}))
        cases.append((state, delivery, {'workflow': 'existing_ocr_reconciliation', 'phase': 'downloadable_export'}))
        for index, args in enumerate(cases):
            with self.subTest(case=index):
                self.assertFalse(terminal_workflow_delivery(*args))


class TerminalGraphTests(unittest.IsolatedAsyncioTestCase):
    setUp = hooks.SpecializedRuntimeTests.setUp
    tearDown = hooks.SpecializedRuntimeTests.tearDown

    async def scenario(self, *, append_goal=False):
        state_fixture, delivery = fixture()
        initial_state = self.runtime.initial_state
        def seeded(run, content):
            state = initial_state(run, content)
            state['messages'].extend(deepcopy(state_fixture['messages']))
            state['visible'].append('delivery_publish')
            return state
        self.runtime.initial_state = seeded
        calls = []
        async def provider(state, schemas):
            calls.append(state['step'])
            if len(calls) == 1:
                return response(state, 'delivery_publish')
            self.assertTrue(append_goal, 'Completed workflow requested another controller response')
            self.assertIn('新目标需要重新核实', [m.get('content') for m in state['messages']])
            return response(state, content='新目标尚未完成')
        async def publish(*_):
            if append_goal:
                await self.runtime.submit(self.session['id'], '新目标需要重新核实', 'appended-goal')
            return deepcopy(delivery)
        self.runtime.registry.tools['delivery_publish'] = replace(self.runtime.registry.tools['delivery_publish'],
            input_schema=hooks.Empty, execute=publish, availability=None, resource_keys=lambda *_: ())
        self.runtime.model_provider = provider
        await self.runtime.start()
        try:
            run = await self.runtime.submit(self.session['id'], '核对已有OCR结果与参考表并交付直接审计证据', 'terminal-goal')
            for _ in range(200):
                final = self.runtime.store.run(run['id'])
                if final['status'] not in {'running', 'waiting_job'}:
                    break
                await asyncio.sleep(.02)
            self.assertEqual(final['status'], 'incomplete' if append_goal else 'succeeded')
            self.assertEqual(len(calls), 2 if append_goal else 1)
            operations = self.runtime.store.rows('SELECT tool FROM operations WHERE run_id=?', (run['id'],))
            self.assertEqual([row['tool'] for row in operations], ['delivery_publish'])
            self.assertEqual(len(self.runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?', (run['id'],))), len(calls))
        finally:
            await self.runtime.close()

    async def test_terminal_workflow_ends_without_duplicate_controller_or_delivery(self):
        await self.scenario()

    async def test_goal_arriving_during_publish_requires_fresh_controller_and_delivery(self):
        await self.scenario(append_goal=True)


if __name__ == '__main__':
    unittest.main()

"""Real runtime entry hooks: bounded matching, frozen defaults and tool guards.

CPU only. No graph, controller, service or OCR model starts in these cases.
The asset library is a deterministic scope fixture; skill/store/registry are real.
"""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.context import core_tools, skill_control_tools
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.store import Conflict, digest
from workbench_platform.store import WorkspaceStore


ROOT = Path('F:/OCR/output/platform-specialized-agent-20261007/evidence/specialized-runtime')
SOURCE = Path(__file__).resolve().parents[2]


class Empty(AgentModel):
    pass


class Assets:
    def get(self, workspace_id, identifier):
        return SimpleNamespace(asset_id=identifier, revision=1, blob_hash=digest(identifier))


class Sets:
    def create(self, workspace_id, items, asset_set_id):
        return SimpleNamespace(asset_set_id=asset_set_id, revision=1, items=items,
                               manifest_hash=digest([item.model_dump() for item in items]))


class SpecializedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.skill_root = self.root / 'registered-skills'
        shutil.copytree(SOURCE / 'config/skills', self.skill_root)
        self.registration = patch('workbench_platform.agent.runtime.registration',
            return_value={'roots': [self.skill_root], 'workspace_roots': {}})
        self.registration.start()
        self.workspace = WorkspaceStore(self.root / 'workspace.sqlite3')
        self.workspace.create_workspace('runtime hook fixture', workspace_id='wtest')
        self.services = SimpleNamespace(workspace_store=self.workspace,
                                        asset_library=(Assets(), Sets(), None))
        self.runtime = AgentRuntime(self.root / 'agent', self.services,
                                   model_provider=lambda *_: self.fail('controller must not run'))
        self.services.agent = self.runtime
        self.session = self.runtime.store.create_session('wtest', '图片任务',
            ['image' + str(index) for index in range(200)], 'session-main')
        self.executed = []

    def tearDown(self):
        self.registration.stop()
        self.temporary.cleanup()

    def start(self, content, request_id='run-main'):
        run, _ = self.runtime.store.start_run(self.session['id'], content, request_id, 0)
        self.runtime.tasks.record_goal(self.session['id'], content)
        return run, self.runtime.initial_state(run, content)

    def save(self, values, revision=0, request_id='defaults'):
        return self.runtime.preferences.update('wtest', 'screen_excel', revision, values, [],
            {'mode': 'user_form', 'quote': '用户在默认参数表单保存'}, request_id)

    def instrument(self, tool):
        self.runtime.registry.tools[tool] = replace(self.runtime.registry.tools[tool], input_schema=Empty,
            execute=lambda *_: (self.executed.append(tool) or
                               AgentToolResult(status='success', summary='fixture').model_dump()),
            availability=None, resource_keys=lambda *_: (), lookup_operation=None)

    async def test_single_action_selects_registered_skill_and_keeps_large_scope_bounded(self):
        run, state = self.start('帮我将这200张图片转Excel，每张图片单独一个Excel文件')
        active = self.runtime.skills.active_manifest(self.session['id'])
        self.assertEqual([item['name'] for item in active], ['screen-batch-excel'])
        self.assertIn('ocr_screen_batch_status', state['visible'])
        self.assertNotIn('classification_train', state['visible'])
        scope = json.loads(next(item['content'] for item in state['messages']
                                if item['id'].endswith(':scope')).split('\n', 1)[1])
        self.assertEqual(scope['selected_asset_count'], 200)
        self.assertEqual(scope['selected_asset_set']['item_count'], 200)
        self.assertEqual(len(scope['selected_assets']), 10)
        snapshots = self.runtime.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots']
        self.assertEqual(snapshots[0]['overrides'], {'output_layout': 'per_image'})
        self.assertIsNone(self.runtime.graph)
        self.assertFalse(self.runtime.running)
        self.assertEqual(self.runtime.store.rows('SELECT * FROM operations'), [])

    async def test_mixed_goal_retains_discovery_without_auto_activating_one_workflow(self):
        run, state = self.start('图片转Excel，再核对金额并生成底稿')
        matching = self.runtime.skills.match('wtest', self.session['id'],
                                             '图片转Excel，再核对金额并生成底稿')
        self.assertEqual(matching['match'], 'mixed')
        self.assertEqual(self.runtime.skills.active_manifest(self.session['id']), [])
        self.assertEqual(state['visible'], core_tools(self.runtime.registry))
        self.assertIn('tools_list', state['visible'])
        self.assertNotIn('data_compare', state['visible'])
        self.assertEqual(self.runtime.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots'], [])

    async def test_existing_ocr_entry_is_staged_pinned_and_does_not_grant_execution(self):
        goal = '请核对已有OCR结果与reference.csv并交付直接审计证据，禁止重新OCR'
        run, state = self.start(goal)
        active = self.runtime.skills.active_manifest(self.session['id'])
        self.assertEqual([item['name'] for item in active], ['existing-ocr-reconciliation'])
        self.assertEqual(set(state['visible']) - set(core_tools(self.runtime.registry)),
                         {'native_attach_result', 'results_describe'})
        self.assertNotIn('data_compare', state['visible'])
        self.assertNotIn('audit_bind_evidence', state['visible'])
        allowed = self.runtime.skills.allowed_tools(self.session['id'], self.runtime.registry.tools)
        self.assertTrue({'results_to_input', 'data_compare', 'audit_read_evidence', 'delivery_publish'} <= allowed)
        self.assertFalse({'ocr_run', 'ocr_edit_result', 'classification_train', 'data_apply_step'} & allowed)
        snapshots = self.runtime.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots']
        self.assertEqual([item['business'] for item in snapshots], ['existing_ocr_reconciliation'])
        self.instrument('native_attach_result')
        ctx = self.runtime.context({**state, 'step': 1})
        blocked = await self.runtime.registry.dispatch(
            {'call_id': 'existing-readonly', 'tool': 'native_attach_result', 'arguments': {}},
            replace(ctx, grants=frozenset({'read'})))
        self.assertEqual(blocked['error']['code'], 'scope_denied')
        self.assertEqual(self.executed, [])
        before = self.runtime.skills.request_snapshot('wtest', self.session['id'])
        skill_file = self.skill_root / 'existing-ocr-reconciliation/SKILL.md'
        skill_file.write_text(skill_file.read_text(encoding='utf-8') + '\n后续源变化不能改已固定任务。\n', encoding='utf-8')
        after = self.runtime.skills.request_snapshot('wtest', self.session['id'])
        self.assertEqual(before.instructions, after.instructions)
        self.assertEqual(before.active[0]['version'], after.active[0]['version'])
        self.assertTrue(after.active[0]['source_changed'])

    async def test_existing_bridge_contract_survives_successful_catalog_under_budget(self):
        run, state = self.start('核对已有OCR结果与参考表并交付直接审计证据')
        state['messages'].append({'id': 'background', 'role': 'user', 'content': '背景文字' * 600})
        def pair(tool, result):
            call_id = 'phase-' + str(len(state['messages']))
            state['messages'].extend([
                {'id': call_id, 'role': 'assistant', 'protocol': 'openai_chat_completions', 'content': '', 'calls': [{'call_id': call_id, 'tool': tool, 'arguments': {}}]},
                {'id': call_id + ':result', 'role': 'tool', 'call_id': call_id, 'result': result}])
        pair('native_attach_result', AgentToolResult(status='success', summary='原图已核').model_dump())
        contract = self.runtime.registry.schemas(['results_to_input'])['results_to_input']
        pair('tools_describe', AgentToolResult(status='success', summary='已实际加载桥工具', data={
            'format': 'platform-tool-activation-v1', 'activated_tools': ['results_to_input'],
            'tools': [{'name': 'results_to_input', 'allowed': True, 'loaded': True, 'schema_sha256': digest(contract)}]}).model_dump())
        pair('results_describe', AgentToolResult(status='success', summary='真实表目录已读').model_dump())
        state['visible'].append('results_to_input')
        state['step'] = 3
        captured = []
        async def provider(_, schemas):
            captured.append(schemas)
            return {'id': 'bounded-phase-reply', 'role': 'assistant', 'content': '', 'calls': [],
                    'usage': {'input_tokens': 0, 'output_tokens': 0}}
        self.runtime.model_provider = provider
        result = await self.runtime.model(state)
        self.assertIn('results_to_input', result['selected_tools'])
        self.assertIn('results_to_input', captured[0])
        self.assertNotIn('data_compare', captured[0])
        event = json.loads(self.runtime.store.rows("SELECT payload FROM events WHERE kind='model_request'")[-1]['payload'])
        self.assertLessEqual(event['context_metrics']['input_tokens_estimate'], 14336)
        self.assertTrue(event['context_metrics']['reclaimed_tools'])
        self.assertEqual(self.runtime.store.rows('SELECT * FROM operations'), [])

    async def test_verified_phase_exposes_only_authorized_next_contract_without_discovery_call(self):
        run, state = self.start('核对已有OCR结果与参考表并交付直接审计证据')
        for tool in ('native_attach_result', 'results_describe'):
            state['messages'].extend([
                {'role': 'assistant', 'protocol': 'openai_chat_completions', 'content': '',
                 'calls': [{'call_id': tool, 'tool': tool, 'arguments': {}}]},
                {'role': 'tool', 'call_id': tool,
                 'result': AgentToolResult(status='success', summary='真实来源阶段已完成').model_dump()}])
        self.assertNotIn('results_to_input', state['visible'])
        state['step'] = 2
        captured = []
        async def provider(_, schemas):
            captured.append(schemas)
            return {'role': 'assistant', 'content': '', 'calls': [], 'usage': {}}
        self.runtime.model_provider = provider
        result = await self.runtime.model(state)
        self.assertIn('results_to_input', result['selected_tools'])
        self.assertIn('results_to_input', captured[0])
        self.assertNotIn('data_compare', captured[0])
        self.assertNotIn('ocr_run', captured[0])
        self.assertEqual(self.runtime.store.rows('SELECT * FROM operations'), [])
        self.assertEqual([item['name'] for item in self.runtime.skills.active_manifest(self.session['id'])],
                         ['existing-ocr-reconciliation'])

    async def test_information_goal_never_activates_skill_or_submits_work(self):
        run, state = self.start('请介绍如何把图片转Excel，评估提升方案')
        self.assertEqual(self.runtime.skills.match('wtest', self.session['id'],
            '请介绍如何把图片转Excel，评估提升方案')['intent'], 'information')
        self.assertEqual(self.runtime.skills.active_manifest(self.session['id']), [])
        self.assertEqual(self.runtime.store.rows('SELECT * FROM operations'), [])
        self.assertEqual(self.runtime.store.rows('SELECT * FROM model_requests'), [])
        self.assertIn('conversation_answer', state['visible'])
        self.assertFalse(self.runtime.running)

    async def test_completed_prediction_and_evidence_phases_reclaim_controls_but_keep_next_and_exact_history(self):
        for phase, next_tool in (('actual_predictions', 'classification_read_results'),
                                 ('direct_evidence', 'audit_bind_evidence'),
                                 ('evidence_readback', 'audit_read_evidence'),
                                 ('downloadable_export', 'classification_export')):
            with self.subTest(phase=phase):
                reclaimable = set(self.runtime.route_reclaimable_core({'phase': phase, 'next_tools': [next_tool]}))
                self.assertTrue({'conversation_answer', 'tools_list', 'ask_user'} <= reclaimable)
                self.assertFalse({'tools_describe', 'session_read_history', next_tool} & reclaimable)
        self.assertEqual(self.runtime.store.rows('SELECT id FROM operations'), [])
        self.assertIsNone(self.runtime.graph)

    async def test_existing_skill_restriction_survives_screen_match_and_visible_core_schema(self):
        self.runtime.skills.activate('wtest', self.session['id'], 'invoice-ocr-reconciliation', 'prior-skill')
        allowed_before = self.runtime.skills.allowed_tools(self.session['id'], self.runtime.registry.tools,
                                                         core=skill_control_tools(self.runtime.registry))
        run, state = self.start('帮我将图片转Excel')
        self.assertEqual([item['name'] for item in self.runtime.skills.active_manifest(self.session['id'])],
                         ['invoice-ocr-reconciliation'])
        self.assertEqual(self.runtime.skills.allowed_tools(self.session['id'], self.runtime.registry.tools,
            core=skill_control_tools(self.runtime.registry)), allowed_before)
        self.instrument('ocr_screen_batch_run')
        # The legacy core can expose a schema; invocation still enforces the
        # prior skill. Schema visibility cannot authorize a disallowed tool.
        self.assertIn('ocr_screen_batch_run', state['visible'])
        result = await self.runtime.registry.dispatch({'call_id': 'blocked-screen',
            'tool': 'ocr_screen_batch_run', 'arguments': {}}, self.runtime.context(state))
        self.assertEqual(result['error']['code'], 'skill_tool_denied')
        self.assertEqual(self.executed, [])

    async def test_auto_activation_cannot_expand_read_only_scope_or_cross_skill_tools(self):
        _, state = self.start('帮我将图片转Excel')
        self.instrument('ocr_screen_batch_run')
        context = replace(self.runtime.context(state), grants=frozenset({'read'}))
        blocked = await self.runtime.registry.dispatch({'call_id': 'read-only',
            'tool': 'ocr_screen_batch_run', 'arguments': {}}, context)
        self.assertEqual(blocked['error']['code'], 'scope_denied')
        self.instrument('classification_train')
        unrelated = await self.runtime.registry.dispatch({'call_id': 'cross-skill',
            'tool': 'classification_train', 'arguments': {}}, self.runtime.context(state))
        self.assertEqual(unrelated['error']['code'], 'skill_tool_denied')
        self.assertEqual(self.executed, [])
        self.assertEqual(context.grants, frozenset({'read'}))

    async def test_missing_required_registration_does_not_claim_auto_execution_ready(self):
        self.runtime.registry.tools.pop('ocr_screen_batch_run')
        _, state = self.start('帮我将图片转Excel')
        matching = self.runtime.skills.match('wtest', self.session['id'], '帮我将图片转Excel')
        self.assertFalse(matching['workflows'][0]['can_select'])
        self.assertIsNone(matching['auto_skill_candidate'])
        self.assertEqual(self.runtime.skills.active_manifest(self.session['id']), [])
        self.assertNotIn('ocr_screen_batch_run', state['visible'])

    async def test_one_task_override_is_frozen_and_does_not_save_as_new_default(self):
        self.save({'value_mode': 'confirmed_numeric', 'include_pending_review': False})
        content = '帮我将图片转Excel，原值按文本保留，包含待复核'
        run, _ = self.start(content)
        frozen = self.runtime.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots'][0]
        self.assertEqual(frozen['values'], {'value_mode': 'text', 'include_pending_review': True})
        self.assertEqual(self.runtime.preferences.preview('wtest', 'screen_excel')['effective_values'],
                         {'value_mode': 'confirmed_numeric', 'include_pending_review': False})
        self.save({'include_pending_review': True}, revision=1, request_id='later-default')
        self.runtime.store.status(run['id'], run['generation'], 'answered')
        next_run, _ = self.start('帮我将图片转Excel', 'run-next')
        next_snapshot = self.runtime.preferences.snapshots('wtest', self.session['id'], next_run['id'])['snapshots'][0]
        self.assertEqual(next_snapshot['values'], {'value_mode': 'confirmed_numeric', 'include_pending_review': True})
        self.assertEqual(self.runtime.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots'][0], frozen)

    async def test_cold_reentry_preserves_frozen_skill_bytes_defaults_and_scope(self):
        self.save({'value_mode': 'confirmed_numeric', 'include_pending_review': False})
        content = '帮我将图片转Excel，原值按文本保留'
        run, state = self.start(content)
        frozen = self.runtime.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots'][0]
        active = self.runtime.skills.request_snapshot('wtest', self.session['id'])
        self.save({'value_mode': 'confirmed_numeric', 'include_pending_review': True},
                  revision=1, request_id='changed-default')
        skill_file = self.skill_root / 'screen-batch-excel/SKILL.md'
        skill_file.write_text(skill_file.read_text(encoding='utf-8') + '\n新源版本，不应影响当前任务。\n', encoding='utf-8')
        reopened = AgentRuntime(self.runtime.store.root, self.services,
                                model_provider=lambda *_: self.fail('controller must not run'))
        self.services.agent = reopened
        replayed = reopened.initial_state(reopened.store.run(run['id']), content)
        restored = reopened.skills.request_snapshot('wtest', self.session['id'])
        self.assertEqual(restored.active[0]['version'], active.active[0]['version'])
        self.assertTrue(restored.active[0]['source_changed'])
        self.assertEqual(restored.instructions, active.instructions)
        self.assertNotIn('新源版本', restored.instructions)
        self.assertEqual(reopened.preferences.snapshots('wtest', self.session['id'], run['id'])['snapshots'][0], frozen)
        original_scope = next(item for item in state['messages'] if item['id'].endswith(':scope'))
        restored_scope = next(item for item in replayed['messages'] if item['id'].endswith(':scope'))
        self.assertEqual(restored_scope, original_scope)
        self.assertEqual(reopened.store.rows('SELECT * FROM operations'), [])
        with self.assertRaises(Conflict):
            reopened.initial_state(reopened.store.run(run['id']), '帮我将图片转Excel，包含待复核')


if __name__ == '__main__':
    unittest.main()

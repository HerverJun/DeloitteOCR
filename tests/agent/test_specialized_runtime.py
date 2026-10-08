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

    async def test_information_goal_never_activates_skill_or_submits_work(self):
        run, state = self.start('请介绍如何把图片转Excel，评估提升方案')
        self.assertEqual(self.runtime.skills.match('wtest', self.session['id'],
            '请介绍如何把图片转Excel，评估提升方案')['intent'], 'information')
        self.assertEqual(self.runtime.skills.active_manifest(self.session['id']), [])
        self.assertEqual(self.runtime.store.rows('SELECT * FROM operations'), [])
        self.assertEqual(self.runtime.store.rows('SELECT * FROM model_requests'), [])
        self.assertIn('conversation_answer', state['visible'])
        self.assertFalse(self.runtime.running)

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

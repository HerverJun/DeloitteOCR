"""Scope, unknown capability and immutable defaults regression cases (CPU only)."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx
from fastapi import FastAPI

from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.matching import WORKFLOWS, match_goal, readiness_projection
from workbench_platform.agent.preferences import TaskPreferences, PreferenceValues, task_overrides
from workbench_platform.agent.registry import ToolContext, ToolRegistry, ToolSpec
from workbench_platform.agent.skills import SkillService
from workbench_platform.agent.skills_routes import router
from workbench_platform.agent.store import AgentStore, Conflict, RunSuperseded
from workbench_platform.agent.tools import skills, preferences

ROOT = Path('F:/OCR/output/platform-specialized-agent-20261007/evidence/matching-preferences')
SOURCE = Path(__file__).resolve().parents[2]


class Empty(AgentModel):
    pass


class SpecializedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.store = AgentStore(self.root / 'agent')
        self.session = self.store.create_session('wtest', '业务任务', ['image1'], 'session-main')
        self.other = self.store.create_session('wother', '其他项目', [], 'session-other')
        self.registry = ToolRegistry()
        names = {name for spec in WORKFLOWS.values() for name in spec['tools']}
        for path in (SOURCE / 'config/skills').glob('*/SKILL.md'):
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.startswith('allowed-tools:'):
                    names.update(line.split(':', 1)[1].split())
        reserved = {'preferences_read', 'preferences_update', 'capabilities_read', 'skills_match',
                    'skills_list', 'skills_activate', 'skills_deactivate', 'skills_read_resource'}
        for name in sorted(names - reserved):
            self.registry.register(ToolSpec(name, '真实注册业务工具', Empty,
                lambda *_: AgentToolResult(status='success', summary='真实工具回执').model_dump(),
                availability=lambda _: {'state': 'preparable', 'reason': '已安装服务未运行'}))
        skills.register(self.registry)
        preferences.register(self.registry)
        self.skills = SkillService(self.store, [SOURCE / 'config/skills'], registry=self.registry)
        self.preferences = TaskPreferences(self.store)
        self.agent = SimpleNamespace(store=self.store, registry=self.registry,
                                     skills=self.skills, preferences=self.preferences)
        self.services = SimpleNamespace(agent=self.agent,
            workspace_store=SimpleNamespace(get_workspace=lambda key: object() if key in ('wtest', 'wother') else None))

    def tearDown(self):
        self.temporary.cleanup()

    def start(self, content='默认以后每张图片一个Excel，原值按文本保留'):
        run, _ = self.store.start_run(self.session['id'], content, 'run-main', 1)
        self.run = run
        self.ctx = ToolContext('wtest', self.session['id'], run['id'], run['generation'], 1,
                               'preference-op', ('image1',), self.services, self.store)
        self.seq = self.store.rows('SELECT seq FROM events WHERE run_id=? AND kind=?',
                                   (run['id'], 'message'))[0]['seq']
        return run

    def save(self, values, revision=0, request='save-default', business='screen_excel', **kwargs):
        return self.preferences.update('wtest', business, revision, values, [],
            {'mode': 'user_form', 'quote': '用户在默认参数表单明确保存'}, request, **kwargs)

    async def test_screen_skill_and_matching_preserve_old_skill_and_authority(self):
        matched = self.skills.match('wtest', self.session['id'], '这批200张图片转Excel，每张一个文件')
        self.assertEqual(matched['match'], 'single')
        self.assertEqual(matched['auto_skill_candidate'], 'screen-batch-excel')
        self.assertFalse(matched['grants_changed'])
        self.assertFalse(matched['schemas_activated'])
        self.assertEqual(self.skills.active_manifest(self.session['id']), [])
        self.skills.activate('wtest', self.session['id'], 'invoice-ocr-reconciliation', 'invoice')
        before = self.skills.allowed_tools(self.session['id'], self.registry.tools)
        blocked = self.skills.match('wtest', self.session['id'], '这批图片转Excel')
        self.assertIsNone(blocked['auto_skill_candidate'])
        self.assertIn('ocr_screen_batch_run', blocked['workflows'][0]['blocked_by_active_skills'])
        self.assertEqual(self.skills.allowed_tools(self.session['id'], self.registry.tools), before)
        self.skills.deactivate('wtest', self.session['id'], 'invoice-ocr-reconciliation', 'stop-invoice')
        receipt = self.skills.activate('wtest', self.session['id'], 'screen-batch-excel', 'screen')
        allowed = self.skills.allowed_tools(self.session['id'], self.registry.tools)
        self.assertIn('ocr_screen_batch_run', allowed)
        self.assertNotIn('classification_train', allowed)
        self.assertEqual(receipt['skill']['name'], 'screen-batch-excel')
        self.assertTrue((SOURCE / 'config/skills/invoice-ocr-reconciliation/SKILL.md').is_file())

    async def test_mixed_unknown_negated_and_training_targets_do_not_force_workflow(self):
        mixed = match_goal('图片转Excel，再核对金额并生成底稿', self.registry)
        self.assertEqual(mixed['match'], 'mixed')
        self.assertIsNone(mixed['auto_skill_candidate'])
        self.assertEqual({row['id'] for row in mixed['workflows']},
                         {'screen_excel', 'payment_reconciliation', 'evidence_workpaper'})
        for goal in ('请研究一个新的算法', '不要图片转Excel。只讨论原理', '训练图片分类模型'):
            self.assertEqual(match_goal(goal, self.registry)['match'], 'unknown')
        information = self.skills.match('wtest', self.session['id'], '介绍如何把图片转Excel')
        self.assertEqual(information['intent'], 'information')
        self.assertIsNone(information['auto_skill_candidate'])
        self.assertIsNone(self.skills.match('wtest', self.session['id'], '帮我介绍如何图片转Excel')['auto_skill_candidate'])
        action = self.skills.match('wtest', self.session['id'], '可以帮我将这批图片转Excel吗')
        self.assertEqual(action['intent'], 'action')
        self.assertEqual(action['auto_skill_candidate'], 'screen-batch-excel')
        missing = self.registry.tools.pop('ocr_screen_batch_run')
        matched = match_goal('图片转Excel', self.registry)
        self.assertFalse(matched['workflows'][0]['can_select'])
        self.assertEqual(matched['workflows'][0]['missing_required_tools'], ['ocr_screen_batch_run'])
        self.registry.tools[missing.name] = missing

    async def test_registration_service_and_model_readiness_do_not_conflate(self):
        self.start()
        rows = [{'id': 'registered-only', 'role': 'ocr_engine', 'ready': True,
                 'details': {'model_path': 'private-path'}, 'reason': ''},
                {'id': 'unavailable', 'role': 'ocr_engine', 'ready': False,
                 'details': {'ready': False}, 'reason': '模型未准备'},
                {'id': 'catalog-ready', 'role': 'ocr_engine', 'ready': True,
                 'details': {'ready': True}, 'version_ref': {'package_id': 'native'}}]
        value = readiness_projection(self.ctx, names=['ocr_screen_batch_run', 'nonexistent_tool'], models=rows)
        self.assertTrue(value['tools'][0]['registered'])
        self.assertEqual(value['tools'][0]['service_state'], 'preparable')
        self.assertFalse(value['tools'][1]['registered'])
        self.assertEqual([row['model_state'] for row in value['models']], ['unknown', 'unavailable', 'ready'])
        self.assertTrue(value['models'][0]['queue_submittable'])
        self.assertTrue(all(row['runtime_file_validation_required'] for row in value['models']))
        self.assertNotIn('private-path', json.dumps(value))
        self.assertFalse(value['starts_services'])
        self.assertFalse(value['probes_model_execution'])

    async def test_defaults_scoped_versioned_revocable_and_request_identity_checked(self):
        self.save({'value_mode': 'text'}, business='project')
        self.save({'include_pending_review': True, 'output_layout': 'per_image'}, request='business')
        read = self.preferences.preview('wtest', 'screen_excel')
        self.assertEqual(read['effective_values']['value_mode'], 'text')
        self.assertTrue(read['effective_values']['include_pending_review'])
        self.assertNotIn('include_pending_review', self.preferences.preview('wtest', 'classification')['effective_values'])
        self.assertEqual(self.preferences.preview('wother', 'screen_excel')['effective_values'], {})
        self.assertEqual(self.save({'value_mode': 'text'}, business='project')['revision'], 1)
        with self.assertRaises(Conflict):
            self.save({'value_mode': 'confirmed_numeric'}, business='project')
        with self.assertRaises(Conflict):
            self.save({'value_mode': 'confirmed_numeric'}, business='project', request='stale')
        revoked = self.preferences.update('wtest', 'screen_excel', 1, {}, ['include_pending_review'],
            {'mode': 'user_form', 'quote': '撤回待核默认'}, 'revoke')
        self.assertEqual(revoked['revision'], 2)
        self.assertNotIn('include_pending_review', self.preferences.preview('wtest', 'screen_excel')['effective_values'])
        self.assertEqual(self.preferences.preview('wtest', 'screen_excel')['profile']['entries']['output_layout']['source']['actor'], 'user')

    async def test_run_snapshot_single_override_survives_default_edit_and_cold_reopen(self):
        self.save({'value_mode': 'confirmed_numeric', 'include_pending_review': False})
        self.start()
        frozen = self.preferences.freeze('wtest', self.session['id'], self.run['id'], 'screen_excel', {'value_mode': 'text'})
        self.assertEqual(frozen['values']['value_mode'], 'text')
        self.save({'include_pending_review': True}, revision=1, request='later')
        reopened = TaskPreferences(AgentStore(self.store.root))
        self.assertEqual(reopened.freeze('wtest', self.session['id'], self.run['id'], 'screen_excel'), frozen)
        self.assertFalse(frozen['values']['include_pending_review'])
        with self.assertRaises(Conflict):
            reopened.freeze('wtest', self.session['id'], self.run['id'], 'screen_excel', {'value_mode': 'confirmed_numeric'})
        with self.assertRaises(ValueError):
            reopened.freeze('wother', self.other['id'], self.run['id'], 'screen_excel')
        with self.assertRaises(ValueError):
            reopened.snapshots('wtest', self.other['id'])
        with self.store.transaction() as db:
            db.execute('UPDATE agent_preference_snapshots SET sha256=?', ('0' * 64,))
        with self.assertRaises(Conflict):
            reopened.snapshots('wtest', self.session['id'])

    async def test_single_task_formatting_does_not_save_defaults_or_authorize_numeric_signoff(self):
        parsed = task_overrides('每张图片单独一个Excel文件，原值按文本保留，包含待复核', 'screen_excel')
        self.assertEqual(parsed, {'output_layout': 'per_image', 'value_mode': 'text', 'include_pending_review': True})
        self.assertEqual(task_overrides('不要文本导出。先讨论规则', 'screen_excel'), {})
        self.assertEqual(task_overrides('每张图片一个Excel', 'classification'), {})
        self.assertEqual(task_overrides('只导出已确认，排除待复核', 'screen_excel'), {'include_pending_review': False})
        self.assertNotIn('numeric_columns', task_overrides('金额按数值导出，不用人工确认', 'screen_excel'))
        self.start()
        snap = self.preferences.freeze('wtest', self.session['id'], self.run['id'], 'screen_excel', parsed)
        self.assertEqual(snap['values'], parsed)
        self.assertEqual(self.preferences.preview('wtest', 'screen_excel')['profile']['revision'], 0)
        self.store.status(self.run['id'], self.run['generation'], 'cancelled')
        self.assertEqual(self.preferences.freeze('wtest', self.session['id'], self.run['id'], 'screen_excel'), snap)
        with self.assertRaises(RunSuperseded):
            self.preferences.freeze('wtest', self.session['id'], self.run['id'], 'payment_reconciliation')

    async def test_agent_defaults_need_real_user_source_and_cannot_sign_review(self):
        self.start()
        origin = {'mode': 'user_event', 'event_seq': self.seq, 'quote': '默认以后每张图片一个Excel，原值按文本保留'}
        saved = self.preferences.update('wtest', 'screen_excel', 0, {'value_mode': 'text'}, [], origin,
                                        'agent-default', context=self.ctx)
        self.assertEqual(saved['entries']['value_mode']['source']['event_seq'], self.seq)
        self.assertEqual(self.preferences.update('wtest', 'screen_excel', 0, {'value_mode': 'text'}, [], origin,
                        'agent-default', context=self.ctx), saved)
        for source in ({'mode': 'user_form', 'quote': 'agent想保存'},
                       {'mode': 'user_event', 'event_seq': self.seq, 'quote': '默认不用人工核对'},
                       {'mode': 'user_event', 'event_seq': self.seq, 'quote': '每张图片一个Excel'}):
            with self.assertRaises(ValueError):
                self.preferences.update('wtest', 'screen_excel', 1, {'include_pending_review': True}, [],
                                        source, 'bad-source-' + str(len(source['quote'])), context=self.ctx)
        for values in ({'require_human_review': False}, {'numeric_columns': {'0': [1]}},
                       {'strategy_id': 'invented-engine'}, {'include_pending_review': 'true'}):
            with self.assertRaises(ValueError):
                PreferenceValues.model_validate(values)
        with self.assertRaises(RunSuperseded):
            self.preferences.update('wtest', 'screen_excel', 1, {'value_mode': 'text'}, [], origin,
                                    'stale-generation', context=replace(self.ctx, generation=999))

    async def test_registry_default_write_obeys_scope_and_real_source(self):
        self.start()
        call = {'call_id': 'saved-default', 'tool': 'preferences_update', 'arguments': {
            'business': 'screen_excel', 'expected_revision': 0, 'values': {'value_mode': 'text'},
            'source': {'mode': 'user_event', 'event_seq': self.seq,
                       'quote': '默认以后每张图片一个Excel，原值按文本保留'}}}
        blocked = await self.registry.dispatch(call, replace(self.ctx, grants=frozenset({'read'})))
        self.assertEqual(blocked['error']['code'], 'scope_denied')
        self.assertEqual(self.preferences.preview('wtest', 'screen_excel')['profile']['revision'], 0)
        result = await self.registry.dispatch(call, self.ctx)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['data']['revision'], 1)
        self.assertEqual(await self.registry.dispatch(call, self.ctx), result)
        self.assertEqual(self.preferences.preview('wtest', 'screen_excel')['profile']['revision'], 1)

    async def test_preference_api_view_change_revoke_and_write_origin(self):
        app = FastAPI()
        app.include_router(router)
        app.state.agent = self.agent
        app.state.asset_library = (object(), object(), object())
        app.state.workspace_store = SimpleNamespace(get_workspace=lambda key: object() if key in ('wtest', 'wother') else None)
        path = '/api/platform/v1/workspaces/wtest/agent/preferences'
        body = {'request_id': 'form1', 'business': 'screen_excel', 'expected_revision': 0,
                'values': {'value_mode': 'text'}, 'source': {'mode': 'user_form', 'quote': '保存默认文本导出'}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:9908') as client:
            self.assertEqual((await client.post(path, json=body)).status_code, 403)
            headers = {'origin': 'http://127.0.0.1:9908'}
            self.assertEqual((await client.post(path, json=body, headers=headers)).status_code, 200)
            self.assertEqual((await client.get(path, params={'business': 'screen_excel'})).json()['profile']['revision'], 1)
            body.update(request_id='form2', expected_revision=1, values={}, revoke_keys=['value_mode'])
            self.assertEqual((await client.post(path, json=body, headers=headers)).status_code, 200)
            self.assertEqual((await client.get(path, params={'business': 'screen_excel'})).json()['effective_values'], {})
            body.update(request_id='form3', expected_revision=2, values={'value_mode': 'text'}, revoke_keys=[],
                        source={'mode': 'user_event', 'quote': '伪造用户'})
            self.assertEqual((await client.post(path, json=body, headers=headers)).status_code, 400)


if __name__ == '__main__':
    unittest.main()

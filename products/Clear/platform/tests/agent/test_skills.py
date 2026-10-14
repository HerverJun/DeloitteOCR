"""Skill host invariants and four real business-tool continuations, no model calls."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from packages.contracts.agent import AgentModel, AgentToolResult
from workbench_platform.agent.registry import ToolContext, ToolRegistry, ToolSpec
from workbench_platform.agent.skills import SkillService, registration
from workbench_platform.agent.skills_routes import router
from workbench_platform.agent.store import AgentStore, Conflict, RunSuperseded
from workbench_platform.agent.tools import skills

ROOT = Path('F:/OCR/output/agent-context-skills-development-20261005/evidence/skills')
SOURCE = Path(__file__).resolve().parents[2]


class Empty(AgentModel):
    pass


def skill_at(root, name='sample-skill', *, allowed='read_value', host='clear-workbench', required='', body='执行已注册工具读取真实结果。'):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'references').mkdir(exist_ok=True)
    (directory / 'assets').mkdir(exist_ok=True)
    (directory / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: 本地业务指导\n'
        'compatibility: Clear Workbench registered tools\nallowed-tools: ' + allowed + '\nmetadata:\n  host: ' + host +
        '\n  requires-tools: [' + required + ']\n---\n' + body, encoding='utf-8')
    (directory / 'references/rules.md').write_text('固定规则原始版本，0123456789。', encoding='utf-8')
    (directory / 'assets/template.md').write_text('# 输出模板\n只能填入实际结果。', encoding='utf-8')
    return directory


class SkillHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'registered-skills'
        self.source.mkdir()
        self.skill = skill_at(self.source)
        self.store = AgentStore(self.root / 'agent')
        self.session = self.store.create_session('wtest', 'Skills', [], 'skills-session')
        self.other = self.store.create_session('wother', 'Other', [], 'skills-other')
        self.registry = ToolRegistry()
        self.registry.register(ToolSpec('read_value', '只读实际对象', Empty,
            lambda args, ctx: AgentToolResult(status='success', summary='读取').model_dump()))
        skills.register(self.registry)
        self.service = SkillService(self.store, [self.source], registry=self.registry)

    def tearDown(self):
        self.temporary.cleanup()

    def activate(self, request='activate1', **kwargs):
        return self.service.activate('wtest', self.session['id'], 'sample-skill', request, **kwargs)

    async def test_server_registration_limits_local_f_roots_and_workspaces(self):
        config = self.root / 'config'
        config.mkdir()
        manifest = config / 'skills-registry.json'
        value = {'format': 'clear-skill-roots-v1', 'platform_roots': ['../registered-skills'],
                 'workspace_roots': {'wtest': [str(self.source)]}}
        manifest.write_text(json.dumps(value), encoding='utf-8')
        configured = registration(config)
        self.assertEqual(configured['roots'], [self.source])
        self.assertEqual(configured['workspace_roots']['wtest'], [self.source])
        value['platform_roots'] = ['C:/Users/A/.codex/skills']
        manifest.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(ValueError):
            registration(config)
        value['platform_roots'] = ['../registered-skills']
        value['format'] = 'automatic-asset-registration'
        manifest.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(ValueError):
            registration(config)

    async def test_registry_validates_metadata_compatibility_and_lightweight_pagination(self):
        skill_at(self.source, 'another-skill', host='different-host')
        skill_at(self.source, 'missing-tool', required='missing_registered_tool')
        bad = skill_at(self.source, 'bad-name')
        (bad / 'SKILL.md').write_text('---\nname: WRONG\ndescription: Bad\n---\nbody', encoding='utf-8')
        page = self.service.catalog('wtest', self.session['id'], limit=1)
        self.assertEqual(page['total'], 3)
        self.assertEqual(page['next_offset'], 1)
        self.assertEqual(page['items'][0]['name'], 'another-skill')
        self.assertFalse(page['items'][0]['compatible'])
        self.assertEqual(len(page['rejected']), 1)
        prompt = self.service.catalog_prompt('wtest', self.session['id'])
        self.assertNotIn('固定规则', json.dumps(prompt, ensure_ascii=False))
        self.assertNotIn('body', prompt['items'][0])
        self.assertNotIn('resources', prompt['items'][0])
        filtered = self.service.catalog('wtest', keyword='sample', limit=2)
        self.assertEqual(filtered['total'], 1)
        with self.assertRaises(ValueError):
            self.service.activate('wtest', self.session['id'], 'another-skill', 'badhost')
        with self.assertRaises(ValueError):
            self.service.activate('wtest', self.session['id'], 'missing-tool', 'missingtool')

    async def test_registered_f_roots_scope_and_resource_escape_do_not_read_assets(self):
        with self.assertRaises(ValueError):
            SkillService(self.store, [Path('C:/Users/A/.codex/skills')])
        with self.assertRaises(ValueError):
            self.service.catalog('wtest', self.other['id'])
        with self.assertRaises(ValueError):
            self.service.read_resource('wtest', self.session['id'], 'sample-skill')
        self.activate()
        for path in ('../secret.txt', 'references/../../secret', 'C:/secret', 'references\\rules.md', '/SKILL.md'):
            with self.assertRaises(ValueError):
                self.service.read_resource('wtest', self.session['id'], 'sample-skill', path)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'outside.txt').write_text('Outside source', encoding='utf-8')
        import subprocess
        junction = self.skill / 'linked-resources'
        outcome = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)],
                                 capture_output=True, text=True)
        if outcome.returncode == 0:
            page = self.service.catalog('wtest')
            self.assertEqual(page['items'], [])
            self.assertTrue(page['rejected'])
            # Remove only the exact junction, never its target directory.
            junction.rmdir()
            self.assertTrue((outside / 'outside.txt').is_file())

    async def test_fixed_all_file_versions_survive_source_mutation_removal_and_cold_restore(self):
        original = self.activate()
        version = original['skill']['version']
        self.assertEqual(len(original['skill']['resources']), 3)
        self.assertNotIn('固定规则', self.service.active_prompt(self.session['id']))
        first = self.service.read_resource('wtest', self.session['id'], 'sample-skill', 'references/rules.md', 0, 6, 'read1')
        self.assertEqual(first['content'], '固定规则原始')
        (self.skill / 'references/rules.md').write_text('新的资源不能替代原快照', encoding='utf-8')
        catalog = self.service.catalog('wtest', self.session['id'])
        self.assertTrue(catalog['items'][0]['source_changed'])
        self.assertNotEqual(catalog['items'][0]['version'], version)
        # Plain reactivation preserves old version; update requires seen version.
        self.assertEqual(self.activate('activate2')['skill']['version'], version)
        shutil.rmtree(self.skill)
        cold = SkillService(AgentStore(self.store.root), [self.source], registry=self.registry)
        restored = cold.read_resource('wtest', self.session['id'], 'sample-skill', 'references/rules.md', 6, 4000, 'read2')
        self.assertEqual(first['content'] + restored['content'], '固定规则原始版本，0123456789。')
        self.assertTrue(restored['source_changed'])
        manifest = cold.active_manifest(self.session['id'])[0]
        self.assertEqual(manifest['version'], version)
        self.assertEqual({item['request_id'] for item in manifest['read_resources']}, {'read1', 'read2'})
        self.assertIn('执行已注册工具', cold.active_prompt(self.session['id']))

    async def test_idempotent_activation_explicit_upgrade_and_cross_session_requests(self):
        receipt = self.activate()
        self.assertEqual(self.activate(), receipt)
        with self.assertRaises(Conflict):
            self.service.activate('wother', self.other['id'], 'sample-skill', 'activate1')
        (self.skill / 'references/rules.md').write_text('升级规则', encoding='utf-8')
        new = self.service.catalog('wtest')['items'][0]['version']
        with self.assertRaises(Conflict):
            self.activate('stale-upgrade', version='0' * 64)
        upgraded = self.activate('upgrade', version=new)
        self.assertTrue(upgraded['changed'])
        self.assertEqual(upgraded['skill']['version'], new)
        self.assertEqual(self.service.read_resource('wtest', self.session['id'], 'sample-skill', 'references/rules.md')['content'], '升级规则')
        stopped = self.service.deactivate('wtest', self.session['id'], 'sample-skill', 'stop1')
        self.assertEqual(stopped['active'], [])
        self.assertEqual(self.activate(), receipt)
        self.assertEqual(self.service.active_manifest(self.session['id']), [])

    async def test_tool_scope_fencing_permissions_and_scripts_are_not_shell(self):
        scripts = self.skill / 'scripts'
        scripts.mkdir()
        (scripts / 'unsafe.ps1').write_text('Write-Output secret', encoding='utf-8')
        run, _ = self.store.start_run(self.session['id'], 'Goal', 'skills-run', 1)
        services = SimpleNamespace(workspace_store=SimpleNamespace(get_workspace=lambda _: object()),
                                   agent=SimpleNamespace(skills=self.service))
        ctx = ToolContext('wtest', self.session['id'], run['id'], 1, 0, '', (), services, self.store)
        with self.assertRaises(Conflict):
            self.activate()
        call = {'call_id': 'activate', 'tool': 'skills_activate', 'arguments': {'name': 'sample-skill'}}
        denied = await self.registry.dispatch(call, replace(ctx, grants=frozenset({'read'})))
        self.assertEqual(denied['error']['code'], 'scope_denied')
        result = await self.registry.dispatch(call, ctx)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(await self.registry.dispatch(call, ctx), result)
        page = await self.registry.dispatch({'call_id': 'read-script', 'tool': 'skills_read_resource',
            'arguments': {'name': 'sample-skill', 'path': 'scripts/unsafe.ps1'}}, ctx)
        self.assertEqual(page['status'], 'unsupported')
        self.assertNotIn('content', page['data'])
        with self.assertRaises(RunSuperseded):
            self.service.deactivate('wtest', self.session['id'], 'sample-skill', 'obsolete', context=replace(ctx, generation=999))
        self.assertEqual(self.service.allowed_tools(self.session['id'], {'read_value', 'unregistered', 'skills_deactivate'},
            core={'skills_deactivate'}), {'read_value', 'skills_deactivate'})
        self.assertNotIn('unregistered', self.service.allowed_tools(self.session['id'], {'unregistered'}))

    async def test_skill_routes_loopback_scope_and_busy_user_mutations(self):
        app = FastAPI()
        app.include_router(router)
        app.state.agent = SimpleNamespace(store=self.store, skills=self.service)
        app.state.workspace_store = SimpleNamespace(get_workspace=lambda name: object() if name in {'wtest', 'wother'} else None)
        app.state.asset_library = (object(), object(), None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:9908') as client:
            prefix = '/api/platform/v1'
            page = await client.get(prefix + '/agent/skills', params={'workspace_id': 'wtest', 'session_id': self.session['id']})
            self.assertEqual(page.status_code, 200)
            wrong = await client.get(prefix + '/agent/skills', params={'workspace_id': 'wtest', 'session_id': self.other['id']})
            self.assertEqual(wrong.status_code, 403)
            path = prefix + '/agent/sessions/' + self.session['id'] + '/skills/activate'
            payload = {'name': 'sample-skill', 'request_id': 'user1'}
            self.assertEqual((await client.post(path, json=payload)).status_code, 403)
            self.assertEqual((await client.post(path, json=payload, headers={'origin': 'http://127.0.0.1:9908'})).status_code, 200)
            read = await client.get(prefix + '/agent/sessions/' + self.session['id'] + '/skills/sample-skill/resources')
            self.assertEqual(read.status_code, 200)
            self.assertIn('name: sample-skill', read.json()['content'])
            self.store.start_run(self.session['id'], 'Goal', 'busy-run', 1)
            stop = await client.post(prefix + '/agent/sessions/' + self.session['id'] + '/skills/deactivate',
                json={'name': 'sample-skill', 'request_id': 'user2'}, headers={'origin': 'http://127.0.0.1:9908'})
            self.assertEqual(stop.status_code, 409)


class SkillBusinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_coverage_completion as coverage
        coverage.CoverageTests.setUp(self)
        self.skills = self.runtime.skills

    def tearDown(self):
        import test_coverage_completion as coverage
        coverage.CoverageTests.tearDown(self)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60

    def serve(self, app):
        import test_coverage_completion as coverage
        return coverage.CoverageTests.serve(self, app)

    def link(self, family, project_id):
        import test_coverage_completion as coverage
        return coverage.CoverageTests.link(self, family, project_id)

    def jobs(self, urls, instances):
        import test_coverage_completion as coverage
        return coverage.CoverageTests.jobs(self, urls, instances)

    async def call(self, name, args, **kwargs):
        import test_coverage_completion as coverage
        allowed = self.skills.allowed_tools(self.ctx.session_id, self.registry.tools,
            core={'skills_list', 'skills_activate', 'skills_read_resource', 'skills_deactivate',
                  'tools_list', 'tools_describe', 'tools_load', 'workspace_get_context',
                  'audit_get_task', 'audit_get_project', 'models_list', 'ask_user', 'conversation_answer'})
        self.assertIn(name, allowed, 'Business continuation must obey active skill allowed-tools')
        result = await coverage.CoverageTests.call(self, name, args, **kwargs)
        self.assertIn(result['status'], {'success', 'pending'}, result)
        return result

    def assert_success(self, value):
        self.assertEqual(value['status'], 'success', value)
        return value['data']

    async def use_skill(self, name):
        result = await self.call('skills_activate', {'name': name})
        self.assertIn(name, self.skills.active_prompt(self.ctx.session_id))
        reference = next(item['path'] for item in result['data']['skill']['resources'] if item['kind'] == 'reference')
        resource = await self.call('skills_read_resource', {'name': name, 'path': reference})
        self.assertEqual(resource['data']['version'], result['data']['skill']['version'])

    def csv_asset(self, name, text):
        (self.root / name).write_text(text, encoding='utf-8-sig')
        self.assets.roots['skill-fixtures'] = self.root
        return self.assets.register('wtest', 'skill-fixtures', name, media_type='text/csv', storage_mode='managed')

    async def import_asset(self, session, side, asset):
        return await self.call('data_import', {'data_session_id': session,
            'expected_version': self.native.get(session)['version'], 'side': side, 'asset_id': asset.asset_id,
            'asset_revision': asset.revision, 'blob_hash': asset.blob_hash, 'options': {}})

    async def compare(self, left, right):
        session = self.assert_success(await self.call('data_create_session', {'name': 'Skill真实核对'}))['data_session_id']
        for side, asset in [('left', left), ('right', right)]:
            await self.call('data_inspect_input', {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
            await self.import_asset(session, side, asset)
            await self.call('data_apply_step', {'data_session_id': session, 'expected_version': self.native.get(session)['version'],
                'side': side, 'step': {'operator': 'cast', 'column': 'c2', 'target': 'decimal'}})
        compared = await self.call('data_compare', {'data_session_id': session, 'expected_version': self.native.get(session)['version'],
            'spec': {'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c2', 'c2']], 'amount_pairs': [['c2', 'c2']], 'amount_tolerance': '0'}})
        result = await self.call('data_read_result', {'data_session_id': session, 'result_id': compared['data']['result_id']})
        self.assertEqual(result['data']['rows'][0]['category'], 'different')
        return compared, result

    async def test_payment_skill_then_evidence_skill_generate_real_download_workpaper(self):
        await self.use_skill('payment-reconciliation')
        left = self.csv_asset('skill-left.csv', '编号,金额\n00001,10.00\n')
        right = self.csv_asset('skill-right.csv', '编号,金额\n00001,10.01\n')
        compared, result = await self.compare(left, right)
        await self.call('skills_deactivate', {'name': 'payment-reconciliation'})
        await self.use_skill('evidence-workpaper')
        bound = self.assert_success(await self.call('audit_bind_evidence', {'reference': result['result_refs'][0]}))
        finding = self.assert_success(await self.call('audit_write_finding', {'expected_revision': 0,
            'title': '真实金额差异', 'facts': '实际同号金额10.00与10.01不相同。',
            'interpretation': '原因待核实。', 'evidence_ids': [bound['evidence_id']]}))
        exported = await self.call('audit_generate_workpaper', {'draft_ids': [finding['draft_id']]})
        from workbench_platform.application_services.references import verify
        bundle = verify(exported['artifact_refs'][0], self.ctx)
        self.assertEqual({item['name'] for item in bundle['files']}, {'workpaper.md', 'workpaper.html', 'findings.json', 'evidence.json', 'sources.csv'})
        for item in bundle['files']:
            artifact = self.center.file('wtest', bundle['bundle_id'], item['name'])
            with self.center.store.stage_download('wtest', artifact.artifact_id, expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                self.assertGreater(path.stat().st_size, 0)

    async def test_invoice_skill_reads_real_native_ocr_table_then_runs_real_ledger_compare(self):
        import test_coverage_completion as coverage
        from PIL import Image
        from ocr_workbench.service import create_app
        from ocr_workbench.imaging import add_image
        await self.use_skill('invoice-ocr-reconciliation')
        with patch.dict('os.environ', {'WORKBENCH_APP_ID': 'ocr', 'WORKBENCH_INSTANCE_ID': 'instanceskillocr', 'WORKBENCH_NONCE': 'b' * 48}):
            app = create_app(coverage.OCR, self.root / 'skill-native-ocr', 'coverage-token-' + 'a' * 40,
                             start_queue=False, review_only=True, agent_enabled=False)
        native = app.state.store
        project = native.project('Skill OCR')
        link = self.link('ocr', project['id'])
        self.jobs({'ocr': self.serve(app)}, {'ocr': 'instanceskillocr'})
        image_path = self.root / 'skill-invoice.png'
        Image.new('RGB', (90, 60), 'white').save(image_path)
        self.assets.roots['skill-images'] = self.root
        self.assets.register('wtest', 'skill-images', image_path.name, media_type='image/png', storage_mode='managed')
        image = add_image(native, project['id'], image_path.name, image_path)
        task = native.enqueue(project['id'], [image['active_version']], ['ppocr'])[0]
        native.claim()
        table = {'rows': 2, 'columns': 2, 'cells': [{'row': row, 'column': column, 'row_span': 1, 'column_span': 1, 'text': text}
            for row, column, text in [(0, 0, '编号'), (0, 1, '金额'), (1, 0, '00001'), (1, 1, '10.00')]]}
        # The fixture is explicit synthetic OCR output; native storage, HTTP,
        # fixed source table conversion and data-worker execution are real.
        native.complete(task, {'engine': 'ppocr', 'text': '00001 10.00', 'tables': [table], 'blocks': []})
        result_id = native.one('tasks', task)['result_id']
        attached = await self.call('native_attach_result', {'application': 'ocr', 'link_id': link.link_id, 'object_id': result_id})
        reference = attached['result_refs'][0]
        await self.call('results_describe', {'reference': reference})
        converted = self.assert_success(await self.call('results_to_input', {'reference': reference, 'table_index': 0}))
        derived = self.assets.get('wtest', converted['asset_id'])
        right = self.csv_asset('skill-ocr-ledger.csv', '编号,金额\n00001,10.01\n')
        _, compared = await self.compare(derived, right)
        self.assertEqual(compared['data']['rows'][0]['L_c1_value'], '00001')

    async def test_classification_skill_fixed_import_annotation_and_real_dataset_preparation(self):
        from PIL import Image
        from image_platform.api import create_app
        from image_platform.settings import Settings
        from workbench_platform.jobs.classification_handoff import ClassificationHandoff
        await self.use_skill('classification-training-preparation')
        app = create_app(Settings(self.root / 'skill-native-classification'))
        native = app.state.store
        project = native.create_project('Skill classification', '', [
            {'label_id': 'invoice', 'display_name': '票据'}, {'label_id': 'other', 'display_name': '其他'}])
        link = self.link('classification', project['id'])
        self.jobs({'classification': self.serve(app)}, {'classification': 'instanceskillclass'})
        self.services.classification_handoff = ClassificationHandoff(self.root / 'skill-classification-handoffs.sqlite3',
            self.services.job_service, self.assets, self.sets)
        self.assets.roots['skill-images'] = self.root
        assets = []
        for index in range(6):
            name = f'skill-training-{index}.png'
            Image.new('RGB', (32, 32), (index * 30, 10, 20)).save(self.root / name)
            assets.append(self.assets.register('wtest', 'skill-images', name, media_type='image/png', storage_mode='managed'))
        fixed = self.assert_success(await self.call('assets_create_set', {'asset_ids': [item.asset_id for item in assets]}))
        imported = self.assert_success(await self.call('classification_import_assets', {'link_id': link.link_id,
            'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision']}))
        labels = self.assert_success(await self.call('classification_read_annotations', {'handoff_id': imported['handoff_id']}))['items']
        await self.call('classification_annotate', {'handoff_id': imported['handoff_id'], 'items': [
            {'asset_id': item['asset_id'], 'expected_hash': item['expected_hash'], 'label_id': 'invoice' if index < 3 else 'other'}
            for index, item in enumerate(labels)]})
        dataset = self.assert_success(await self.call('classification_publish_dataset',
            {'handoff_id': imported['handoff_id'], 'name': 'Skill准备版本'}))
        manifest = native.one('SELECT manifest_path FROM datasets WHERE id=?', (dataset['id'],))
        self.assertTrue(Path(manifest['manifest_path']).is_file())
        self.assertTrue(Path(manifest['manifest_path']).with_suffix('.csv').is_file())
        self.assertEqual(len(native.rows('SELECT * FROM annotations')), 6)
        self.assertEqual(native.rows("SELECT * FROM jobs WHERE kind='train'"), [])


if __name__ == '__main__':
    unittest.main()

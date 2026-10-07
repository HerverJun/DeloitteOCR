"""Current coverage: real native HTTP/storage, real data workers, synthetic models."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_data_tools as base
import test_graph as graph_tests
from workbench_platform.agent.runtime import AgentRuntime
from workbench_platform.agent.context import core_tools
from workbench_platform.agent.store import digest
from workbench_platform.batches.service import BatchService
from workbench_platform.audit.service import AuditService
from workbench_platform.application_services.references import verify
from workbench_platform.application_services.manual import candidates
from workbench_platform.agent.tools import platform_management, classification_management, ocr_management

SOURCE = Path(__file__).resolve().parents[2]
CLASSIFICATION = SOURCE.parent / 'classification'
OCR = SOURCE.parent / 'ocr'
sys.path.extend([str(CLASSIFICATION/'src'), str(OCR/'src')])
PROGRAM = SOURCE.parent.parent/'program'
sys.path.extend([str(PROGRAM/'bundles/ocr/runtimes/control/Lib/site-packages'),
                 str(PROGRAM/'bundles/classification/runtimes/control/Lib/site-packages')])


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    asset = base.DataToolTests.asset
    create = base.DataToolTests.create
    import_asset = base.DataToolTests.import_asset

    async def wait_status(self, runtime, run_id, statuses):
        # A batch includes several real worker imports/casts/comparison/export
        # stages. Its bound exceeds the simple graph suite's single-job 20s,
        # especially when the native OCR/classifier suite runs concurrently.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            value = runtime.store.run(run_id)
            if value['status'] in statuses:
                return value
            await asyncio.sleep(.1)
        self.fail('Batch graph did not settle: ' + json.dumps({
            'run': runtime.store.run(run_id), 'events': runtime.store.events(value['session_id']),
            'batches': self.services.batch_service.store.list('wtest')}, ensure_ascii=False))

    def setUp(self):
        base.DataToolTests.setUp(self)
        self.root = Path(self.temporary.name)
        uploads = self.root/'uploads'; uploads.mkdir()
        self.assets.roots['uploads'] = uploads
        self.native.asset_registry = self.assets
        self.assets.roots['data'] = self.native.root
        self.runtime = AgentRuntime(self.store.root, self.services)
        self.services.agent = self.runtime
        self.registry = self.runtime.registry
        self.store = self.runtime.store
        self.ctx = replace(self.ctx, store=self.store)
        self.servers = []
        self.services.batch_service = BatchService(SimpleNamespace(state=self.services), self.root/'batches')
        self.services.audit_service = AuditService(SimpleNamespace(state=self.services), self.root/'audit')
        self.center.batch_store = self.services.batch_service.store
        self.center.audit_service = self.services.audit_service
        self.services.job_service = None
        self.services.launch_service = None
        self.services.supervisor = SimpleNamespace(registry=SimpleNamespace(installations={}))

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60

    def tearDown(self):
        for server, thread in self.servers:
            server.should_exit = True; thread.join(10)
        self.services.batch_service.close()
        base.DataToolTests.tearDown(self)

    def serve(self, app):
        import uvicorn
        sock = socket.socket(); sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        self.servers.append((server, thread))
        deadline = time.monotonic()+10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(server.started)
        return 'http://127.0.0.1:'+str(port)

    def link(self, family, project_id):
        workspace = self.workspaces.get_workspace('wtest')
        return self.workspaces.add_link('wtest', family, project_id, role='owner', expected_revision=workspace.revision)

    def jobs(self, urls, instances):
        from workbench_platform.jobs.routes import JobService
        from workbench_platform.jobs.projection import JobProjectionStore
        token = self.root/'ocr.token'; token.write_text('coverage-token-'+'a'*40, encoding='utf-8')
        launches = SimpleNamespace(current=lambda app: SimpleNamespace(instance_id=instances[app], base_url=urls[app]))
        self.services.job_service = JobService(JobProjectionStore(self.root/'jobs.sqlite3'), self.workspaces, launches, ocr_token_file=str(token))
        self.services.launch_service = launches
        self.services.supervisor.registry.installations = {family: SimpleNamespace(installed=True) for family in urls}

    async def call(self, name, args, *, settle=True, call_id=None):
        self.counter += 1
        call = {'call_id': call_id or 'coverage_'+str(self.counter), 'tool': name, 'arguments': args}
        self.registry.validate_batch([call], self.registry.tools)
        value = await self.registry.dispatch(call, self.ctx)
        if settle and value['status'] == 'pending':
            for _ in range(600):
                ref = value['job_refs'][0]
                from workbench_platform.application_services.job_lifecycle import adapter
                handler = adapter(ref).settle
                completed = await asyncio.to_thread(handler, ref, self.ctx)
                if completed is not None:
                    completed['job_refs'] = value['job_refs']
                    operation = self.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?', (self.ctx.run_id, call['call_id']))[0]
                    self.store.finish_operation(operation['id'], completed)
                    value = completed; break
                await asyncio.sleep(.05)
            else:
                self.fail('任务等待超时')
        return value

    def assert_success(self, value):
        self.assertEqual(value['status'], 'success', value)
        return value['data']

    def lose_reply(self, name):
        specification = self.registry.tools[name]
        def execute(arguments, context):
            specification.execute(arguments, context)
            raise OSError('synthetic response lost after native commit')
        return patch.dict(self.registry.tools, {name: replace(specification, execute=execute)})

    async def test_registered_batch_formal_audit_todos_and_real_worker_results(self):
        names = set(self.registry.tools)
        self.assertEqual(len(core_tools(self.registry)), 14)
        self.assertTrue({'classification_train', 'ocr_visual_review', 'native_attach_result', 'documents_read', 'results_concat_tables', 'batch_run', 'audit_list_todos'} <= names)
        for name in names:
            self.registry.tools[name].input_schema.model_json_schema()
        for name, text in [('coverage-left.csv', '编号,金额\n00001,10.00\nB,20.00\n'), ('coverage-right.csv', '编号,金额\n00001,10.00\nB,21.00\n')]:
            (base.RUN/'fixtures'/name).write_text(text, encoding='utf-8')
        left, right = self.asset('coverage-left.csv'), self.asset('coverage-right.csv')
        role = {'formats': ['csv'], 'columns': [{'header': '编号', 'kinds': ['text']}, {'header': '金额', 'kinds': ['text']}],
                'import_options': {}, 'steps': [{'operator': 'cast', 'parameters': {'column': '金额', 'target': 'decimal'}}]}
        template = self.assert_success(await self.call('batch_create_template', {'name': 'Agent对账', 'scenario': 'data_compare',
            'config': {'roles': {'left': role, 'right': role}, 'comparison': {'key_pairs': [['编号', '编号']], 'compare_pairs': [['金额', '金额']],
                'amount_pairs': [['金额', '金额']], 'amount_tolerance': '0', 'type_policy': 'strict', 'null_policy': 'never_match'}}}, call_id='template_once'))
        replay = self.assert_success(await self.call('batch_create_template', {'name': 'Agent对账', 'scenario': 'data_compare',
            'config': template['config']}, call_id='template_once'))
        self.assertEqual(template['template_id'], replay['template_id'])
        args = {'template_id': template['template_id'], 'template_version': 1, 'inputs': [{'role': side, 'asset_id': asset.asset_id,
            'asset_revision': asset.revision, 'blob_hash': asset.blob_hash} for side, asset in [('left', left), ('right', right)]]}
        self.assertTrue(self.assert_success(await self.call('batch_preflight', args))['ok'])
        with self.lose_reply('batch_run'):
            completed = await self.call('batch_run', args)
        value = self.assert_success(completed); batch = value['batch']
        self.assertEqual(batch['status'], 'succeeded', batch)
        issue = next(item for item in value['issues']['items'] if item['category'] == 'different')
        created = self.assert_success(await self.call('audit_formal_create_project', {'project': {'title': '审计项目', 'audit_object': '供应商',
            'period_start': '2026-01-01', 'period_end': '2026-12-31', 'objective': '核对金额'}}))
        project_id = created['project_id']
        procedure = self.assert_success(await self.call('audit_formal_create_procedure', {'project_id': project_id, 'expected_revision': 1,
            'procedure': {'title': '台账核对', 'basis': '双方资料', 'scope': '两表'}, 'batches': [{'batch_id': batch['batch_id'], 'batch_revision': batch['revision']}]}))
        procedure_id = procedure['entity_id']
        finding = self.assert_success(await self.call('audit_formal_create_finding', {'project_id': project_id, 'expected_revision': 2,
            'procedure_id': procedure_id, 'finding': {'title': '金额差异', 'explanation': '待核实'},
            'evidence': [{'batch_id': batch['batch_id'], 'batch_revision': batch['revision'], 'item_id': issue['item_id']}]}))
        todos = self.assert_success(await self.call('audit_list_todos', {}))['items']
        self.assertTrue(todos)
        located = self.assert_success(await self.call('audit_locate_todo', {'todo_id': todos[0]['todo_id']}))
        self.assertEqual(located['item']['todo_id'], todos[0]['todo_id'])
        history = self.assert_success(await self.call('audit_formal_read_history', {'project_id': project_id, 'kind': 'finding', 'entity_id': finding['entity_id']}))
        self.assertTrue(history['items']); self.assertEqual(history['items'][0]['actor']['display_name'], 'AI审计Agent')
        self.assertEqual(self.assert_success(await self.call('batch_list', {}))['items'][0]['batch_id'], batch['batch_id'])
        with self.lose_reply('batch_dispose'):
            disposition = self.assert_success(await self.call('batch_dispose', {'batch_id': batch['batch_id'], 'expected_revision': batch['revision'],
                'item_ids': [issue['item_id']], 'action': 'defer', 'note': '等待资料'}))
        self.assertTrue(self.assert_success(await self.call('batch_read_history', {'batch_id': batch['batch_id']}))['items'])
        latest = self.services.batch_service.get(batch['batch_id'])
        with self.lose_reply('batch_export'):
            exported = await self.call('batch_export', {'batch_id': batch['batch_id'], 'expected_revision': latest['revision'], 'kind': 'intermediate'}, call_id='batch_export_once')
        self.assert_success(exported); self.assertTrue(verify(exported['artifact_refs'][0], self.ctx)['files'])
        self.assertEqual(exported, await self.call('batch_export', {'batch_id': batch['batch_id'], 'expected_revision': latest['revision'], 'kind': 'intermediate'}, call_id='batch_export_once'))
        from workbench_platform.application_services.references import reference_location
        self.assertIn('/batches?', reference_location(completed['job_refs'][0], self.ctx.session_id))
        denied = await self.registry.dispatch({'call_id': 'out_of_scope', 'tool': 'batch_read', 'arguments': {'batch_id': batch['batch_id']}}, replace(self.ctx, selected_assets=(left.asset_id,)))
        self.assertEqual(denied['error']['code'], 'invalid_input')
        read = self.assert_success(await self.call('audit_formal_read', {'project_id': project_id, 'kind': 'finding', 'entity_id': finding['entity_id']}))
        self.assertFalse(read['records']); self.assertEqual(read['finding']['status'], 'draft')
        # Run the new family through the real graph observer, then continue
        # choosing a tool after the batch worker has finished.
        decisions = []
        async def provider(state, schemas):
            decisions.append(state['step'])
            if state['step'] == 0:
                return graph_tests.response(state, 'batch_run', args)
            if state['step'] == 1:
                previous = state['messages'][-1]['result']
                self.assertEqual(previous['data']['batch']['status'], 'succeeded')
                # Capture the actual synthetic business response for the wire
                # budget regression, rather than constructing a mirror of the
                # summary algorithm. This contains no controller credential.
                (base.RUN/'evidence'/'batch-context-result.json').write_text(
                    json.dumps(previous, ensure_ascii=False), encoding='utf-8')
                return graph_tests.response(state, 'batch_read', {'batch_id': previous['data']['batch']['batch_id']})
            return graph_tests.response(state, content='已读取批次实际结果')
        self.runtime.model_provider = provider
        # This existing observer/recovery scenario starts with a known batch
        # template. Preload only its two business contracts; normal core-only
        # discovery is covered by the production wire-context checks.
        initial_state = self.runtime.initial_state
        def batch_state(run, content):
            state = initial_state(run, content)
            state['visible'] = list(dict.fromkeys([*state['visible'], 'batch_run', 'batch_read']))
            return state
        self.runtime.initial_state = batch_state
        self.runtime.cancel(self.ctx.run_id)
        await self.runtime.start()
        try:
            session = self.store.create_session('wtest', '批次动态图', [], 'batch_graph_session')
            run = await self.runtime.submit(session['id'], '按现有模板执行并读取', 'batch_graph_message')
            final = await self.wait_status(self.runtime, run['id'], {'incomplete', 'failed', 'succeeded'})
            self.assertEqual(final['status'], 'incomplete', self.store.events(session['id']))
            self.assertEqual(decisions, [0, 1, 2])
            requests = [event['payload'] for event in self.store.events(session['id'])['items']
                        if event['kind'] == 'model_request']
            self.assertEqual(len(requests), 3)
            for request in requests:
                self.assertLessEqual(request['context_metrics']['input_tokens_estimate'], 14336)
            self.assertTrue(requests[-1]['context_metrics']['compressed'])
            (base.RUN/'evidence'/'batch-context-budget.json').write_text(json.dumps({
                'core_count': len(core_tools(self.registry)), 'requests': requests,
                'decisions': decisions, 'final_status': final['status'],
                'real_data_workers': True, 'real_external_controller': False}, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            await self.runtime.close()

    async def test_native_ocr_attach_literal_edit_export_evidence_and_table_concat(self):
        from PIL import Image
        from ocr_workbench.service import create_app
        from ocr_workbench.imaging import add_image
        with patch.dict('os.environ', {'WORKBENCH_APP_ID': 'ocr', 'WORKBENCH_INSTANCE_ID': 'instanceocr', 'WORKBENCH_NONCE': 'b'*48}), \
             patch('ocr_workbench.multimodal_runtime.load_config', return_value={'profile_id': 'coverage-visual'}), \
             patch('ocr_workbench.multimodal_runtime.review_readiness', return_value={'ready': True, 'profiles': ['coverage-visual'], 'label': '合成视觉测试'}):
            app = create_app(OCR, self.root/'native-ocr', 'coverage-token-'+'a'*40, start_queue=False, review_only=True, agent_enabled=False)
        store = app.state.store; project = store.project('原生独立导入')
        url = self.serve(app); link = self.link('ocr', project['id']); self.jobs({'ocr': url}, {'ocr': 'instanceocr'})
        source = self.root/'source.png'; Image.new('RGB', (90, 60), 'white').save(source)
        self.assets.roots['synthetic'] = self.root
        asset = self.assets.register('wtest', 'synthetic', 'source.png', media_type='image/png', storage_mode='managed')
        image = add_image(store, project['id'], 'source.png', source)
        task = store.enqueue(project['id'], [image['active_version']], ['ppocr'])[0]; store.claim()
        table = {'rows': 2, 'columns': 2, 'cells': [{'row': r, 'column': c, 'row_span': 1, 'column_span': 1, 'text': text}
            for r, c, text in [(0,0,'编号'), (0,1,'金额'), (1,0,'00001'), (1,1,'10.00')]]}
        store.complete(task, {'engine': 'ppocr', 'text': '金额 10.00', 'tables': [table], 'blocks': []})
        result_id = store.one('tasks', task)['result_id']
        attach = await self.call('native_attach_result', {'application': 'ocr', 'link_id': link.link_id, 'object_id': result_id})
        self.assert_success(attach); ref = attach['result_refs'][0]
        self.assertEqual(verify(ref, self.ctx)['source_asset']['asset_id'], asset.asset_id)
        read = self.assert_success(await self.call('documents_read', {'reference': ref, 'offset': 3, 'limit': 2}))
        self.assertEqual(read['text'], '10')
        with self.lose_reply('ocr_edit_result'):
            edited = await self.call('ocr_edit_result', {'reference': ref, 'changes': [{'kind': 'cell', 'table_index': 0, 'row': 1, 'column': 1, 'before': '10.00', 'after': '10.01'}]}, call_id='edit_once')
        self.assert_success(edited); new = edited['result_refs'][0]
        self.assertEqual(new['revision'], 1)
        self.assertEqual(store.result(result_id)['edited']['tables'][0]['cells'][3]['text'], '10.01')
        replay = await self.call('ocr_edit_result', {'reference': ref, 'changes': [{'kind': 'cell', 'table_index': 0, 'row': 1, 'column': 1, 'before': '10.00', 'after': '10.01'}]}, call_id='edit_once')
        self.assertEqual(replay, edited); self.assertEqual(store.result(result_id)['revision'], 1)
        combined = await self.call('results_concat_tables', {'tables': [{'reference': new, 'table_index': 0}, {'reference': new, 'table_index': 0}]})
        combined_asset = self.assert_success(combined)
        with self.assets.stage_verified('wtest', combined_asset['asset_id'], revision=combined_asset['asset_revision'], blob_hash=combined_asset['blob_hash']) as path:
            self.assertEqual(path.read_text(encoding='utf-8-sig').count('00001,10.01'), 2)
        evidence = self.assert_success(await self.call('audit_bind_evidence', {'reference': new}))
        self.assertEqual(evidence['reference']['object_id'], result_id)
        with self.lose_reply('native_export_result'):
            exported = await self.call('native_export_result', {'binding_id': attach['data']['binding_id']})
        bundle = self.assert_success(exported)['bundle']
        self.assertTrue(verify(exported['artifact_refs'][0], self.ctx)['files'])
        self.assertTrue(any(item['reference']['object_id'] == result_id for item in candidates(self.ctx)['items']))
        self.assertTrue(self.assert_success(await self.call('ocr_list_visual_models', {'link_id': link.link_id}))['models'] is not None)
        missing = await self.call('ocr_visual_review', {'reference': new, 'model_id': 'missing-visual'}, settle=False)
        self.assertEqual(missing['error']['code'], 'invalid_input')
        self.assertEqual(store.rows('SELECT count(*) n FROM multimodal_requests')[0]['n'], 0)
        # Explicit synthetic model response; real native submission, immutable
        # snapshot, queue record and result observer, with no network model.
        from ocr_workbench.multimodal_store import prepare_review, complete_review
        visual_config = {'model': {'id': 'coverage-visual', 'sha256': 'fixture-only'}}
        app.state.application_services.visual_parameters = lambda model: (visual_config, SimpleNamespace(wake=threading.Event()))
        def catalog_ready(bundle, config):
            return {'ready': True, 'profiles': ['coverage-visual'], 'label': '合成视觉测试'}
        app.state.review_only = False
        with patch('ocr_workbench.multimodal_runtime.load_config', return_value={'profile_id': 'coverage-visual'}), \
             patch('ocr_workbench.multimodal_runtime.review_readiness', side_effect=catalog_ready):
            with self.lose_reply('ocr_visual_review'):
                visual = await self.call('ocr_visual_review', {'reference': new, 'model_id': 'coverage-visual'}, settle=False, call_id='visual_once')
        self.assertEqual(visual['status'], 'pending', visual)
        self.assertIsNone(ocr_management.settle(visual['job_refs'][0], self.ctx))
        visual_task = store.claim()['id']; snapshot = prepare_review(store, visual_task)
        response = {'items': [{'target_id': item['id'], 'decision': 'keep', 'after': item['before'], 'reason': '合成响应，仅检查调用协议'}
                              for item in snapshot['targets']], 'summary': 'fixture-only'}
        self.assertTrue(complete_review(store, visual_task, response))
        finished = ocr_management.settle(visual['job_refs'][0], self.ctx)
        self.assert_success(finished); self.assertTrue(finished['data']['proposals'])
        self.assertEqual(store.result(result_id)['revision'], 1)
        self.assertEqual(store.rows('SELECT count(*) n FROM multimodal_requests')[0]['n'], 1)
        # Lost response recovery must return the operation's fixed revision,
        # even after a later manual change.
        operation_id = self.store.rows("SELECT id FROM operations WHERE call_id='edit_once'")[0]['id']
        later_tables = store.result(result_id)['edited']['tables']
        later_tables[0]['cells'][1]['text'] = '其他表头'
        store.save(result_id, {'text': '人工后续版本', 'tables': later_tables}, 1)
        recovered = ocr_management.edit_lookup(ocr_management.Edit.model_validate({'reference': ref, 'changes': [{'kind': 'cell', 'table_index': 0, 'row': 1, 'column': 1, 'before': '10.00', 'after': '10.01'}]}), replace(self.ctx, operation_id=operation_id))
        self.assertEqual(recovered['result_refs'][0]['revision'], 1)
        later = await self.call('native_read_result', {'binding_id': attach['data']['binding_id']})
        rejected = await self.call('results_concat_tables', {'tables': [{'reference': new, 'table_index': 0}, {'reference': later['result_refs'][0], 'table_index': 0}]})
        self.assertEqual(rejected['status'], 'error')

    async def test_classification_annotation_dataset_replay_scoped_training_protocol(self):
        from PIL import Image
        from image_platform.api import create_app
        from image_platform.settings import Settings
        from image_platform.datasets import register_asset
        from image_platform.store import dump, now
        from workbench_platform.jobs.classification_handoff import ClassificationHandoff
        from workbench_platform.jobs.ocr_handoff import OcrHandoff
        app = create_app(Settings(self.root/'native-classification'))
        native = app.state.store
        project = native.create_project('分类', '', [{'label_id': 'invoice', 'display_name': '票据'}, {'label_id': 'other', 'display_name': '其他'}])
        url = self.serve(app); link = self.link('classification', project['id']); self.jobs({'classification': url}, {'classification': 'instanceclass'})
        self.services.classification_handoff = ClassificationHandoff(self.root/'classification-handoffs.sqlite3', self.services.job_service, self.assets, self.sets)
        self.assets.roots['synthetic'] = self.root
        assets = []
        for index in range(6):
            name = 'training-'+str(index)+'.png'; Image.new('RGB', (32,32), (index*30, 10, 20)).save(self.root/name)
            assets.append(self.assets.register('wtest', 'synthetic', name, media_type='image/png', storage_mode='managed'))
        fixed = self.assert_success(await self.call('assets_create_set', {'asset_ids': [asset.asset_id for asset in assets]}))
        imported = self.assert_success(await self.call('classification_import_assets', {'link_id': link.link_id, 'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision']}))
        handoff_id = imported['handoff_id']
        labels = self.assert_success(await self.call('classification_read_annotations', {'handoff_id': handoff_id}))['items']
        args = {'handoff_id': handoff_id, 'items': [{'asset_id': item['asset_id'], 'expected_hash': item['expected_hash'], 'label_id': 'invoice' if index < 3 else 'other'} for index, item in enumerate(labels)]}
        with self.lose_reply('classification_annotate'):
            annotated = await self.call('classification_annotate', args, call_id='label_once')
        self.assert_success(annotated)
        self.assertEqual((await self.call('classification_annotate', args, call_id='label_once')), annotated)
        self.assertTrue(all(row['origin'].startswith('agent:') for row in native.rows('SELECT * FROM annotations')))
        with self.lose_reply('classification_publish_dataset'):
            dataset = self.assert_success(await self.call('classification_publish_dataset', {'handoff_id': handoff_id, 'name': '固定训练版本'}, call_id='dataset_once'))
        with self.lose_reply('classification_train'):
            submitted = await self.call('classification_train', {'handoff_id': handoff_id, 'dataset_id': dataset['id'], 'epochs': 1}, settle=False, call_id='train_once')
        self.assertEqual(submitted['status'], 'pending', submitted)
        repeated = await self.call('classification_train', {'handoff_id': handoff_id, 'dataset_id': dataset['id'], 'epochs': 1}, settle=False, call_id='train_once')
        self.assertEqual(submitted, repeated)
        self.assertEqual(len(native.rows("SELECT * FROM jobs WHERE kind='train'")), 1)
        # Only completion protocol is simulated: no training or weights loaded.
        job_id = submitted['data']['job']['native_job_ids'][0]
        with native.connect(write=True) as db:
            db.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (job_id,))
            db.execute('INSERT INTO models VALUES(?,?,?,?,?,?,?,?)', ('model_coverage', project['id'], job_id, '合成模型元数据', str(self.root/'synthetic-model'), dump({'model_version_id':'model_coverage'}), dump({'fixture_only':True}), now()))
        completed = classification_management.settle(submitted['job_refs'][0], self.ctx)
        self.assertEqual(completed['data']['models'][0]['id'], 'model_coverage')
        # Exercise compare-and-set and durable replay; package validation is
        # explicitly replaced because this test produced no model weights.
        activation = {'link_id': link.link_id, 'model_id': 'model_coverage', 'expected_active_model_id': None}
        with patch('image_platform.api.validate_package', return_value={}):
            chosen = await self.call('classification_activate_model', activation, call_id='model_once')
        self.assert_success(chosen)
        conflicted = await self.call('classification_activate_model', activation)
        self.assertEqual(conflicted['error']['code'], 'revision_conflict')
        self.assertEqual(chosen, await self.call('classification_activate_model', activation, call_id='model_once'))
        manifest = native.one('SELECT manifest_path FROM datasets WHERE id=?', (dataset['id'],))
        self.assertTrue(Path(manifest['manifest_path']).with_suffix('.csv').is_file())
        denied = await self.registry.dispatch({'call_id': 'deniedtrain', 'tool': 'classification_train', 'arguments': {'handoff_id': handoff_id, 'dataset_id': dataset['id'], 'epochs': 1}}, replace(self.ctx, selected_assets=(assets[0].asset_id,)))
        self.assertEqual(denied['error']['code'], 'invalid_input')

    async def test_source_text_and_pdf_pages_are_literal_and_scoped(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        self.assets.roots['synthetic'] = self.root
        (self.root/'source.txt').write_text('编号00001\n金额10.00\n尾行', encoding='utf-8')
        asset = self.assets.register('wtest', 'synthetic', 'source.txt', media_type='text/plain', storage_mode='managed')
        ref = {'kind': 'asset', 'workspace_id': 'wtest', 'object_id': asset.asset_id, 'revision': asset.revision, 'sha256': asset.blob_hash}
        first = await self.call('documents_read', {'reference': ref, 'offset': 2, 'limit': 5})
        self.assertEqual(self.assert_success(first)['text'], '00001'); self.assertEqual(first['next_offset'], 7)
        denied = await self.registry.dispatch({'call_id': 'scope_text', 'tool': 'documents_read', 'arguments': {'reference': ref}}, replace(self.ctx, selected_assets=('not_this_asset',)))
        self.assertEqual(denied['status'], 'error')
        writer = PdfWriter()
        for text in ('Page one 00001', 'Page two 10.00'):
            page = writer.add_blank_page(300, 200)
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject(); stream.set_data(('BT /F1 12 Tf 20 100 Td ('+text+') Tj ET').encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(self.root/'source.pdf')
        pdf = self.assets.register('wtest', 'synthetic', 'source.pdf', media_type='application/pdf', storage_mode='managed')
        pdfref = {'kind': 'asset', 'workspace_id': 'wtest', 'object_id': pdf.asset_id, 'revision': pdf.revision, 'sha256': pdf.blob_hash}
        self.assertEqual((await self.call('documents_read', {'reference': pdfref}))['status'], 'error')
        value = self.assert_success(await self.call('documents_read', {'reference': pdfref, 'page_number': 2, 'offset': 9, 'limit': 5}))
        self.assertEqual(value['text'], '10.00')
        # A result limit must resume within a file and then reach later files.
        from workbench_platform.application_services.search import search
        from workbench_platform.agent.tools.audit import Search
        (self.root/'matches.txt').write_text('needle first\nneedle second\nneedle third', encoding='utf-8')
        matching = self.assets.register('wtest', 'synthetic', 'matches.txt', media_type='text/plain', storage_mode='managed')
        (self.root/'later.txt').write_text('needle fourth', encoding='utf-8')
        later = self.assets.register('wtest', 'synthetic', 'later.txt', media_type='text/plain', storage_mode='managed')
        ctx = replace(self.ctx, selected_assets=(matching.asset_id, later.asset_id))
        seen, offset, within, ocr_offset = [], 0, 0, 0
        for _ in range(6):
            page = search(Search(query='needle', limit=1, asset_offset=offset, asset_match_offset=within, ocr_offset=ocr_offset), ctx)
            seen.extend(item['text'] for item in page['items'])
            coverage = page['coverage']
            if coverage['next_asset_offset'] < 0 and coverage['next_ocr_offset'] < 0:
                break
            offset = coverage['next_asset_offset'] if coverage['next_asset_offset'] >= 0 else coverage['asset_page_total']
            within = coverage['next_asset_match_offset']
            ocr_offset = max(0, coverage['next_ocr_offset'])
        self.assertEqual(seen, ['needle first', 'needle second', 'needle third', 'needle fourth'])
        # Initial refs share the OCR cursor with discovered refs, without duplicates.
        refs = [{'kind': 'result', 'application': 'ocr', 'parent_id': 'fixture', 'object_id': str(i)} for i in range(3)]
        task = {'initial_refs': refs, 'result_refs': refs}
        def snapshot(ref, _):
            return {'snapshot': {'edited': {'text': 'needle OCR '+ref['object_id']}},
                    'source_asset': {'asset_id': matching.asset_id}, 'locator': {}}
        with patch.object(self.runtime.tasks, 'get', return_value=task), \
             patch('workbench_platform.application_services.manual.result_references', return_value=iter(())), \
             patch('workbench_platform.application_services.search.verify', side_effect=snapshot):
            seen, ocr_offset = [], 0
            for _ in range(3):
                page = search(Search(query='needle', limit=1, asset_offset=2, ocr_offset=ocr_offset), ctx)
                seen.extend(item['text'] for item in page['items'])
                ocr_offset = page['coverage']['next_ocr_offset']
            self.assertEqual(seen, ['needle OCR 2', 'needle OCR 1', 'needle OCR 0'])
            self.assertEqual(ocr_offset, -1)

    async def test_native_classification_import_scope_disposition_and_export(self):
        from PIL import Image
        from image_platform.api import create_app
        from image_platform.settings import Settings
        from image_platform.datasets import register_asset
        from image_platform.store import dump
        with patch.dict('os.environ', {'WORKBENCH_APP_ID': 'classification', 'WORKBENCH_INSTANCE_ID': 'instanceclass', 'WORKBENCH_NONCE': 'c'*48}):
            app = create_app(Settings(self.root/'native-classification'))
        native = app.state.store
        project = native.create_project('原生分类', '', [{'label_id': 'invoice', 'display_name': '票据'}, {'label_id': 'other', 'display_name': '其他'}])
        url = self.serve(app); link = self.link('classification', project['id']); self.jobs({'classification': url}, {'classification': 'instanceclass'})
        self.assets.roots['synthetic'] = self.root
        Image.new('RGB', (32,32), 'white').save(self.root/'selected.png')
        selected = self.assets.register('wtest', 'synthetic', 'selected.png', media_type='image/png', storage_mode='managed')
        Image.new('RGB', (32,32), 'black').save(self.root/'imported.png')
        source, _ = register_asset(native, project['id'], self.root/'imported.png')
        job_id = native.submit(project['id'], 'inference', {'model_id': 'fixture_model'}, [source])
        with native.connect(write=True) as db:
            db.execute("UPDATE jobs SET state='succeeded',done=1 WHERE id=?", (job_id,))
            db.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?", (dump({'label_id': 'invoice', 'status': 'accepted', 'score': .9}), job_id))
        oldctx = self.ctx; self.ctx = replace(self.ctx, selected_assets=(selected.asset_id,))
        attached = await self.call('native_attach_result', {'application': 'classification', 'link_id': link.link_id, 'object_id': job_id})
        self.assertEqual(attached['status'], 'success'); required = attached['data']['required_assets']
        self.assertEqual(len(required), 1); self.assertEqual(self.ctx.selected_assets, (selected.asset_id,))
        denied = await self.call('native_read_result', {'binding_id': attached['data']['binding_id']})
        self.assertEqual(denied['status'], 'error')
        self.ctx = replace(self.ctx, selected_assets=(selected.asset_id, required[0]))
        read = await self.call('classification_read_results', {'handoff_id': attached['data']['binding_id']})
        self.assert_success(read); reference = read['result_refs'][0]
        self.assertEqual(reference['coverage']['origin'], 'native_binding')
        from workbench_platform.application_services.references import reference_location
        self.assertIn('native_binding=', reference_location(reference, self.ctx.session_id))
        item = read['data']['items'][0]
        disposed = await self.call('classification_dispose_results', {'reference': reference, 'item_ids': [item['native_item_id']], 'state': 'confirmed', 'label_id': 'invoice'})
        self.assert_success(disposed); self.assertEqual(disposed['result_refs'][0]['coverage']['origin'], 'native_binding')
        export = await self.call('native_export_result', {'binding_id': attached['data']['binding_id']})
        self.assert_success(export); self.assertTrue(verify(export['artifact_refs'][0], self.ctx)['files'])
        with self.assertRaises(ValueError):
            verify(disposed['result_refs'][0], replace(self.ctx, workspace_id='wother'))
        projects = self.assert_success(await self.call('native_list_projects', {'application': 'classification'}))
        self.assertTrue(projects['items'])
        created = await self.call('native_create_project', {'application': 'classification', 'name': '可训练项目', 'categories': ['凭证', '其他']}, call_id='project_once')
        self.assert_success(created); self.assertEqual(created, await self.call('native_create_project', {'application': 'classification', 'name': '可训练项目', 'categories': ['凭证', '其他']}, call_id='project_once'))
        self.ctx = oldctx

    async def test_native_preprocessing_and_real_cpu_fusion_keep_sources(self):
        from PIL import Image
        from ocr_workbench.service import create_app
        from ocr_workbench.imaging import prepare_task
        from workbench_platform.jobs.ocr_handoff import OcrHandoff
        from workbench_platform.jobs.native_ocr import OcrAdapter, Capabilities
        from workbench_platform.agent.tools import ocr_processing
        with patch.dict('os.environ', {'WORKBENCH_APP_ID': 'ocr', 'WORKBENCH_INSTANCE_ID': 'instanceocr', 'WORKBENCH_NONCE': 'b'*48}):
            app = create_app(OCR, self.root/'native-processing', 'coverage-token-'+'a'*40, start_queue=False, review_only=True, agent_enabled=False)
        native = app.state.store; project = native.project('处理选项')
        url = self.serve(app); link = self.link('ocr', project['id']); self.jobs({'ocr': url}, {'ocr': 'instanceocr'})
        self.services.ocr_handoff = OcrHandoff(self.root/'ocr-handoffs.sqlite3', self.services.job_service, self.assets, self.sets)
        self.assets.roots['synthetic'] = self.root
        Image.new('RGB', (90,60), 'white').save(self.root/'processing.png')
        asset = self.assets.register('wtest', 'synthetic', 'processing.png', media_type='image/png', storage_mode='managed')
        fixed = self.assert_success(await self.call('assets_create_set', {'asset_ids': [asset.asset_id]}))
        engines = {'ppocr': {'package_id': 'builtin', 'name': 'ppocr'}, 'hunyuan': {'package_id': 'builtin', 'name': 'hunyuan'}}
        app.state.application_services.require_recognition = lambda: None
        app.state.application_services.registry = SimpleNamespace(engines=lambda: engines)
        args = {'link_id': link.link_id, 'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision'],
                'engines': ['ppocr', 'hunyuan'], 'preprocess': [{'kind': 'rotate', 'degrees': 90}]}
        with patch.object(OcrAdapter, 'capabilities', return_value=Capabilities(True, True, True, True, ('json',), None)):
            submitted = await self.call('ocr_run', args, settle=False, call_id='processing_once')
        self.assertEqual(submitted['status'], 'pending', submitted)
        for index in range(2):
            task = prepare_task(native, native.claim())
            version = native.one('versions', task['version_id'])
            self.assertEqual((version['width'], version['height']), (60,90))
            native.complete(task['id'], {'engine': task['engine'], 'text': '编号 00001\n金额 10.00', 'tables': [], 'blocks': []})
        read = await self.call('ocr_read_results', {'handoff_id': submitted['data']['handoff_id']})
        self.assert_success(read); self.assertEqual(len(read['result_refs']), 2)
        self.assertTrue(all(verify(ref, self.ctx)['source_asset']['asset_id'] == asset.asset_id for ref in read['result_refs']))
        self.services.ocr_handoff.result_snapshot('wtest', submitted['data']['handoff_id'])
        catalog = self.assert_success(await self.call('ocr_list_processing_options', {'link_id': link.link_id}))
        self.assertTrue(catalog['fusion_policies'])
        fusionargs = {'references': read['result_refs'], 'content_type': 'print'}
        fusion = await self.call('ocr_fuse_results', fusionargs, settle=False, call_id='fusion_once')
        self.assertEqual(fusion['status'], 'pending', fusion)
        self.assertIsNone(ocr_processing.settle(fusion['job_refs'][0], self.ctx))
        task = native.claim(fusion=True); native.run_fusion(task)
        completed = ocr_processing.settle(fusion['job_refs'][0], self.ctx)
        self.assert_success(completed); self.assertTrue(verify(completed['result_refs'][0], self.ctx)['snapshot']['original']['fusion'])
        self.assert_success(await self.call('ocr_read_issues', {'reference': completed['result_refs'][0]}))
        self.assertEqual(fusion, await self.call('ocr_fuse_results', fusionargs, settle=False, call_id='fusion_once'))
        self.assertEqual(native.rows("SELECT count(*) n FROM tasks WHERE kind='fusion'")[0]['n'], 1)

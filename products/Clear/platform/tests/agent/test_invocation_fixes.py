"""Regressions for the seven confirmed invocation defects, without GPU/model calls."""
import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_data_tools as data_tests
import test_graph as graph_tests
import test_routes as route_tests
from packages.contracts.agent import AgentRef
from packages.contracts.audit import DirectEvidenceRequest, AuditDraftWrite, AuditDraftExport
from workbench_platform.agent.store import Conflict, digest, canonical
from workbench_platform.agent.tools import audit, results
from workbench_platform.agent.tools.audit import Search
from workbench_platform.application_services.manual import candidates
from workbench_platform.application_services.references import verify, data_reference, reference_location
from workbench_platform.application_services.search import search
from workbench_platform.audit.service import AuditService


class InvocationFixTests(unittest.IsolatedAsyncioTestCase):
    setUp = data_tests.DataToolTests.setUp
    tearDown = data_tests.DataToolTests.tearDown
    asset = data_tests.DataToolTests.asset
    call = data_tests.DataToolTests.call
    create = data_tests.DataToolTests.create
    import_asset = data_tests.DataToolTests.import_asset
    make_runtime = graph_tests.GraphTests.make_runtime
    wait_status = graph_tests.GraphTests.wait_status

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 60
        self.assets.roots['data'] = self.native.root
        self.native.asset_registry = self.assets

    def enable_audit(self):
        self.services.batch_service = SimpleNamespace(workspace=lambda identifier: self.workspaces.get_workspace(identifier))
        self.services.audit_service = AuditService(SimpleNamespace(state=self.services), Path(self.temporary.name) / 'audit')
        return self.services.audit_service.drafts

    async def test_revision_conflict_is_observed_and_model_can_read_then_correct(self):
        self.enable_audit()
        async def provider(state, schemas):
            previous = state['messages'][-1]
            if state['step'] == 0:
                return graph_tests.response(state, 'audit_write_finding', {'expected_revision': 1, 'title': '待补', 'missing_information': '缺资料'})
            if state['step'] == 1:
                self.assertEqual(previous['result']['error']['code'], 'revision_conflict')
                return graph_tests.response(state, 'audit_project_overview')
            if state['step'] == 2:
                self.assertEqual(previous['result']['status'], 'success')
                return graph_tests.response(state, 'audit_write_finding', {'expected_revision': 0, 'title': '待补', 'missing_information': '缺资料'})
            self.assertEqual(previous['result']['status'], 'success')
            return graph_tests.response(state, 'conversation_answer', {'content': '已按最新版本保存待补信息。'})
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '保存待补信息', 'fixconflict')
            final = await self.wait_status(runtime, run['id'], {'answered', 'failed'})
            self.assertEqual(final['status'], 'answered', runtime.store.events(session['id']))
            self.assertEqual(len(runtime.store.rows('SELECT * FROM operations WHERE run_id=?', (run['id'],))), 4)
            self.assertEqual(len(self.services.audit_service.drafts.list(replace(self.ctx, session_id=session['id'], store=runtime.store))['items']), 1)
        finally:
            await runtime.close()

    async def test_call_identity_conflict_terminates_instead_of_orphan_running(self):
        async def provider(state, schemas):
            value = graph_tests.response(state, 'data_create_session', {'name': 'one' if state['step'] == 0 else 'two'})
            value['calls'][0]['call_id'] = 'reusedidentity'
            return value
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '冲突', 'identityconflict')
            final = await self.wait_status(runtime, run['id'], {'failed'})
            self.assertEqual(final['status'], 'failed')
            self.assertEqual(len(self.native.list('wtest')), 1)
            self.assertTrue(any(e['payload'].get('code') == 'execution_conflict' for e in runtime.store.events(session['id'])['items']))
        finally:
            await runtime.close()

    async def test_model_configuration_conflict_leaves_explicit_failed_state(self):
        async def provider(state, schemas):
            raise Conflict('configuration changed')
        runtime, session = await self.make_runtime(provider)
        try:
            run = await runtime.submit(session['id'], '配置竞争', 'configconflict')
            self.assertEqual((await self.wait_status(runtime, run['id'], {'failed'}))['status'], 'failed')
        finally:
            await runtime.close()

    async def test_artifacts_from_every_application_describe_bind_and_open_correctly(self):
        drafts = self.enable_audit()
        runtime, session = await self.make_runtime(lambda *args: None)
        try:
            ctx = replace(self.ctx, session_id=session['id'], run_id='', store=runtime.store)
            asset = self.asset('suppliers-left.csv')
            ref = AgentRef(kind='asset', object_id=asset.asset_id, workspace_id='wtest', revision=asset.revision, sha256=asset.blob_hash)
            bound = drafts.bind(DirectEvidenceRequest(reference=ref), ctx)
            finding = drafts.write(AuditDraftWrite(request_id='afinding', expected_revision=0, title='原资料', facts='固定资料', evidence_ids=[bound['evidence_id']]), ctx, {'kind': 'human'})
            bundle = drafts.export(AuditDraftExport(request_id='abundle', draft_ids=[finding['draft_id']], title='成果'), ctx, {'kind': 'human'})
            for app in ('platform', 'data', 'ocr', 'classification', 'forensics'):
                artifact = AgentRef(kind='artifact', object_id=bundle['bundle_id'], workspace_id='wtest', revision=1, application=app)
                described = results.describe(results.Describe(reference=artifact), ctx)
                self.assertEqual(described['data']['bundle_id'], bundle['bundle_id'])
                saved = drafts.bind(DirectEvidenceRequest(reference=artifact), ctx)
                self.assertTrue(saved['location_url'].startswith('/artifacts?'))
                self.assertIn('task_return=', saved['location_url'])
                self.assertTrue(drafts.evidence(saved['evidence_id'], ctx)['observation']['files'])
            for file in bundle['files']:
                item = self.center.file('wtest', bundle['bundle_id'], file['name'])
                with self.center.store.stage_download('wtest', item.artifact_id, expected_revision=item.revision, expected_hash=item.blob_hash) as path:
                    self.assertTrue(path.read_bytes())
            self.assertTrue(reference_location(ref.model_dump(), session['id']).startswith('/assets?'))
            self.assertTrue(reference_location({**ref.model_dump(), 'kind': 'asset_set'}, session['id']).startswith('/assets?'))
            self.assertTrue(reference_location({**ref.model_dump(), 'kind': 'job', 'application': 'ocr'}, session['id']).startswith('/jobs?'))
        finally:
            await runtime.close()

    async def test_fixed_classification_evidence_survives_labels_and_can_be_delivered(self):
        drafts = self.enable_audit()
        asset = self.asset('suppliers-left.csv')
        current = {'model_id': 'model', 'input_set_id': 'set', 'native_job_id': 'nativejob',
                   'items': [{'asset_id': asset.asset_id, 'human_label': 'old', 'predicted_label': 'predicted'}]}
        original = AgentRef(kind='result', application='classification', workspace_id='wtest', parent_id='handoff', object_id='nativejob', revision=digest(current['items']), sha256=digest(current['items']))
        saved_id = None
        async def provider(state, schemas):
            if state['step'] == 0:
                return graph_tests.response(state, 'audit_read_evidence', {'evidence_id': saved_id})
            previous = state['messages'][-1]['result']
            if state['step'] == 1:
                self.assertTrue(previous['data']['stale'])
                self.assertEqual(previous['data']['observation']['items'][0]['human_label'], 'old')
                return graph_tests.response(state, 'delivery_publish', {'summary': '交付保存时的历史分类事实，来源已更新', 'result_refs': previous['result_refs']})
            return graph_tests.response(state, content='已交付')
        runtime, session = await self.make_runtime(provider)
        try:
            ctx = replace(self.ctx, session_id=session['id'], run_id='', store=runtime.store, selected_assets=(asset.asset_id,))
            with patch('workbench_platform.agent.tools.classification.snapshot', side_effect=lambda *args: current):
                bound = drafts.bind(DirectEvidenceRequest(reference=original), ctx)
                saved_id = bound['evidence_id']
                from workbench_platform.audit.direct import snapshot
                legacy = snapshot(DirectEvidenceRequest(reference=original), ctx, legacy=True)
                for key in ('snapshot_version', 'source_assets'):
                    legacy.pop(key)
                with drafts.store.transaction() as db:
                    db.execute('INSERT INTO direct_audit_evidence VALUES(?,?,?,?)', (legacy['evidence_id'], 'wtest', session['id'], canonical(legacy)))
                current['items'] = [{'asset_id': asset.asset_id, 'human_label': 'new'}]
                with self.assertRaises(ValueError): verify(original.model_dump(), ctx)
                read = audit.read_evidence(audit.EvidenceRead(evidence_id=saved_id), ctx)
                self.assertTrue(drafts.evidence(legacy['evidence_id'], ctx)['stale'])
                self.assertTrue(read['warnings']); self.assertEqual(read['result_refs'][0]['kind'], 'evidence')
                self.assertTrue(verify(read['result_refs'][0], ctx)['stale'])
                finding = drafts.write(AuditDraftWrite(request_id='stale_finding', expected_revision=0, title='历史分类', facts='old', evidence_ids=[saved_id]), ctx, {'kind': 'human'})
                self.assertTrue(drafts.get(finding['draft_id'], ctx)['outdated'])
                bundle = drafts.export(AuditDraftExport(request_id='stale_export', draft_ids=[finding['draft_id']], title='历史底稿'), ctx, {'kind': 'human'})
                self.assertEqual(bundle['status'], 'outdated')
                run = await runtime.submit(session['id'], '读取固定证据并交付', 'stale_delivery')
                self.assertEqual((await self.wait_status(runtime, run['id'], {'succeeded', 'failed', 'incomplete'}))['status'], 'succeeded', runtime.store.events(session['id']))
                with self.assertRaises(ValueError): drafts.evidence(saved_id, replace(ctx, selected_assets=('otherasset',)))
                with self.assertRaises(ValueError): drafts.evidence(saved_id, replace(ctx, workspace_id='wother'))
                with drafts.store.transaction() as db:
                    row = db.execute('SELECT body FROM direct_audit_evidence WHERE evidence_id=?', (saved_id,)).fetchone()
                    tampered = json.loads(row['body']); tampered['observation']['items'][0]['human_label'] = 'tampered'
                    db.execute('UPDATE direct_audit_evidence SET body=? WHERE evidence_id=?', (canonical(tampered), saved_id))
                with self.assertRaisesRegex(ValueError, '哈希'): drafts.evidence(saved_id, ctx)
        finally:
            await runtime.close()

    async def test_fixed_derived_input_keeps_explicit_scope_and_checks_lineage(self):
        drafts = self.enable_audit()
        uploads = Path(self.temporary.name) / 'uploads'; uploads.mkdir()
        self.assets.roots['uploads'] = uploads
        original = self.asset('suppliers-left.csv')
        bridged = results.install(b'a,b\n001,10\n', 'derived.csv', 'text/csv', [], [original.asset_id], {}, replace(self.ctx, operation_id='fixbridge'))
        derived = self.assets.get('wtest', bridged['data']['asset_id'])
        runtime, session = await self.make_runtime(lambda *args: None)
        try:
            ctx = replace(self.ctx, session_id=session['id'], run_id='', selected_assets=(derived.asset_id,))
            ref = AgentRef(kind='asset', workspace_id='wtest', object_id=derived.asset_id, revision=derived.revision, sha256=derived.blob_hash)
            saved = drafts.bind(DirectEvidenceRequest(reference=ref), ctx)
            self.assertEqual(saved['source_assets'][0]['asset_id'], derived.asset_id)
            self.assertFalse(drafts.evidence(saved['evidence_id'], ctx)['stale'])
            self.assertFalse(drafts.evidence(saved['evidence_id'], replace(ctx, selected_assets=(original.asset_id,)))['stale'])
            with self.store.transaction() as db:
                db.execute("UPDATE derived_inputs SET provenance_hash=? WHERE asset_id=?", ('f'*64, derived.asset_id))
            with self.assertRaises(ValueError): drafts.evidence(saved['evidence_id'], ctx)
        finally:
            await runtime.close()

    async def test_truncated_source_display_keeps_complete_hash_and_legacy_compatibility(self):
        name = 'many-source-rows.csv'
        (data_tests.RUN / 'fixtures' / name).write_text('code,amount\n' + ''.join(f'001,{i}\n' for i in range(260)), encoding='utf-8')
        session_id = await self.create(); asset = self.asset(name)
        await self.import_asset(session_id, 'left', asset)
        columns = self.native.get(session_id)['state']['tables']['left']['columns']
        grouped = await self.call('data_apply_step', {'data_session_id': session_id, 'expected_version': self.native.get(session_id)['version'],
            'side': 'left', 'step': {'operator': 'group_summary', 'columns': [columns[0]['column_id']], 'amount_columns': []}})
        self.assertEqual(grouped['status'], 'success')
        table = await self.call('data_read_table', {'data_session_id': session_id, 'side': 'left'})
        drafts = self.enable_audit()
        runtime, session = await self.make_runtime(lambda *args: None)
        try:
            ctx = replace(self.ctx, session_id=session['id'], run_id='', selected_assets=(asset.asset_id,))
            args = DirectEvidenceRequest(reference=table['result_refs'][0])
            saved = drafts.bind(args, ctx)
            self.assertEqual(saved['source_count'], 260); self.assertEqual(len(saved['sources']), 200)
            self.assertEqual(len(saved['source_assets']), 1)
            self.assertEqual(drafts.evidence(saved['evidence_id'], ctx)['snapshot_hash'], saved['snapshot_hash'])
            from workbench_platform.audit.direct import snapshot
            legacy = snapshot(args, ctx, legacy=True)
            for key in ('snapshot_version', 'source_assets'): legacy.pop(key)
            with drafts.store.transaction() as db:
                db.execute('INSERT INTO direct_audit_evidence VALUES(?,?,?,?)', (legacy['evidence_id'], 'wtest', session['id'], canonical(legacy)))
            self.assertEqual(drafts.evidence(legacy['evidence_id'], ctx)['source_count'], 260)
        finally:
            await runtime.close()

    async def test_search_selected_asset_beyond_first_project_page_and_manual_ocr(self):
        for number in range(205):
            name = f'search-{number}.txt'
            (data_tests.RUN / 'fixtures' / name).write_text(f'needle-{number}', encoding='utf-8')
            asset = self.assets.register('wtest', 'fixtures', name, media_type='text/plain', storage_mode='managed')
            if number == 0: target = asset
        runtime, session = await self.make_runtime(lambda *args: None)
        try:
            ctx = replace(self.ctx, session_id=session['id'], store=runtime.store, selected_assets=(target.asset_id,))
            with patch.object(self.assets, 'list', side_effect=AssertionError('selected scope must not start at project list')):
                found = search(Search(query='needle-0'), ctx)
            self.assertEqual(found['items'][0]['reference']['object_id'], target.asset_id)
            self.assertEqual(found['coverage']['asset_page_total'], 1)
            old = {'kind': 'result', 'application': 'ocr', 'workspace_id': 'wtest', 'parent_id': 'hp', 'object_id': 'result', 'revision': 1}
            new = {**old, 'revision': 2}
            task = {'result_refs': [old], 'initial_refs': [new], 'last_manual_change': {'references': [new]}}
            value = {'snapshot': {'original': {'text': 'deleted phrase'}, 'edited': {'text': 'manual phrase', 'tables': []}}, 'source_asset': {'asset_id': target.asset_id}, 'locator': {}}
            with patch.object(runtime.tasks, 'get', return_value=task), patch('workbench_platform.application_services.search.verify', return_value=value) as checked:
                found = search(Search(query='manual phrase'), ctx)
                self.assertEqual(found['items'][0]['reference']['revision'], 2)
                self.assertEqual(checked.call_count, 1)
                self.assertEqual(search(Search(query='deleted phrase'), ctx)['items'], [])
            all_project = search(Search(query='never'), replace(ctx, selected_assets=()))
            self.assertTrue(all_project['coverage']['candidate_files_truncated'])
        finally:
            await runtime.close()


class CandidatePagingTests(unittest.TestCase):
    def test_data_forensics_and_artifact_candidates_cross_service_pages(self):
        def page(items, offset, limit):
            return {'items': items[offset:offset+limit], 'total': len(items)}
        sessions = [{'id': f'data{i}'} for i in range(67)]
        bundles = [{'bundle_id': f'bundle{i}', 'title': f'成果 {i}'} for i in range(67)]
        inspections = [{'inspection_id': f'inspect{i}', 'status': 'succeeded', 'revision': 1, 'report_hash': 'a'*64} for i in range(67)]
        services = SimpleNamespace(data_workbench=SimpleNamespace(list_page=lambda workspace, offset, limit: page(sessions, offset, limit),
            get=lambda identifier: {'id': identifier, 'name': identifier, 'state': {'tables': {'left': {'revision': 'v'+identifier}}, 'result_id': None}}),
            artifact_center=SimpleNamespace(list=lambda workspace, offset, limit: page(bundles, offset, limit)),
            forensic_service=SimpleNamespace(list=lambda workspace, offset, limit: page(inspections, offset, limit)),
            agent=SimpleNamespace(tasks=SimpleNamespace(get=lambda identifier: {'result_refs': [], 'initial_refs': [], 'job_refs': []})))
        ctx = SimpleNamespace(services=services, workspace_id='wtest', session_id='task', selected_assets=())
        with patch('workbench_platform.application_services.manual.data_reference', return_value={'content_sha256': 'b'*64}), \
             patch('workbench_platform.application_services.manual.verify', return_value={}), \
             patch('workbench_platform.application_services.manual.reference_assets', return_value=[]):
            collected = []; offset = 0
            while True:
                value = candidates(ctx, offset, 50); collected.extend(value['items'])
                if value['next_offset'] is None: break
                offset = value['next_offset']
            self.assertEqual(len(collected), 201)
            identifiers = {item['reference']['object_id'] for item in collected}
            self.assertIn('vdata66', identifiers); self.assertIn('inspect66', identifiers); self.assertIn('bundle66', identifiers)
            self.assertEqual(len(candidates(ctx, keyword='成果 66')['items']), 1)

    def test_long_pdf_images_and_old_handoffs_all_remain_discoverable(self):
        def connect():
            db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE ocr_handoff(handoff_id TEXT,workspace_id TEXT,updated_at TEXT)')
            db.executemany('INSERT INTO ocr_handoff VALUES(?,?,?)', [(f'h{i}', 'wtest', str(1000-i)) for i in range(61)])
            return db
        services = SimpleNamespace(data_workbench=SimpleNamespace(list_page=lambda *args: {'items': [], 'total': 0}),
            artifact_center=SimpleNamespace(list=lambda *args: {'items': [], 'total': 0}), ocr_handoff=SimpleNamespace(_connect=connect),
            agent=SimpleNamespace(tasks=SimpleNamespace(get=lambda identifier: {'result_refs': [], 'initial_refs': [], 'job_refs': []})))
        ctx = SimpleNamespace(services=services, workspace_id='wtest', session_id='task', selected_assets=())
        def ref(parent, index):
            return {'kind': 'result', 'application': 'ocr', 'workspace_id': 'wtest', 'parent_id': parent, 'object_id': f'r{parent}_{index}', 'revision': 1}
        adapter = SimpleNamespace(document_pages=lambda document, offset, limit: {'pages': [{'page_number': i+1} for i in range(offset, min(offset+limit, 73))], 'total': 73})
        def read(args, context):
            if args.handoff_id == 'h0': raise ValueError('PDF')
            if args.handoff_id == 'h1':
                return {'result_refs': [ref('h1', i) for i in range(args.offset, min(args.offset+args.limit, 63))], 'next_offset': args.offset+args.limit if args.offset+args.limit < 63 else None}
            return {'result_refs': [ref(args.handoff_id, 0)], 'next_offset': None}
        with patch('workbench_platform.application_services.manual.verify', return_value={}), patch('workbench_platform.application_services.manual.reference_assets', return_value=[]), \
             patch('workbench_platform.agent.tools.ocr.read', side_effect=read), patch('workbench_platform.agent.tools.pdf.bound', return_value=(None, None, adapter, ['document'])), \
             patch('workbench_platform.agent.tools.pdf.read', side_effect=lambda args, ctx: {'result_refs': [ref('h0', args.page_number)]}):
            all_items = []; offset = 0
            while True:
                page = candidates(ctx, offset, 50)
                all_items.extend(page['items'])
                if page['next_offset'] is None: break
                offset = page['next_offset']
            self.assertEqual(len(all_items), 73+63+59)
            ids = {item['reference']['object_id'] for item in all_items}
            self.assertIn('rh0_73', ids); self.assertIn('rh1_62', ids); self.assertIn('rh60_0', ids)
            self.assertEqual(len(ids), len(all_items))


class ManualUploadRouteTests(unittest.TestCase):
    setUp = route_tests.AgentRouteTests.setUp
    tearDown = route_tests.AgentRouteTests.tearDown

    def test_upload_identity_explicit_include_return_idempotency_and_agent_readback(self):
        client, base, headers = self.client, '/api/platform/v1', route_tests.LOCAL
        native = client.app.state.data_workbench
        native.python = data_tests.PYTHON
        registry = client.app.state.asset_library[0]
        session = client.post(base+'/data/sessions', headers=headers, json={'name': '手动', 'workspace_id': self.workspace}).json()['id']
        upload = client.post(base+f'/data/sessions/{session}/uploads?name=same.csv', headers=headers, content=b'code,amount\n001,1\n')
        self.assertEqual(upload.status_code, 201, upload.text)
        asset_id = upload.json()['source_asset']['asset_id']
        same = client.post(base+f'/data/sessions/{session}/uploads?name=same.csv', headers=headers, content=b'code,amount\n001,1\n')
        self.assertEqual(same.json()['source_asset']['asset_id'], asset_id)
        agent = client.post(base+f'/workspaces/{self.workspace}/agent/sessions', headers=headers, json={'name': '手动任务', 'request_id': 'manual_task', 'selected_assets': [asset_id]}).json()['id']
        task_path = base+f'/agent/sessions/{agent}'
        revision = client.get(task_path+'/task', headers=headers).json()['revision']
        client.post(task_path+'/task/control', headers=headers, json={'request_id': 'take', 'expected_revision': revision, 'action': 'take_over'})
        def import_upload(upload_id, side):
            job = native.submit(session, 'import', {'upload_id': upload_id, 'side': side, 'options': {}}, native.get(session)['version'])
            import time
            for _ in range(200):
                state = native.job(session, job['id'])
                if state['status'] in {'succeeded', 'failed'}: break
                time.sleep(.05)
            self.assertEqual(state['status'], 'succeeded', state)
        import_upload(same.json()['id'], 'left')
        page = client.get(task_path+'/manual-results', headers=headers).json()
        self.assertEqual(page['items'][0]['required_assets'], [])
        original_ref = page['items'][0]['reference']
        ctx = client.app.state.agent.context({'workspace_id': self.workspace, 'session_id': agent, 'run_id': '', 'generation': 0})
        self.assertEqual(verify(original_ref, ctx)['rows'][0]['cells'][0]['value'], '001')
        new = client.post(base+f'/data/sessions/{session}/uploads?name=new.csv', headers=headers, content=b'code,amount\n002,2\n').json()
        import_upload(new['id'], 'right')
        page = client.get(task_path+'/manual-results?limit=1&offset=1', headers=headers).json()
        candidate = page['items'][0]
        self.assertEqual(candidate['required_assets'][0]['asset_id'], new['source_asset']['asset_id'])
        revision = client.get(task_path+'/task', headers=headers).json()['revision']
        command = {'request_id': 'manualreturn', 'expected_revision': revision, 'summary': '新增资料', 'references': [candidate['reference']]}
        self.assertEqual(client.post(task_path+'/manual-return', headers=headers, json=command).status_code, 400)
        command['include_asset_ids'] = [new['source_asset']['asset_id']]
        returned = client.post(task_path+'/manual-return', headers=headers, json=command)
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertEqual(client.post(task_path+'/manual-return', headers=headers, json=command).json(), returned.json())
        current = client.get(task_path+'/task', headers=headers).json()
        self.assertEqual(set(current['selected_assets']), {asset_id, new['source_asset']['asset_id']})
        ctx = replace(ctx, selected_assets=tuple(current['selected_assets']))
        self.assertEqual(verify(candidate['reference'], ctx)['rows'][0]['cells'][0]['value'], '002')
        client.post(task_path+'/task/control', headers=headers, json={'request_id': 'take2', 'expected_revision': current['revision'], 'action': 'take_over'})
        self.assertEqual(client.post(task_path+'/manual-return', headers=headers, json={**command, 'request_id': 'stale'}).status_code, 409)
        revision = client.get(task_path+'/task', headers=headers).json()['revision']
        fake = {**candidate['reference'], 'sha256': 'f'*64}
        self.assertEqual(client.post(task_path+'/manual-return', headers=headers, json={**command, 'request_id': 'forged', 'expected_revision': revision, 'references': [fake]}).status_code, 400)
        self.assertEqual(client.post(task_path+'/manual-return', headers=headers, json={**command, 'request_id': 'foreign', 'expected_revision': revision, 'include_asset_ids': ['foreignasset']}).status_code, 400)
        with closing(native.db()) as db, db:
            db.execute('UPDATE uploads SET source_asset_id=NULL,source_workspace_id=NULL,source_asset_revision=NULL,source_blob_hash=NULL WHERE id=?', (same.json()['id'],))
        self.assertIsNone(native.upload_source(session, same.json()['id'])['source_asset_id'])
        client.get(task_path+'/manual-results', headers=headers)
        self.assertEqual(native.upload_source(session, same.json()['id'])['source_asset_id'], asset_id)

"""SC07: real local audit/asset ledgers, no model or business submission."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from packages.contracts.agent import AgentRef
from packages.contracts.audit import AuditDraftWrite, DirectEvidenceRequest, AuditEvidenceSnapshot
from scripts.product_compatibility import check, DATA_COMPATIBILITY
from workbench_platform.agent.registry import ToolContext
from workbench_platform.agent.store import AgentStore
from workbench_platform.agent.tasks import AuditTasks
from workbench_platform.assets.registry import AssetRegistry
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.data_api.service import DataWorkbenchService, DataError
from workbench_platform.store import WorkspaceStore
from workbench_platform.audit.service import AuditService
from workbench_platform.audit.routes import router

RUN = Path('F:/OCR/output/platform-convergence-dev-20261005')
ACTOR = {'id': 'localTransfer', 'display_name': '合成转档操作人', 'source': 'local'}
HEADERS = {'Origin': 'http://127.0.0.1:8766'}
BASE = '/api/platform/v1'


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=RUN / 'workspace')
        self.root = Path(self.temp.name)
        source = self.root / 'source'; source.mkdir()
        (source / 'ledger.csv').write_text('编号,金额\n000001,123.45\n', encoding='utf-8')
        (self.root / 'blobs').mkdir()
        self.workspaces = WorkspaceStore(self.root / 'workspaces.sqlite3')
        self.workspaces.create_workspace('合成项目', workspace_id='wtest')
        self.workspaces.create_workspace('另一项目', workspace_id='wother')
        self.assets = AssetRegistry(self.root / 'assets.sqlite3', self.root / 'blobs', {'source': source})
        self.asset = self.assets.register('wtest', 'source', 'ledger.csv', media_type='text/csv', storage_mode='managed')
        self.sets = AssetSetStore(self.assets)
        self.data = DataWorkbenchService(self.root / 'data', None)
        self.center = ArtifactCenter(self.root / 'artifacts', self.assets, self.sets, self.data)
        self.store = AgentStore(self.root / 'agent')
        session = self.store.create_session('wtest', '合成任务', [self.asset.asset_id], 'sessionFixture')
        run, _ = self.store.start_run(session['id'], '合成任务', 'runFixture', 1)
        services = SimpleNamespace(workspace_store=self.workspaces, asset_library=(self.assets, self.sets, None),
            data_workbench=self.data, artifact_center=self.center,
            batch_service=SimpleNamespace(workspace=self.workspaces.get_workspace, store=SimpleNamespace(lock=threading.RLock())))
        self.runtime = SimpleNamespace(store=self.store, services=services)
        services.agent = self.runtime
        self.runtime.tasks = AuditTasks(self.runtime)
        self.runtime.tasks.record_goal(session['id'], '核对合成编号并保存来源')
        self.ctx = ToolContext('wtest', session['id'], run['id'], 1, 0, '', (self.asset.asset_id,), services, self.store)
        self.app = FastAPI(); self.app.state = services
        self.audit = services.audit_service = AuditService(self.app, self.root / 'audit')
        self.app.include_router(router)
        @self.app.exception_handler(DataError)
        async def error(request, value):
            return JSONResponse({'detail': str(value)}, status_code=value.status)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8766')
        ref = AgentRef(kind='asset', workspace_id='wtest', object_id=self.asset.asset_id,
                       revision=self.asset.revision, sha256=self.asset.blob_hash)
        evidence = self.audit.drafts.bind(DirectEvidenceRequest(reference=ref), self.ctx)
        self.source_evidence = evidence['evidence_id']
        self.raw = {'request_id': 'draftFixture', 'expected_revision': 0, 'title': '固定合成发现',
                    'facts': '编号000001金额123.45', 'interpretation': '仅为工程事实解释', 'rule': '固定金额口径',
                    'missing_information': '尚需业务说明', 'recommendations': '补充原始凭证',
                    'evidence_ids': [self.source_evidence], 'status': 'confirmed'}
        draft = self.audit.drafts.write(AuditDraftWrite(**self.raw), self.ctx,
                                       {'kind': 'agent', 'model': 'synthetic-fixture', 'formal_signature': False})
        self.draft_id = draft['draft_id']
        self.draft = self.audit.drafts.get(self.draft_id, self.ctx)

    def tearDown(self):
        self.client.close(); self.data.close(); self.temp.cleanup()

    def archive(self, name='archiveA', workspace='wtest'):
        create = {'schema_version': '1.0', 'audit_version': '1.0', 'request_id': name, 'actor': ACTOR,
                  'project': {'title': name, 'audit_object': '合成台账', 'period_start': '2026-01-01',
                              'period_end': '2026-12-31', 'objective': '核对固定资料'}}
        result = self.client.post(BASE + f'/workspaces/{workspace}/audit-projects', headers=HEADERS, json=create)
        self.assertEqual(result.status_code, 201, result.text)
        project_id = result.json()['entity_id']
        procedure = {'schema_version': '1.0', 'audit_version': '1.0', 'request_id': name + 'Procedure',
                     'actor': ACTOR, 'expected_revision': 1, 'batches': [],
                     'procedure': {'title': '直接证据程序', 'basis': '固定实际原件', 'scope': '编号000001', 'conclusion': '待人工完成'}}
        result = self.client.post(BASE + f'/audit-projects/{project_id}/procedures', headers=HEADERS, json=procedure)
        self.assertEqual(result.status_code, 201, result.text)
        return project_id, result.json()['entity_id']

    def transfer(self, project_id, procedure_id, request_id='transferOne', source=None):
        return {'schema_version': '1.0', 'audit_version': '1.0', 'request_id': request_id, 'actor': ACTOR,
                'expected_revision': self.audit.get(project_id)['revision'], 'procedure_id': procedure_id,
                'source_draft': source or self.draft['transfer_source']}

    def post(self, project_id, payload):
        return self.client.post(BASE + f'/audit-projects/{project_id}/findings', headers=HEADERS, json=payload)

    def counts(self):
        return {name: self.store.rows(f'SELECT count(*) AS n FROM {name}')[0]['n']
                for name in ('sessions', 'runs', 'model_requests', 'operations')}

    def test_two_archives_replay_source_and_export_readback(self):
        counts = self.counts()
        a, ap = self.archive(); b, bp = self.archive('archiveB')
        payload = self.transfer(a, ap)
        response = self.post(a, payload); self.assertEqual(response.status_code, 201, response.text)
        receipt = response.json(); finding = self.audit.get(a, 'finding', receipt['entity_id'])
        self.assertEqual(self.post(a, payload).json(), receipt)
        conflict = deepcopy(payload); conflict['actor']['display_name'] = '改变原请求'
        self.assertEqual(self.post(a, conflict).status_code, 409)
        second = self.post(b, self.transfer(b, bp, 'transferTwo'))
        self.assertEqual(second.status_code, 201, second.text)
        other = self.audit.get(b, 'finding', second.json()['entity_id'])
        compatibility = check(self.root, DATA_COMPATIBILITY)
        self.assertTrue(compatibility['compatible'])
        self.assertEqual(compatibility['writes'], 0)
        refs = finding['evidence_refs']; other_refs = other['evidence_refs']
        self.assertNotEqual(refs[0]['evidence_id'], other_refs[0]['evidence_id'])
        self.assertEqual(refs[0]['source_evidence_id'], other_refs[0]['source_evidence_id'])
        self.assertEqual(self.client.get(BASE + f"/audit-projects/{b}/evidence/{refs[0]['evidence_id']}").status_code, 404)
        self.assertEqual(finding['origin']['author']['kind'], 'agent')
        self.assertEqual(finding['origin']['transfer_kind'], 'human')
        self.assertEqual(finding['records'], []); self.assertFalse(finding['records_current'])
        self.assertEqual(finding['finding']['status'], 'draft')
        evidence = self.audit.evidence(a, refs[0]['evidence_id'])
        AuditEvidenceSnapshot.model_validate({k: v for k, v in evidence.items() if k not in {'stale', 'outdated_reasons'}})
        self.assertEqual(evidence['observation'], self.audit.drafts.evidence(self.source_evidence, self.ctx)['observation'])
        request = {'schema_version': '1.0', 'audit_version': '1.0', 'request_id': 'exportDirect',
                   'actor': ACTOR, 'expected_revision': self.audit.get(a)['revision'], 'kind': 'intermediate'}
        exported = self.client.post(BASE + f'/audit-projects/{a}/workpapers', headers=HEADERS, json=request)
        self.assertEqual(exported.status_code, 201, exported.text)
        bundle = exported.json(); self.assertEqual(bundle['status'], 'current')
        from dataclasses import replace
        from workbench_platform.application_services.references import verify, reference_assets
        from workbench_platform.application_services.manual import candidates
        artifact_ref = AgentRef(kind='artifact', workspace_id='wtest', object_id=bundle['bundle_id'], revision=1, application='platform')
        allowed = verify(artifact_ref.model_dump(), self.ctx)
        self.assertEqual(reference_assets(artifact_ref.model_dump(), allowed, self.ctx), [self.asset.asset_id])
        (self.root / 'source' / 'restricted.csv').write_text('编号,金额\n000003,8.00\n', encoding='utf-8')
        restricted_asset = self.assets.register('wtest', 'source', 'restricted.csv', media_type='text/csv', storage_mode='managed')
        restricted = replace(self.ctx, selected_assets=(restricted_asset.asset_id,))
        with self.assertRaises(ValueError): verify(artifact_ref.model_dump(), restricted)
        saved_hash = allowed['content_sha256']
        self.assertEqual(verify(artifact_ref.model_dump(), replace(self.ctx, selected_assets=()))['content_sha256'], saved_hash)
        candidate = next(item for item in candidates(restricted)['items']
                         if item['reference']['object_id'] == bundle['bundle_id'])
        self.assertEqual([item['asset_id'] for item in candidate['required_assets']], [self.asset.asset_id])
        self.assertFalse(candidate['related_to_task'])
        included = replace(restricted, selected_assets=(restricted_asset.asset_id, self.asset.asset_id))
        self.assertEqual(verify(artifact_ref.model_dump(), included)['content_sha256'], saved_hash)
        readback = {}
        for file in bundle['files']:
            artifact = self.center.file('wtest', bundle['bundle_id'], file['name'])
            with self.center.store.stage_download('wtest', artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                readback[file['name']] = Path(path).read_bytes()
            self.assertEqual(hashlib.sha256(readback[file['name']]).hexdigest(), file['blob_hash'])
            self.assertIn(self.asset.asset_id, [ref.asset_id for ref in artifact.source_refs])
        document = json.loads(readback['workpaper.json'])
        self.assertEqual(document['batches'], [])
        self.assertFalse(any('关联执行证据' in reason for reason in document['unresolved_reasons']))
        self.assertEqual(document['findings'][0]['origin']['content']['facts'], self.raw['facts'])
        for field in ('facts', 'interpretation', 'rule', 'missing_information', 'recommendations'):
            self.assertIn(self.raw[field], readback['workpaper.md'].decode('utf-8'))
        saved = self.audit.evidence(a, refs[0]['evidence_id'])['observation']
        edit = {**self.raw, 'request_id': 'sourceEdit', 'draft_id': self.draft_id, 'expected_revision': 1, 'facts': '新事实版本'}
        self.audit.drafts.write(AuditDraftWrite(**edit), self.ctx, {'kind': 'human'})
        self.assertTrue(self.audit.get(a, 'finding', receipt['entity_id'])['outdated'])
        self.assertEqual(self.audit.evidence(a, refs[0]['evidence_id'])['observation'], saved)
        self.assertEqual(self.post(a, payload).json(), receipt)
        self.assertEqual(self.audit.workpaper(a, bundle['bundle_id'])['status'], 'outdated')
        self.assertEqual(len(self.audit.drafts.get(self.draft_id, self.ctx)['formal_links']), 2)
        self.assertEqual(self.counts(), counts)
        (RUN / 'evidence/sc07-readback.json').write_text(json.dumps({'passed': True, 'archives': [a, b],
            'formal_evidence_ids': [refs[0]['evidence_id'], other_refs[0]['evidence_id']],
            'source_evidence_id': self.source_evidence, 'origin': finding['origin'],
            'business_model_counts_unchanged': counts, 'export_sha256': {k: hashlib.sha256(v).hexdigest() for k, v in readback.items()},
            'compatibility': compatibility, 'fake_batches': 0}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def test_source_target_scope_and_origin_tampering_rejected(self):
        a, ap = self.archive(); other, op = self.archive('otherArchive', 'wother')
        self.assertEqual(self.post(other, self.transfer(other, op)).status_code, 404)
        payload = self.transfer(a, ap)
        bad = deepcopy(payload); bad['source_draft']['snapshot_hash'] = '0' * 64
        self.assertEqual(self.post(a, bad).status_code, 409)
        bad = deepcopy(payload); bad['finding'] = {'title': '替换事实', 'status': 'resolved'}
        self.assertEqual(self.post(a, bad).status_code, 400)
        stale_target = deepcopy(payload); stale_target['expected_revision'] = 1
        self.assertEqual(self.post(a, stale_target).status_code, 409)
        with self.store.transaction() as db:
            body = json.loads(db.execute('SELECT body FROM audit_tasks WHERE session_id=?', (self.ctx.session_id,)).fetchone()[0])
            body['scope_revision'] += 1
            db.execute('UPDATE audit_tasks SET body=? WHERE session_id=?', (json.dumps(body), self.ctx.session_id))
        self.assertEqual(self.post(a, payload).status_code, 409)
        self.assertEqual(self.audit.get(a)['finding_count'], 0)

    def test_source_revision_race_and_direct_selection_scope(self):
        a, ap = self.archive(); old = self.transfer(a, ap)
        changed = {**self.raw, 'draft_id': self.draft_id, 'expected_revision': 1, 'request_id': 'raceEdit', 'facts': '已修订事实'}
        self.audit.drafts.write(AuditDraftWrite(**changed), self.ctx, {'kind': 'human'})
        self.assertEqual(self.post(a, old).status_code, 409)
        selected = {'schema_version': '1.0', 'audit_version': '1.0', 'request_id': 'directSelection', 'actor': ACTOR,
            'expected_revision': 2, 'procedure_id': ap, 'finding': {'title': '直接证据选择'},
            'evidence': [{'task_id': self.ctx.session_id, 'source_evidence_id': self.source_evidence,
                          'snapshot_hash': self.audit.drafts.evidence(self.source_evidence, self.ctx)['snapshot_hash']}]}
        result = self.post(a, selected); self.assertEqual(result.status_code, 201, result.text)
        finding = self.audit.get(a, 'finding', result.json()['entity_id'])
        from dataclasses import replace
        (self.root / 'source' / 'unselected.csv').write_text('编号,金额\n000002,5.00\n', encoding='utf-8')
        unselected = self.assets.register('wtest', 'source', 'unselected.csv', media_type='text/csv', storage_mode='managed')
        with self.assertRaises(DataError):
            self.audit.evidence(a, finding['evidence_refs'][0]['evidence_id'], context=replace(self.ctx, selected_assets=(unselected.asset_id,)))
        with self.assertRaises(DataError):
            self.audit.evidence(a, finding['evidence_refs'][0]['evidence_id'], context=replace(self.ctx, workspace_id='wother'))
        # Existing source snapshot stays unchanged after a tampered archive row.
        with self.audit.store.transaction() as db:
            eid = finding['evidence_refs'][0]['evidence_id']
            row = json.loads(db.execute('SELECT body FROM evidence_snapshots WHERE evidence_id=?', (eid,)).fetchone()[0])
            row['observation']['tampered'] = True
            db.execute('UPDATE evidence_snapshots SET body=? WHERE evidence_id=?', (json.dumps(row), eid))
        with self.assertRaises(DataError): self.audit.evidence(a, eid)
        self.assertNotIn('tampered', self.audit.drafts.evidence(self.source_evidence, self.ctx)['observation'])

    def test_agent_bridge_background_pending_scope_and_generation(self):
        from dataclasses import replace
        from workbench_platform.agent.tools.platform_management import FindingCreate, write_audit, AuditRead, read_audit
        a, ap = self.archive(); b, bp = self.archive('archiveB')
        args = FindingCreate(project_id=a, expected_revision=2, procedure_id=ap,
                             source_draft=self.draft['transfer_source'])
        result = write_audit(args, replace(self.ctx, operation_id='agentTransfer'), 'finding')
        receipt = result['data']; finding = self.audit.get(a, 'finding', receipt['entity_id'])
        self.assertEqual(finding['origin']['transfer_kind'], 'agent')
        self.assertEqual(finding['origin']['author']['model'], 'synthetic-fixture')
        self.assertEqual(finding['origin']['transfer_actor']['display_name'], 'AI审计Agent')
        self.assertEqual(finding['records'], [])
        evidence = read_audit(AuditRead(project_id=a, kind='evidence',
            entity_id=finding['evidence_refs'][0]['evidence_id']), self.ctx)['data']
        self.assertEqual(evidence['observation'], self.audit.drafts.evidence(self.source_evidence, self.ctx)['observation'])
        self.assertEqual(evidence['source_asset_count'], 1)
        with self.assertRaises(ValueError):
            write_audit(FindingCreate(project_id=b, expected_revision=2, procedure_id=bp,
                        source_draft=self.draft['transfer_source']),
                        replace(self.ctx, generation=0, operation_id='staleGeneration'), 'finding')
        self.assertEqual(self.audit.get(b)['finding_count'], 0)
        (self.root / 'source' / 'outside.csv').write_text('编号,金额\n000002,5.00\n', encoding='utf-8')
        outside = self.assets.register('wtest', 'source', 'outside.csv', media_type='text/csv', storage_mode='managed')
        with self.assertRaises(ValueError):
            write_audit(FindingCreate(project_id=b, expected_revision=2, procedure_id=bp,
                        source_draft=self.draft['transfer_source']),
                        replace(self.ctx, selected_assets=(outside.asset_id,), operation_id='outsideTransfer'), 'finding')
        self.assertEqual(self.audit.get(b)['finding_count'], 0)
        self.runtime.tasks.save_profile('wtest', {'request_id': 'profileEdit', 'expected_revision': 0,
            'audit_object': '新合成背景', 'period_start': '', 'period_end': '', 'objective': '新目标', 'rules': '新口径'})
        current = self.audit.get(a, 'finding', receipt['entity_id'])
        self.assertTrue(current['outdated'])
        self.assertIn('源项目背景或审计口径已变更', current['outdated_reasons'])
        self.assertEqual(current['origin'], finding['origin'])
        self.assertEqual(self.post(b, self.transfer(b, bp, 'oldBackground')).status_code, 409)
        fresh = self.audit.drafts.write(AuditDraftWrite(**{**self.raw, 'draft_id': self.draft_id,
            'expected_revision': 1, 'request_id': 'freshBackground'}), self.ctx, {'kind': 'human'})
        self.draft = self.audit.drafts.get(fresh['draft_id'], self.ctx)
        with self.store.transaction() as db:
            body = json.loads(db.execute('SELECT body FROM audit_tasks WHERE session_id=?', (self.ctx.session_id,)).fetchone()[0])
            body['pending_scope'] = {'selected_assets': [outside.asset_id], 'initial_refs': []}
            db.execute('UPDATE audit_tasks SET body=? WHERE session_id=?', (json.dumps(body), self.ctx.session_id))
        self.assertEqual(self.post(b, self.transfer(b, bp, 'pendingScope')).status_code, 409)
        self.assertEqual(self.audit.get(b)['finding_count'], 0)

    def test_source_version_remains_locked_during_formal_commit(self):
        import sqlite3
        from contextlib import contextmanager
        a, ap = self.archive()
        original = self.audit.store.transaction
        probes = []
        source_path = self.store.path
        testcase = self
        class CommitProbe:
            def __init__(self, db): self.db = db
            def __getattr__(self, name): return getattr(self.db, name)
            def commit(self):
                with closing(sqlite3.connect(source_path, timeout=0)) as competing:
                    with testcase.assertRaises(sqlite3.OperationalError):
                        competing.execute('BEGIN IMMEDIATE')
                probes.append('source-locked-before-target-commit')
                self.db.commit()
        @contextmanager
        def transaction():
            with original() as db: yield CommitProbe(db)
        with patch.object(self.audit.store, 'transaction', transaction):
            response = self.post(a, self.transfer(a, ap))
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(probes, ['source-locked-before-target-commit'])
        with self.store.transaction() as db:
            db.execute('UPDATE audit_project_profiles SET revision=revision WHERE workspace_id=?', ('wtest',))

    def test_other_task_draft_preserves_original_evidence_task(self):
        from dataclasses import replace
        session = self.store.create_session('wtest', '证据复用任务', [self.asset.asset_id], 'reuseTask')
        self.runtime.tasks.record_goal(session['id'], '复用已有固定证据')
        ctx = replace(self.ctx, session_id=session['id'], run_id='')
        draft = self.audit.drafts.write(AuditDraftWrite(**{**self.raw, 'request_id':'reuseDraft'}), ctx, {'kind':'human'})
        draft = self.audit.drafts.get(draft['draft_id'], ctx)
        a, ap = self.archive()
        response = self.post(a, self.transfer(a, ap, 'reuseTransfer', draft['transfer_source']))
        self.assertEqual(response.status_code, 201, response.text)
        finding = self.audit.get(a, 'finding', response.json()['entity_id'])
        self.assertEqual(finding['origin']['source']['task_id'], session['id'])
        self.assertEqual(finding['evidence_refs'][0]['task_id'], self.ctx.session_id)
        self.assertEqual(finding['evidence_refs'][0]['source_evidence_id'], self.source_evidence)
        self.assertFalse(finding['outdated'])


if __name__ == '__main__': unittest.main(verbosity=2)

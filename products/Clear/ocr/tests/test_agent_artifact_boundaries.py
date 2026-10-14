"""Real file-boundary and export-format checks at the Agent artifact entry."""
import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

import httpx
from PIL import Image

import test_agent_artifacts
from test_structure_workflow import table
from ocr_workbench.agent.store import AgentStore
from ocr_workbench.agent.policy import AgentPolicy
from ocr_workbench.agent.policy import PolicyDenied
from ocr_workbench.imaging import add_image
from ocr_workbench.service import create_app
from ocr_workbench.store import Store, now, uid


class ArtifactBoundaryTests(unittest.TestCase):
    setUp = test_agent_artifacts.ArtifactTests.setUp
    prepare = test_agent_artifacts.ArtifactTests.prepare

    def test_text_markdown_json_and_workbook_preserve_selected_snapshot(self):
        artifacts, args, context = self.prepare()
        result_id = args.results[0].result_id
        values = table([['Account', 'Amount'], ['00123456789012345678', '1234567890.01']])
        self.store.save(result_id, {'text': 'Account 00123456789012345678', 'tables': [values]}, 0)
        for step, format in enumerate(('txt', 'md', 'json', 'xlsx'), 1):
            with self.subTest(format=format):
                export = args.model_copy(update={'format': format, 'results': [args.results[0].model_copy(update={'revision': 1})]})
                self.policy.grant(self.project, 'scoped_new_artifact', {'document_ids': [self.photo['id']], 'page_ids': [self.photo['id']],
                    'version_ids': [self.photo['active_version']], 'result_ids': [result_id], 'format': format, 'partial_policy': 'ask'},
                    source='user_request', expires=time.time() + 60, run_id=self.run['id'])
                call = 'format-' + format
                self.agent.record_calls(self.project, self.run['id'], 1, step, [{'call_id': call, 'tool': 'export_results', 'arguments': export.model_dump(exclude_none=True)}])
                output = artifacts.export(export, {**context, 'step': step, 'call_id': call})
                key = output['artifact_refs'][0]['artifact_id']
                path = artifacts.verify(self.project, key)
                manifest = json.loads(artifacts.get(self.project, key)['manifest'])
                self.assertEqual(manifest['results'], [export.results[0].model_dump()])
                self.assertEqual(manifest['format'], format)
                self.assertEqual(manifest['run_id'], self.run['id'])
                self.assertEqual(manifest['sources'][0]['page_id'], self.photo['id'])
                self.assertEqual(manifest['sources'][0]['engine'], 'glm')
                self.assertEqual(manifest['sources'][0]['revision'], 1)
                self.assertFalse(manifest['sources'][0]['human_confirmed'])
                self.assertEqual(len(manifest['sources'][0]['edited_sha256']), 64)
                if format == 'xlsx':
                    from openpyxl import load_workbook
                    workbook = load_workbook(path)
                    try:
                        cells = [cell for sheet in workbook for row in sheet for cell in row]
                        identifier = next(cell for cell in cells if cell.value == '00123456789012345678')
                        self.assertEqual(identifier.data_type, 's')
                        self.assertTrue(any(str(cell.value) == '1234567890.01' for cell in cells))
                    finally:
                        workbook.close()
                elif zipfile.is_zipfile(path):
                    with zipfile.ZipFile(path) as archive:
                        payload = '\n'.join(archive.read(name).decode('utf-8-sig') for name in archive.namelist() if name.endswith('.' + format))
                    self.assertIn('00123456789012345678', payload)
                else:
                    self.assertIn('00123456789012345678', path.read_text('utf-8-sig'))

    def test_disk_full_during_copy_does_not_publish_ready_or_escape_staging(self):
        artifacts, args, context = self.prepare()
        with patch('ocr_workbench.agent.artifacts.shutil.copyfileobj', side_effect=OSError(errno.ENOSPC, 'Synthetic full volume')):
            with self.assertRaises(OSError):
                artifacts.export(args, context)
        self.assertFalse(self.store.rows("SELECT id FROM agent_artifacts WHERE state='ready'"))
        self.assertFalse(list((self.store.root / 'agent-artifacts').rglob('export.*')))
        self.assertFalse(list((self.store.root / 'agent-artifacts').rglob('*.staging-*')))
        # A retry of the same call reconciles the original operation, without
        # inventing another artifact after a recoverable filesystem failure.
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        self.assertEqual(len(self.store.rows('SELECT id FROM agent_artifacts')), 1)
        self.assertIn('42', artifacts.verify(self.project, key).read_text('utf-8-sig'))

    def test_manifest_path_traversal_invalidates_download_without_touching_other_file(self):
        artifacts, args, context = self.prepare()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        folder = artifacts.directory(self.project, key)
        marker = self.store.root / 'outside.txt'
        marker.write_text('Preserve this unrelated file', 'utf-8')
        manifest = json.loads((folder / 'manifest.json').read_text('utf-8'))
        manifest['file'] = '../../../outside.txt'
        (folder / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
        with self.assertRaises(PolicyDenied):
            artifacts.lease(self.project, key)
        self.assertEqual(artifacts.get(self.project, key)['state'], 'corrupt')
        self.assertEqual(marker.read_text('utf-8'), 'Preserve this unrelated file')
        for value in ('../escape', 'a' * 32 + '/escape', 'C:/outside', 'a' * 31):
            with self.assertRaises(ValueError):
                artifacts.directory(self.project, value)

    def test_missing_corrupt_and_transient_io_are_distinct(self):
        artifacts, args, context = self.prepare()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        path = artifacts.verify(self.project, key)
        with patch.object(artifacts, '_published', side_effect=PermissionError('busy file')):
            with self.assertRaises(PolicyDenied):
                artifacts.verify(self.project, key)
        self.assertEqual(artifacts.get(self.project, key)['state'], 'ready')
        original = path.read_bytes()
        path.write_bytes(b'Changed bytes')
        with self.assertRaises(PolicyDenied):
            artifacts.verify(self.project, key)
        self.assertEqual(artifacts.get(self.project, key)['state'], 'corrupt')
        path.write_bytes(original)
        artifacts.reconcile(self.project, key)
        path.unlink()
        with self.assertRaises(PolicyDenied):
            artifacts.verify(self.project, key)
        self.assertEqual(artifacts.get(self.project, key)['state'], 'missing')

    @unittest.skipUnless(os.name == 'nt', 'Real Windows junction boundary')
    def test_real_junction_rejected_before_export_and_external_files_preserved(self):
        artifacts, args, context = self.prepare()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside).resolve()
            marker = target / 'keep.txt'
            marker.write_text('outside-marker', 'utf-8')
            link = (self.store.root / 'agent-artifacts').absolute()
            self.assertTrue(link.parent.resolve().is_relative_to(self.store.root.resolve()))
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
            self.assertTrue(link.is_junction())
            try:
                with self.assertRaises(ValueError):
                    artifacts.export(args, context)
                self.assertFalse(self.store.rows('SELECT id FROM agent_artifacts'))
                self.assertEqual(list(target.iterdir()), [marker])
                self.assertEqual(marker.read_text('utf-8'), 'outside-marker')
            finally:
                # Remove only the directory entry created above, never its target.
                link.rmdir()
            self.assertTrue(marker.exists())

    def test_real_file_symlink_cannot_be_downloaded_or_deleted_outside_artifact(self):
        artifacts, args, context = self.prepare()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        folder = artifacts.directory(self.project, key)
        target = artifacts.verify(self.project, key)
        with tempfile.TemporaryDirectory() as outside:
            marker = Path(outside) / '保留原件.txt'
            original = target.read_bytes()
            marker.write_bytes(original)
            target.unlink()
            try:
                target.symlink_to(marker)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'OS denied symlink creation: {error}')
            with self.assertRaises(PolicyDenied):
                artifacts.lease(self.project, key)
            self.assertEqual(artifacts.get(self.project, key)['state'], 'corrupt')
            self.agent.transition(self.project, self.run['id'], 1, 'cancelled', event_key='symlink-stop')
            self.assertTrue(artifacts.delete(self.project, key))
            self.assertFalse(folder.exists())
            self.assertEqual(marker.read_bytes(), original)

    def test_export_and_download_under_long_chinese_workspace_path(self):
        original_store = self.store
        root = Path(self.temp.name) / ('中文档案' * 19) / ('财务归档' * 19)
        self.store = Store(root)
        self.agent = AgentStore(self.store)
        self.project = self.store.project('长路径项目')['id']
        self.other = self.store.project('另一个项目')['id']
        source = Path(self.temp.name) / '长路径源图.png'
        Image.new('RGB', (3, 3), 'white').save(source)
        self.photo = add_image(self.store, self.project, '一页.png', source)
        self.session = self.agent.create_session(self.project, {'client_request_id': 'long-s', 'title': '长路径'})
        self.run = self.agent.create_run(self.project, self.session['id'],
                                         {'client_request_id': 'long-r', 'content': '导出'}, context={}, config={}, limits={})
        self.policy = AgentPolicy(self.agent)
        try:
            artifacts, args, context = self.prepare()
            key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
            target = artifacts.verify(self.project, key)
            self.assertGreater(len(str(target)), 260)
            self.assertIn('42', target.read_text('utf-8-sig'))
            lease, leased_target = artifacts.lease(self.project, key)
            self.assertEqual(leased_target, target)
            artifacts.release(lease)
        finally:
            self.store = original_store

    def test_restart_legacy_export_cleanup_preserves_agent_artifact_and_staging_policy(self):
        artifacts, args, context = self.prepare()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        published = artifacts.verify(self.project, key)
        original = published.read_bytes()
        staging = published.parent.parent / (key + '.staging-' + uid())
        staging.mkdir()
        (staging / 'export.txt').write_text('unfinished', encoding='utf-8')
        legacy = self.store.root / 'exports' / 'export-old'
        legacy.mkdir(parents=True)
        (legacy / 'old.txt').write_text('obsolete', encoding='utf-8')
        bundle = Path(self.temp.name) / 'restart-bundle'
        (bundle / 'config').mkdir(parents=True)
        (bundle / 'config' / 'engines.json').write_text('{}', encoding='utf-8')

        first = create_app(bundle, self.store.root, 'fixture-token', start_queue=False, agent_enabled=False)
        self.assertFalse(legacy.exists())
        self.assertEqual(artifacts.verify(self.project, key).read_bytes(), original)
        self.assertTrue(staging.exists(), 'unfinished run owns its staging directory')
        self.assertNotIn(staging.relative_to(self.store.root).as_posix(),
                         [item['path'] for item in first.state.application_services.maintenance.orphans()['files']])

        self.agent.transition(self.project, self.run['id'], 1, 'cancelled', event_key='restart-stop')
        second = create_app(bundle, self.store.root, 'fixture-token', start_queue=False, agent_enabled=False)
        maintenance = second.state.application_services.maintenance
        relative = staging.relative_to(self.store.root).as_posix()
        self.assertIn(relative, [item['path'] for item in maintenance.orphans()['files']])
        self.assertEqual(artifacts.verify(self.project, key).read_bytes(), original)
        receipt = maintenance.quarantine_orphans([relative])
        self.assertFalse(staging.exists())
        self.assertEqual((Path(receipt['directory']) / '0/export.txt').read_text('utf-8'), 'unfinished')
        self.assertEqual(artifacts.verify(self.project, key).read_bytes(), original)


class ArtifactHttpOwnershipTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_agent_artifacts.ArtifactTests.setUp
    prepare = test_agent_artifacts.ArtifactTests.prepare

    async def test_authenticated_project_bound_snapshot_download_pin_and_delete(self):
        artifacts, args, context = self.prepare()
        key = artifacts.export(args, context)['artifact_refs'][0]['artifact_id']
        contents = artifacts.verify(self.project, key).read_bytes()
        self.agent.transition(self.project, self.run['id'], 1, 'cancelled', event_key='http-stop')
        bundle = Path(self.temp.name) / 'bundle'
        (bundle / 'config').mkdir(parents=True)
        (bundle / 'config' / 'engines.json').write_text('{}', encoding='utf-8')
        app = create_app(bundle, self.store.root, 'local-fixture-token', start_queue=False, agent_enabled=True)
        own = f'/api/projects/{self.project}/agent/artifacts/{key}'
        foreign = f'/api/projects/{self.other}/agent/artifacts/{key}'
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                self.assertEqual((await client.get(own)).status_code, 401)
                client.headers['Authorization'] = 'Bearer local-fixture-token'
                for url in (f'/api/agent/artifacts/{key}', f'/api/agent/artifacts/{key}/download'):
                    self.assertEqual((await client.get(url)).status_code, 404, url)
                for method, suffix, body in (('get', '', None), ('get', '/download', None),
                                             ('patch', '', {'pinned': True}), ('delete', '', None)):
                    with self.subTest(method=method, suffix=suffix):
                        response = await getattr(client, method)(foreign + suffix, **({'json': body} if body else {}))
                        self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual((await client.get(own)).json()['project_id'], self.project)
                self.assertEqual((await client.get(own + '/download')).content, contents)
                self.assertEqual((await client.patch(own, json={'pinned': True})).json()['pinned'], 1)
                self.assertEqual((await client.patch(own, json={'pinned': False})).json()['pinned'], 0)
                self.assertEqual((await client.delete(foreign)).status_code, 404)
                self.assertTrue((await client.delete(own)).json()['deleted'])
                self.assertEqual((await client.get(own + '/download')).status_code, 400)

    async def test_foreign_proposal_ids_cannot_decide_local_result_over_authenticated_api(self):
        _, args, _ = self.prepare()
        own_result = args.results[0].result_id
        task = self.store.enqueue(self.other, [self.other_photo['active_version']], ['glm'])[0]
        self.store.claim()
        self.store.complete(task, {'text': '另一项目', 'tables': [], 'blocks': [], 'engine': 'glm'})
        foreign_result = self.store.one('tasks', task)['result_id']
        structure_id, visual_id = uid(), uid()
        with self.store.transaction() as db:
            db.execute("""INSERT INTO structure_proposals(id,result_id,version_id,revision,scope,basis,payload,state,created,updated)
                VALUES(?,?,?,?,?,?,?,'pending',?,?)""", (structure_id, foreign_result, self.other_photo['active_version'],
                0, '[]', 'foreign-basis', json.dumps({'kind': 'replace_table'}), now(), now()))
            db.execute("""INSERT INTO multimodal_requests(task_id,result_id,request_id,payload_hash,snapshot,created)
                VALUES(?,?,?,?,?,?)""", (task, foreign_result, 'foreign-review', 'hash', '{}', now()))
            db.execute("""INSERT INTO multimodal_proposals(id,task_id,result_id,target_id,target,current_target,before_value,
                after_value,current_value,status,state,reason,evidence,basis_revision,revision,edited_sha256,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,'replace','pending',?,?,0,0,?,?,?)""", (visual_id, task, foreign_result,
                'foreign-target', '{}', '{}', 'old', 'new', 'old', 'reason', '{}', 'hash', now(), now()))
        bundle = Path(self.temp.name) / 'bundle'
        (bundle / 'config').mkdir(parents=True)
        (bundle / 'config' / 'engines.json').write_text('{}', encoding='utf-8')
        app = create_app(bundle, self.store.root, 'local-fixture-token', start_queue=False, agent_enabled=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver',
                                     headers={'Authorization': 'Bearer local-fixture-token'}) as client:
            structure = f'/api/results/{own_result}/structure'
            visual = f'/api/results/{own_result}/multimodal'
            self.assertEqual((await client.get(structure)).status_code, 200)
            self.assertEqual((await client.get(visual)).status_code, 200)
            self.assertEqual((await client.post(structure + f'/{structure_id}/decision', json={
                'request_id': 'foreign-structure', 'action': 'keep', 'revision': 0,
                'version_id': self.photo['active_version'], 'basis': 'foreign-basis'})).status_code, 404)
            self.assertEqual((await client.post(visual + f'/{visual_id}/decision', json={
                'request_id': 'foreign-visual', 'action': 'reject', 'revision': 0,
                'version_id': self.photo['active_version']})).status_code, 404)
        self.assertFalse(self.store.rows('SELECT * FROM structure_decisions WHERE proposal_id=?', (structure_id,)))
        self.assertFalse(self.store.rows('SELECT * FROM multimodal_decisions WHERE proposal_id=?', (visual_id,)))

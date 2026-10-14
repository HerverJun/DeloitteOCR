"""Real native worker calculations plus independent CSV/XLSX/lineage readback."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from workbench_platform.store import WorkspaceStore
from workbench_platform.assets.registry import AssetRegistry
from workbench_platform.assets.sets import AssetSetStore
from workbench_platform.artifacts.center import ArtifactCenter
from workbench_platform.data_api.service import DataWorkbenchService
from workbench_platform.application_services.data import stage_asset_upload
from workbench_platform.agent.registry import ToolRegistry, ToolContext
from workbench_platform.agent.store import AgentStore
from workbench_platform.agent.tools import data, context

RUN = Path('F:/OCR/output/platform-agent-dev-20261004')
PYTHON = Path('F:/OCR/output/clear-automation-dev-20261002/program/runtimes/data/python.exe')


class DataToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        (RUN / 'evidence/data').mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=RUN / 'evidence/data')
        root = Path(self.temporary.name)
        (root / 'blobs').mkdir()
        self.workspaces = WorkspaceStore(root / 'workspaces.sqlite3')
        self.workspaces.create_workspace('Synthetic Agent test', workspace_id='wtest')
        self.workspaces.create_workspace('Other workspace', workspace_id='wother')
        self.assets = AssetRegistry(root / 'assets.sqlite3', root / 'blobs', {'fixtures': RUN / 'fixtures'})
        self.sets = AssetSetStore(self.assets)
        self.native = DataWorkbenchService(root / 'data-workbench', PYTHON)
        self.center = ArtifactCenter(root / 'artifact-center', self.assets, self.sets, self.native)
        self.registry = ToolRegistry()
        data.register(self.registry)
        context.register(self.registry)
        self.store = AgentStore(root / 'agent')
        session = self.store.create_session('wtest', 'Test', [], 'session_request')
        run, _ = self.store.start_run(session['id'], 'test data tools', 'run_request', 1)
        self.services = SimpleNamespace(workspace_store=self.workspaces, asset_library=(self.assets, self.sets, None),
            data_workbench=self.native, artifact_center=self.center, agent=SimpleNamespace(registry=self.registry))
        self.ctx = ToolContext('wtest', session['id'], run['id'], 1, 0, '', (), self.services, self.store)
        self.counter = 0

    def tearDown(self):
        self.native.close()
        self.temporary.cleanup()

    def asset(self, name, workspace='wtest'):
        return self.assets.register(workspace, 'fixtures', name, media_type='text/csv' if name.endswith('.csv') else
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', storage_mode='managed')

    async def call(self, tool, arguments, *, settle=True):
        self.counter += 1
        call = {'call_id': 'call_' + str(self.counter), 'tool': tool, 'arguments': arguments}
        self.registry.validate_batch([call], self.registry.tools)
        result = await self.registry.dispatch(call, self.ctx)
        if settle and result['status'] == 'pending':
            for _ in range(200):
                value = await asyncio.to_thread(data.settle, result['job_refs'][0], self.ctx)
                if value is not None:
                    op = self.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?',
                                         (self.ctx.run_id, call['call_id']))[0]['id']
                    self.store.finish_operation(op, value)
                    return value
                await asyncio.sleep(0.1)
            self.fail('Native data worker timed out')
        return result

    async def create(self):
        result = await self.call('data_create_session', {'name': '供应商比对'})
        self.assertEqual(result['status'], 'success', result)
        return result['data']['data_session_id']

    async def import_asset(self, session, side, asset, **options):
        result = await self.call('data_import', {'data_session_id': session,
            'expected_version': self.native.get(session)['version'], 'side': side,
            'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash, 'options': options})
        self.assertEqual(result['status'], 'success', result)
        return result

    async def test_real_clean_compare_export_independent_values_and_native_provenance(self):
        session = await self.create()
        for side in ('left', 'right'):
            asset = self.asset('suppliers-' + side + '.csv')
            discovery = await self.call('data_inspect_input', {'asset_id': asset.asset_id,
                'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
            self.assertEqual(discovery['data']['sheets'][0]['columns'][0]['header'], '供应商编号')
            await self.import_asset(session, side, asset)
            for step in ({'operator': 'trim', 'column': 'c2'}, {'operator': 'cast', 'column': 'c3', 'target': 'decimal'}):
                result = await self.call('data_apply_step', {'data_session_id': session,
                    'expected_version': self.native.get(session)['version'], 'side': side, 'step': step})
                self.assertEqual(result['status'], 'success', result)
        left = await self.call('data_read_table', {'data_session_id': session, 'side': 'left'})
        self.assertEqual(left['data']['rows'][0]['cells'][0]['value'], '000001')
        self.assertEqual(left['data']['rows'][1]['cells'][0]['value'], '123456789012345678901234')
        self.assertEqual(left['data']['rows'][0]['cells'][4]['raw'], '=SUM(1,2)')
        compared = await self.call('data_compare', {'data_session_id': session,
            'expected_version': self.native.get(session)['version'],
            'spec': {'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c2', 'c2'], ['c3', 'c3']],
                     'amount_pairs': [['c3', 'c3']], 'amount_tolerance': '1'}})
        self.assertEqual(compared['status'], 'success', compared)
        result_id = compared['data']['result_id']
        result = await self.call('data_read_result', {'data_session_id': session, 'result_id': result_id})
        expected = json.loads((RUN / 'fixtures/expected.json').read_text(encoding='utf-8'))['C01']
        differences = sorted(row['L_c1_value'] for row in result['data']['rows'] if row['category'] == 'different')
        self.assertEqual(differences, expected['difference_keys'])
        self.assertEqual([row['L_c1_value'] for row in result['data']['rows'] if row['category'] == 'same'],
                         expected['equal_with_tolerance'])
        exported = await self.call('data_export', {'data_session_id': session,
            'expected_version': self.native.get(session)['version'], 'result_id': result_id, 'categories': ['different']})
        self.assertEqual(exported['status'], 'success', exported)
        bundle = exported['data']['bundle']
        preserved = RUN / 'evidence/data/readback-files'
        preserved.mkdir(exist_ok=True)
        for file in bundle['files']:
            artifact = self.center.file('wtest', bundle['bundle_id'], file['name'])
            with self.center.store.stage_download('wtest', artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                shutil.copyfile(path, preserved / file['name'])
        # A separate reader, without the processing engine, checks XLSX cells.
        reader = "import json,sys;from openpyxl import load_workbook;b=load_workbook(sys.argv[1],read_only=True,data_only=False);print(json.dumps(list(b.active.values),ensure_ascii=False));b.close()"
        workbook_rows = json.loads(subprocess.check_output([str(PYTHON), '-B', '-c', reader,
                                    str(preserved / 'results.xlsx')], text=True, encoding='utf-8'))
        key_column = workbook_rows[0].index('L_c1_value')
        self.assertEqual(sorted(row[key_column] for row in workbook_rows[1:]), expected['difference_keys'])
        artifact = self.center.file('wtest', bundle['bundle_id'], 'results.csv')
        with self.center.store.stage_download('wtest', artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            with path.open('r', encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(sorted(row['L_c1_value'] for row in rows), expected['difference_keys'])
            self.assertEqual([row['L_c3_value'] for row in rows], ['100.00', '50.00'])
        published = await self.call('delivery_publish', {'summary': '金额差异超过1元的项目已导出',
                                                        'artifact_refs': exported['artifact_refs']})
        self.assertEqual(published['status'], 'success', published)
        self.assertTrue(published['data']['verified'])
        self.assertEqual(len(published['data']['delivery'][0]['files']), 5)
        receipt = {'kind': 'deterministic-tools-real-workers', 'is_real_controller': False,
                   'difference_keys': differences, 'files': bundle['files'], 'tool_calls': self.counter}
        (RUN / 'evidence/data/real-worker-readback.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')

    async def test_multisheet_is_discovered_and_not_implicitly_imported(self):
        session = await self.create()
        asset = self.asset('suppliers-left-multisheet.xlsx')
        discovery = await self.call('data_inspect_input', {'asset_id': asset.asset_id,
                    'asset_revision': asset.revision, 'blob_hash': asset.blob_hash})
        self.assertEqual(discovery['status'], 'success', discovery)
        self.assertEqual([sheet['name'] for sheet in discovery['data']['sheets']], ['供应商台账', '历史台账'])
        ambiguous = await self.call('data_import', {'data_session_id': session, 'expected_version': 1,
            'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash, 'side': 'left'})
        self.assertEqual(ambiguous['error']['code'], 'ambiguous_sheet')
        self.assertEqual(self.native.get(session)['uploads'], [])
        selected = await self.import_asset(session, 'left', asset, sheet_name='供应商台账')
        self.assertEqual(selected['data']['tables']['left']['rows'], 4)

    async def test_native_upload_replay_version_and_cross_project_boundaries(self):
        session = await self.create()
        asset = self.asset('suppliers-left.csv')
        first = stage_asset_upload(self.native, self.assets, 'wtest', session, asset.asset_id, asset.revision,
                                    asset.blob_hash, 'fixed_request')
        second = stage_asset_upload(self.native, self.assets, 'wtest', session, asset.asset_id, asset.revision,
                                     asset.blob_hash, 'fixed_request')
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second['reused'])
        self.assertEqual(len(self.native.get(session)['uploads']), 1)
        await self.import_asset(session, 'left', asset)
        stale = await self.call('data_apply_step', {'data_session_id': session, 'expected_version': 1,
            'side': 'left', 'step': {'operator': 'trim', 'column': 'c2'}})
        self.assertEqual(stale['status'], 'error')
        other = self.asset('suppliers-right.csv', workspace='wother')
        denied = await self.call('data_inspect_input', {'asset_id': other.asset_id,
            'asset_revision': other.revision, 'blob_hash': other.blob_hash})
        self.assertEqual(denied['status'], 'error')
        self.assertEqual(self.native.get(session)['version'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Native queue/HTTP integration; mocked inference, real durable state and exports."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from ocr_workbench.adapter import Cancelled
from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches
from ocr_workbench.service import create_app
from ocr_workbench.store import Conflict, Store
from ocr_workbench.task_queue import TaskQueue


TEMP_ROOT = Path('F:/OCR/output/ocr-screen-benchmark-20261007/temp')
TABLE = {'rows': 2, 'columns': 2, 'cells': [
    {'row': r, 'column': c, 'row_span': 1, 'column_span': 1, 'text': text,
     'is_header': r == 0, 'polygon': [[1+20*c, 2+20*r], [19+20*c, 2+20*r],
                                    [19+20*c, 18+20*r], [1+20*c, 18+20*r]],
     'geometry_origin': 'direct_detection'}
    for r, row in enumerate([['编号', '金额'], ['0001', '12.50']]) for c, text in enumerate(row)]}


def prepare(source, folder, **kwargs):
    with Image.open(source) as image:
        image.save(Path(folder) / 'original.png')
        width, height = image.size
    return {'width': width, 'height': height, 'primary_id': 'original',
            'business_regions': [], 'quality': {'flags': [], 'unreadable': False},
            'prepared_images': [{'id': 'original', 'kind': 'original',
                'path': str(Path(folder) / 'original.png'), 'width': width, 'height': height,
                'prepared_to_source': [1, 0, 0, 0, 1, 0, 0, 0, 1]}]}


def select(candidates, info, **kwargs):
    selected = candidates[0]
    issues = deepcopy(info['quality']['flags'])
    return {'selected_id': selected['id'], 'selected_result': deepcopy(selected['raw']),
            'automatic_delivery': not issues, 'review_required': bool(issues),
            'selected_assessment': {'id': selected['id'], 'issues': issues},
            'assessments': [{'id': selected['id'], 'issues': issues}],
            'conflicts': [], 'modifications': [], 'upgrade_kind': None}


class ScreenQueueTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='screen-queue-', dir=TEMP_ROOT)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        (self.bundle / 'config').mkdir(parents=True)
        (self.bundle / 'config/engines.json').write_text(json.dumps({
            'glm': {'name': 'mock GLM', 'models': [], 'capabilities': {'tables': True}},
            'paddlevl': {'name': 'mock VL', 'models': [], 'capabilities': {'tables': True}}}), 'utf-8')
        self.store = Store(self.root / 'workspace')
        self.project = self.store.project('屏拍队列集成')['id']
        self.events, self.calls = [], []
        self.failure = None
        self.block_context = False
        self.entered = threading.Event()
        self.released = threading.Event()
        owner = self

        class Adapter:
            def __init__(self, bundle, engine, session_root):
                self.engine, self.bundle = engine, bundle
                self.geometry_provider, self.recognition_timeout = 'paddle', 900
                self.ready, self.process = False, None

            def configure_geometry(self, provider):
                self.geometry_provider = provider

            def load(self):
                self.ready = True
                owner.events.append(('load', self.engine, self.geometry_provider))

            def recognize(self, image, output, geometry_request=None):
                owner.calls.append({'engine': self.engine, 'provider': self.geometry_provider,
                    'request': deepcopy(geometry_request), 'timeout': self.recognition_timeout,
                    'image': str(image)})
                if self.engine == 'geometry' and owner.block_context:
                    owner.entered.set()
                    while not owner.released.wait(.01):
                        if self.cancelled.is_set():
                            raise Cancelled()
                if owner.failure == self.engine:
                    owner.failure = None
                    raise RuntimeError('simulated model failure')
                if self.engine == 'geometry':
                    with Image.open(image) as source:
                        width, height = source.size
                    raw = {'status': 'success', 'image': {'width': width, 'height': height},
                        'component': 'shared-geometry-context', 'image_version': geometry_request['image_version'],
                        'regions': [{'box': [0, 0, width, height], 'confidence': .95}],
                        'layout_blocks': [{'label': 'table', 'box': [0, 0, width, height], 'confidence': .95}],
                        'ocr_blocks': [{'text': '金额', 'confidence': .98, 'polygon': [[21,2],[39,2],[39,18],[21,18]]}],
                        'ocr_source': 'mock-independent-ppocr'}
                    (Path(output) / 'geometry.json').write_text(json.dumps(raw), 'utf-8')
                    return raw
                return {'status': 'success', 'engine': self.engine, 'text': '编号 金额\n0001 12.50',
                        'tables': [deepcopy(TABLE)], 'blocks': [], 'elapsed_seconds': .01}

            def unload(self):
                owner.events.append(('unload', self.engine, self.geometry_provider))
                self.ready = False

        self.queue = TaskQueue(self.store, self.bundle, Adapter)
        self.manager = ScreenBatches(self.store, self.queue, self.bundle, prepare=prepare, select=select)

    def tearDown(self):
        self.released.set()
        self.queue.stop()
        self.queue.unload()
        self.manager.stop()
        self.temp.cleanup()

    def images(self, count=1):
        versions = []
        for index in range(count):
            source = self.root / f'input-{index}.png'
            Image.new('RGB', (200, 100), (255, index, 255)).save(source)
            versions.append(add_image(self.store, self.project, f'图{index}.png', source)['active_version'])
        return versions

    def submit(self, versions, **policy):
        return self.manager.submit(self.project, {'version_ids': versions, 'request_id': 'screen-integration',
            'policy': {'primary_engine': 'glm', 'max_upgrades': 0, 'layout_evidence': True, **policy}})['batch_id']

    def drain(self):
        for _ in range(30):
            self.manager.step()
            while self.queue.step():
                pass
            if all(item['status'] in {'completed', 'needs_review', 'failed', 'cancelled'}
                   for item in self.store.rows('SELECT * FROM screen_items')):
                return
        self.fail('bounded integration drain did not finish')

    def test_resident_context_precedes_ocr_and_persists_actual_evidence(self):
        versions = self.images(2)
        batch = self.submit(versions)
        self.manager.step()
        self.assertTrue(self.queue.step())
        self.assertTrue(self.queue.step())
        self.assertEqual(self.events, [('load', 'geometry', 'context')])
        self.assertEqual({row['kind'] for row in self.store.rows('SELECT * FROM tasks')}, {'screen_context'})
        self.manager.step()
        self.assertTrue(self.queue.step())
        self.assertIn(('unload', 'geometry', 'context'), self.events)
        self.assertIn(('load', 'glm', 'paddle'), self.events)
        self.drain()
        detail = self.manager.detail(self.project, batch, versions[0])
        evidence = json.loads(self.store.rows('SELECT evidence FROM screen_items WHERE batch_id=? AND input_version_id=?', (batch, versions[0]))[0]['evidence'])
        self.assertEqual(evidence['provenance']['ocr_source'], 'mock-independent-ppocr')
        self.assertEqual(detail['status'], 'completed')
        self.assertFalse(detail['human_confirmed'])

    def test_failed_context_is_visible_and_never_counts_as_independent_success(self):
        version = self.images()[0]
        batch = self.submit([version])
        self.failure = 'geometry'
        self.drain()
        detail = self.manager.detail(self.project, batch, version)
        self.assertEqual(detail['status'], 'needs_review')
        self.assertIn('context_failed', {flag['code'] for flag in detail['preparation']['quality']['flags']})
        self.assertIn('context_failed', {issue['code'] for issue in detail['issues']})
        self.assertFalse(detail['human_confirmed'])
        evidence = json.loads(self.store.rows('SELECT evidence FROM screen_items WHERE batch_id=? AND input_version_id=?', (batch, version))[0]['evidence'])
        self.assertEqual(evidence['text'], [])
        self.assertIn(('unload', 'geometry', 'context'), self.events)

    def test_ocr_timeout_budget_includes_already_failed_and_context_tasks(self):
        version = self.images()[0]
        batch = self.submit([version], budget_seconds=15)
        self.manager.step()
        self.queue.step()
        context = self.store.rows('SELECT task_id FROM screen_contexts')[0]['task_id']
        end = datetime.now(timezone.utc)
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET started=?,finished=? WHERE id=?',
                       ((end-timedelta(seconds=6)).isoformat(), end.isoformat(), context))
        self.manager.step()
        task = self.store.rows('SELECT task_id FROM screen_tasks')[0]['task_id']
        # A failed prior pass is still spent time, not a successful-candidate timing.
        with self.store.transaction() as db:
            prior = 'failed-budget-evidence'
            row = self.store.one('tasks', task)
            db.execute('''INSERT INTO tasks(id,project_id,image_id,version_id,input_version_id,engine,batch,status,phase,created,started,finished)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (prior, self.project, row['image_id'], version, version,
                'glm', row['batch'], 'failed', 'failed', end.isoformat(),
                (end-timedelta(seconds=4)).isoformat(), end.isoformat()))
            screen = self.store.rows('SELECT * FROM screen_tasks WHERE task_id=?', (task,))[0]
            db.execute('INSERT INTO screen_tasks(task_id,batch_id,input_version_id,pass_index,prepared_id,prepared_path,metadata,attempt) VALUES(?,?,?,?,?,?,?,?)',
                (prior, batch, version, 9, 'prior', screen['prepared_path'], screen['metadata'], 0))
        self.queue.step()
        call = next(call for call in self.calls if call['engine'] == 'glm')
        self.assertGreaterEqual(call['timeout'], 1)
        self.assertLessEqual(call['timeout'], 5)

    def test_multiple_disjoint_crop_cells_map_once_to_original(self):
        def crops(source, folder, **kwargs):
            info = prepare(source, folder, **kwargs)
            for number, x in enumerate((10, 110)):
                target = Path(folder) / f'crop-{number}.png'
                with Image.open(source) as image:
                    image.crop((x, 20, x+80, 80)).save(target)
                info['prepared_images'].append({'id': f'crop-{number}', 'kind': 'table_crop', 'path': str(target),
                    'width': 80, 'height': 60, 'source_polygon': [[x,20],[x+80,20],[x+80,80],[x,80]],
                    'prepared_to_source': [1,0,x,0,1,20,0,0,1]})
            return info
        self.manager.prepare_override = crops
        version = self.images()[0]
        batch = self.submit([version], layout_evidence=False)
        self.drain()
        tables = self.manager.detail(self.project, batch, version)['result']['edited']['tables']
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0]['cells'][0]['polygon'][0], [11, 22])
        self.assertEqual(tables[1]['cells'][0]['polygon'][0], [111, 22])
        self.assertEqual(TABLE['cells'][0]['polygon'][0], [1, 2])

    def test_stop_interrupts_context_and_resume_reuses_identity(self):
        version = self.images()[0]
        batch = self.submit([version])
        self.block_context = True
        self.queue.start()
        self.manager.step()
        self.assertTrue(self.entered.wait(3))
        self.queue.stop()
        self.assertFalse(self.queue.thread.is_alive())
        self.assertIsNone(self.queue.adapter)
        context = self.store.rows('SELECT task_id FROM screen_contexts')[0]['task_id']
        self.assertEqual(self.store.one('tasks', context)['status'], 'interrupted')
        self.manager.step()
        self.manager.action(self.project, batch, {'action': 'resume'})
        self.queue.stopping.clear()
        self.block_context = False
        self.drain()
        self.assertEqual(len(self.store.rows('SELECT * FROM screen_contexts')), 1)
        self.assertEqual(self.manager.detail(self.project, batch, version)['status'], 'completed')

    def test_export_rejects_result_edit_during_workbook_generation(self):
        version = self.images()[0]
        batch = self.submit([version], layout_evidence=False)
        self.drain()
        detail = self.manager.detail(self.project, batch, version)
        from ocr_workbench.tables import export_xlsx
        def competing_edit(*args, **kwargs):
            export_xlsx(*args, **kwargs)
            edited = deepcopy(detail['result']['edited'])
            edited['tables'][0]['cells'][3]['text'] = '13.00'
            self.store.save(detail['result_id'], edited, detail['result']['revision'])
        with patch('ocr_workbench.tables.export_xlsx', side_effect=competing_edit):
            with self.assertRaisesRegex(Conflict, '导出期间'):
                self.manager.export(self.project, batch)

    def test_native_asgi_auth_origin_project_scope_and_binary_export(self):
        app = create_app(self.bundle, self.root / 'http-data', 'x'*32, start_queue=False)
        manager, store = app.state.screen_batches, app.state.store
        manager._hold_wal()
        try:
            project = store.project('HTTP')['id']
            other = store.project('other')['id']
            source = self.root / 'http-image.png'
            Image.new('RGB', (200, 100), 'white').save(source)
            version = add_image(store, project, 'http.png', source)['active_version']
            async def probe():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                    endpoint = f'/api/projects/{project}/screen-batches'
                    self.assertEqual((await client.get(endpoint)).status_code, 401)
                    client.headers['Authorization'] = 'Bearer ' + 'x'*32
                    self.assertEqual((await client.get(endpoint, headers={'Origin': 'https://other.invalid'})).status_code, 403)
                    created = await client.post(endpoint, json={'version_ids': [version], 'request_id': 'native-http',
                        'policy': {'primary_engine':'glm','layout_evidence':False,'max_upgrades':0}})
                    self.assertEqual(created.status_code, 200, created.text)
                    batch = created.json()['batch_id']
                    self.assertEqual((await client.get(f'/api/projects/{other}/screen-batches/{batch}')).status_code, 404)
                    wrong = await client.post(f'/api/projects/{other}/screen-batches', json={
                        'version_ids':[version], 'request_id':'cross-project'})
                    self.assertIn(wrong.status_code, {400,404,409})
                    manager.prepare_override, manager.select_override = prepare, select
                    manager.step()
                    while task := store.claim():
                        store.complete(task['id'], {'engine':'glm','text':'data','tables':[deepcopy(TABLE)],'blocks':[]})
                    manager.step()
                    response = await client.post(endpoint+'/'+batch+'/export', json={})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers['content-type'], 'application/zip')
                    self.assertTrue(response.content.startswith(b'PK'))
            asyncio.run(probe())
        finally:
            manager.stop()


if __name__ == '__main__':
    unittest.main()

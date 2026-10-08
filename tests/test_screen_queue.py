"""Native queue/HTTP integration; mocked inference, real durable state and exports."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import runpy
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from ocr_workbench.adapter import Cancelled
from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches, screen_seconds_spent
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
        self.block_load = False
        self.block_ocr = False
        self.ignore_cancel = False
        self.load_cost = 0
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
                if owner.block_load:
                    owner.entered.set()
                    while not owner.released.wait(.01):
                        if self.cancelled.is_set():
                            raise Cancelled()
                if owner.load_cost:
                    # Simulate elapsed cold loading without waiting or touching a GPU.
                    with owner.store.transaction() as db:
                        db.execute('UPDATE tasks SET started=? WHERE id=?',
                            ((datetime.now(timezone.utc)-timedelta(seconds=owner.load_cost)).isoformat(), owner.queue.current))

            def recognize(self, image, output, geometry_request=None):
                owner.calls.append({'engine': self.engine, 'provider': self.geometry_provider,
                    'request': deepcopy(geometry_request), 'timeout': self.recognition_timeout,
                    'image': str(image)})
                if self.engine == 'geometry' and owner.block_context or self.engine != 'geometry' and owner.block_ocr:
                    owner.entered.set()
                    while not owner.released.wait(.01):
                        if self.cancelled.is_set() and not owner.ignore_cancel:
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
        self.assertEqual(detail['status'], 'needs_review')
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

    def test_context_ipc_timeout_subtracts_cold_load_and_requests_full_layout(self):
        version = self.images()[0]
        self.submit([version], budget_seconds=20)
        self.load_cost = 6
        self.manager.step()
        self.queue.step()
        call = self.calls[0]
        self.assertEqual(call['engine'],'geometry')
        self.assertEqual(call['provider'],'context')
        self.assertLessEqual(call['timeout'],14)
        self.assertGreaterEqual(call['timeout'],1)
        self.assertEqual(call['request']['image_version'],version)
        self.assertIs(call['request']['include_all_layout'],True)
        self.assertIs(call['request']['allow_auxiliary_ocr'],True)

    def test_context_ipc_timeout_uses_shorter_batch_deadline(self):
        version = self.images()[0]
        batch = self.submit([version],budget_seconds=100,batch_budget_seconds=20)
        self.manager.step()
        with self.store.transaction() as db:
            db.execute('UPDATE screen_batches SET deadline=? WHERE id=?',(time.time()+4,batch))
        self.queue.step()
        self.assertGreaterEqual(self.calls[0]['timeout'],1)
        self.assertLessEqual(self.calls[0]['timeout'],4)

    def test_each_context_and_ocr_call_uses_frozen_request_timeout(self):
        version = self.images()[0]
        self.submit([version], budget_seconds=600, request_timeout_seconds=17)
        self.drain()
        self.assertEqual([call['engine'] for call in self.calls], ['geometry', 'glm'])
        self.assertEqual([call['timeout'] for call in self.calls], [17, 17])

    def test_new_batch_default_request_timeout_is_180(self):
        version = self.images()[0]
        self.submit([version], budget_seconds=600)
        self.drain()
        self.assertEqual([call['timeout'] for call in self.calls], [180, 180])

    def test_request_cap_does_not_reset_remaining_image_budget(self):
        version = self.images()[0]
        self.submit([version], budget_seconds=20, request_timeout_seconds=17)
        self.load_cost = 8
        self.manager.step()
        self.queue.step()
        self.assertGreaterEqual(self.calls[0]['timeout'], 1)
        self.assertLessEqual(self.calls[0]['timeout'], 12)

    def test_ordinary_ocr_keeps_900_after_a_capped_screen_call(self):
        version = self.images()[0]
        self.submit([version], layout_evidence=False, budget_seconds=600, request_timeout_seconds=17)
        self.manager.step()
        self.queue.step()
        self.store.enqueue(self.project, [version], ['glm'])
        self.queue.step()
        self.assertEqual([call['timeout'] for call in self.calls], [17, 900])

    def test_pre_cap_persisted_policy_uses_shared_budget_without_new_180_cap(self):
        version = self.images()[0]
        batch = self.submit([version], layout_evidence=False, budget_seconds=600)
        with self.store.transaction() as db:
            policy = json.loads(db.execute('SELECT policy FROM screen_batches WHERE id=?', (batch,)).fetchone()['policy'])
            policy.pop('request_timeout_seconds')
            db.execute('UPDATE screen_batches SET policy=? WHERE id=?', (json.dumps(policy), batch))
        self.manager.step()
        self.queue.step()
        self.assertGreater(self.calls[0]['timeout'], 180)
        self.assertLessEqual(self.calls[0]['timeout'], 600)
        metadata = json.loads(self.store.rows('SELECT metadata FROM screen_tasks')[0]['metadata'])
        self.assertEqual(metadata['request_timeout_seconds'], min(900, metadata['budget_reservation_seconds']))
        self.assertLessEqual(metadata['request_timeout_seconds'], 600)
        self.assertLessEqual(self.calls[0]['timeout'], metadata['request_timeout_seconds'])

    def test_expired_budget_after_context_cold_load_never_dispatches_inference(self):
        version = self.images()[0]
        self.submit([version],budget_seconds=10)
        self.load_cost = 11
        self.manager.step()
        self.queue.step()
        self.assertEqual(self.calls,[])
        task = self.store.rows("SELECT * FROM tasks WHERE kind='screen_context'")[0]
        self.assertEqual(task['status'],'failed')
        self.assertIsNone(self.queue.adapter)
        self.assertIn(('unload','geometry','context'),self.events)

    def test_batch_deadline_interrupts_blocked_context_load(self):
        version = self.images()[0]
        batch = self.submit([version],budget_seconds=20)
        self.manager.step()
        with self.store.transaction() as db:
            db.execute('UPDATE screen_batches SET deadline=? WHERE id=?',(time.time()+.25,batch))
        self.block_load = True
        started = time.monotonic()
        self.queue.step()
        self.assertTrue(self.entered.is_set())
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual(self.calls,[])
        task = self.store.rows("SELECT * FROM tasks WHERE kind='screen_context'")[0]
        self.assertEqual(task['status'],'failed')
        self.assertIn('预算',task['error'])
        self.assertIsNone(self.queue.adapter)

    def test_late_ocr_raw_after_budget_cannot_publish_a_result(self):
        version = self.images()[0]
        batch = self.submit([version],layout_evidence=False,budget_seconds=20)
        self.manager.step()
        with self.store.transaction() as db:
            db.execute('UPDATE screen_batches SET deadline=? WHERE id=?',(time.time()+1.5,batch))
        self.block_ocr, self.ignore_cancel = True, True
        release = threading.Timer(2,self.released.set)
        release.daemon = True
        release.start()
        try:
            self.queue.step()
        finally:
            release.cancel()
        self.assertTrue(self.entered.is_set())
        task = self.store.rows("SELECT * FROM tasks WHERE engine='glm'")[0]
        self.assertEqual(task['status'],'failed')
        self.assertIsNone(task['result_id'])
        self.assertEqual(self.store.rows('SELECT * FROM results'),[])
        self.assertIsNone(self.queue.adapter)

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
        self.assertEqual(self.manager.detail(self.project, batch, version)['status'], 'needs_review')

    def test_resume_keeps_prior_active_segments_and_counts_new_segment(self):
        for context in (False, True):
            with self.subTest(context=context):
                version = self.images()[0]
                # Unique idempotency key keeps this pair independent in one store.
                batch = self.manager.submit(self.project, {'version_ids':[version],
                    'request_id':f'budget-resume-{context}',
                    'policy':{'primary_engine':'glm','max_upgrades':0,
                              'layout_evidence':context,'budget_seconds':100}})['batch_id']
                self.manager.step()
                claimed = self.store.claim()
                self.assertIsNotNone(claimed)
                task_id = claimed['id']
                binding = 'screen_contexts' if context else 'screen_tasks'
                self.assertEqual(len(self.store.rows(f'SELECT * FROM {binding} WHERE task_id=?',(task_id,))),1)
                # Four active seconds followed by a long human pause. A resumed
                # claim must accumulate the former and exclude the latter.
                end = datetime.now(timezone.utc)-timedelta(seconds=60)
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='interrupted',started=?,finished=? WHERE id=?",
                        ((end-timedelta(seconds=4)).isoformat(),end.isoformat(),task_id))
                self.assertAlmostEqual(screen_seconds_spent(self.store,batch,version),4,delta=.3)
                self.queue.action(self.project,'resume',[task_id])
                self.assertEqual(self.store.one('tasks',task_id)['finished'],end.isoformat())
                self.assertEqual(self.store.claim()['id'],task_id)
                self.assertIsNone(self.store.one('tasks',task_id)['finished'])
                spent = self.store.rows(f'SELECT spent_seconds FROM {binding} WHERE task_id=?',(task_id,))[0]['spent_seconds']
                self.assertAlmostEqual(spent,4,delta=.3)
                second_end = datetime.now(timezone.utc)
                with self.store.transaction() as db:
                    db.execute('UPDATE tasks SET started=? WHERE id=?',
                        ((second_end-timedelta(seconds=3)).isoformat(),task_id))
                self.assertAlmostEqual(screen_seconds_spent(self.store,batch,version),7,delta=.3)
                remaining = self.queue._screen_budget_remaining(self.store.one('tasks',task_id))
                self.assertGreater(remaining,90)
                self.assertLessEqual(remaining,93.1)
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='interrupted',finished=? WHERE id=?",(second_end.isoformat(),task_id))
                self.queue.action(self.project,'resume',[task_id])
                self.assertEqual(self.store.claim()['id'],task_id)
                spent = self.store.rows(f'SELECT spent_seconds FROM {binding} WHERE task_id=?',(task_id,))[0]['spent_seconds']
                self.assertAlmostEqual(spent,7,delta=.3)
                with self.store.transaction() as db:
                    db.execute("UPDATE tasks SET status='cancelled',started=?,finished=? WHERE id=?",
                        ((datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat(),
                         datetime.now(timezone.utc).isoformat(),task_id))
                self.assertAlmostEqual(screen_seconds_spent(self.store,batch,version),9,delta=.3)

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
                self.manager.export(self.project, batch, {'include_pending_review': True})

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
                    blocked = await client.post(endpoint+'/'+batch+'/export', json={})
                    self.assertEqual(blocked.status_code, 400)
                    response = await client.post(endpoint+'/'+batch+'/export', json={'include_pending_review':True})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.headers['content-type'], 'application/zip')
                    self.assertTrue(response.content.startswith(b'PK'))
            asyncio.run(probe())
        finally:
            manager.stop()

    def test_experiment_routes_use_actual_observations_and_visual_proposals_only(self):
        driver = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/screen_experiments.py'))
        source = self.root / 'experiment.png'
        Image.new('RGB', (1000, 700), 'white').save(source)
        inputs = self.root / 'experiment-inputs.json'
        inputs.write_text(json.dumps([{'id':'sample','image':str(source),
            'truth':{'amount':'13.25'},'ideal_roi':[1,2,3,4]}]), 'utf-8')
        observed = []
        class ExperimentAdapter:
            def __init__(self, bundle, engine, root):
                self.engine, self.provider = engine, None
            def configure_geometry(self, provider):
                self.provider = self.geometry_provider = provider
            def load(self):
                pass
            def unload(self):
                observed.append(('unload',self.engine,self.provider))
            def recognize(self, image, output, geometry_request=None):
                observed.append(('request',self.engine,self.provider,deepcopy(geometry_request)))
                with Image.open(image) as pixels:
                    width,height = pixels.size
                blocks = [{'text':'12.50','polygon':[[30,30],[100,30],[100,60],[30,60]],'confidence':.98}]
                if self.provider == 'context':
                    return {'status':'success','image':{'width':width,'height':height},'coordinate_space':'exif_normalized_original',
                        'image_version':geometry_request['image_version'],'component':'mock-actual-context',
                        'layout_blocks':[{'label':'table','box':[10,10,width-10,height-10],'confidence':.99}],
                        'regions':[{'box':[10,10,width-10,height-10],'confidence':.99},
                                   {'box':[0,0,100,20],'confidence':.99}],
                        'ocr_blocks':blocks,'ocr_source':'mock-ppocr'}
                if self.engine == 'ppocr':
                    return {'status':'success','text':'12.50','blocks':blocks,'tables':[]}
                return {'status':'success','component':self.provider,'tables':[],
                        'candidate_tables':[],'coordinate_contract':'crop-pixels-to-image-affine-v1'}
        class Review:
            def __init__(self,*args):
                pass
            def __enter__(self):
                return self
            def __exit__(self,*args):
                pass
            def review(self,image,snapshot):
                self_snapshot = deepcopy(snapshot)
                observed.append(('visual',self_snapshot))
                return {'items':[{'target_id':target['id'],'decision':'replace','after':'13.25','reason':'mock observed reading'}
                                 for target in snapshot['targets']]}
        args = SimpleNamespace(bundle=self.bundle,inputs=inputs,output=self.root/'experiment-run',
            geometry_source_root=None,mode='all',providers='rapidtable,tableformer-raw,paddle',limit=1,
            max_tiles=4,max_targets=4,tile_size=768,budget_seconds=30,task_timeout=10,dry_run=False,
            context_run=None,context_verifier=None,context_normalizer=None)
        with patch.dict(os.environ), patch('ocr_workbench.multimodal_runtime.load_config',return_value={}):
            # runpy imports are bound in the function's global namespace.
            driver['run'].__globals__['load_config'] = lambda *args: {}
            receipt = driver['run'](args,adapter_factory=ExperimentAdapter,review_factory=Review)
        self.assertTrue(all(row['source_unchanged'] for row in receipt['rows']))
        self.assertFalse(any(stage['status']=='failed' for stage in receipt['stages']))
        self.assertIn('tile-ppocr',{stage['stage'] for stage in receipt['stages']})
        for call in observed:
            if call[0]=='request' and call[3] is not None:
                self.assertFalse({'truth','ideal_roi','reference'} & set(call[3]))
                if call[2] in {'rapidtable','tableformer-raw','paddle'}:
                    self.assertEqual(call[3]['regions_source'],'actual_prepared_business_regions')
                    self.assertEqual(len(call[3]['regions']),1)
                    self.assertEqual(call[3]['regions'][0]['id'],'region-1')
        visual = next(call[1] for call in observed if call[0]=='visual')
        self.assertTrue(all(target['before']=='' for target in visual['targets']))
        proposals = json.loads((args.output/'image-01/visual/proposals-only.json').read_text('utf-8'))
        self.assertEqual(proposals['proposals'][0]['before'],'12.50')
        self.assertFalse(proposals['proposals'][0]['automatic_apply'])
        self.assertEqual(proposals['proposals'][0]['human_review'],'not_performed')

    def test_experiment_saved_candidate_rejects_tampering_and_stale_source_binding(self):
        driver = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/screen_experiments.py'))
        source = self.root/'candidate-input.png'
        Image.new('RGB',(200,100),'white').save(source)
        candidate = self.root/'candidate.json'
        candidate.write_text(json.dumps({'text':'observed','tables':[deepcopy(TABLE)]}),'utf-8')
        digest = driver['digest']
        record = self.root/'source-record.json'
        source_data = {'id':'sample','status':'success','image_sha256':digest(source),'result_sha256':digest(candidate)}
        record.write_text(json.dumps(source_data),'utf-8')
        item = {'id':'sample','image':str(source),'image_sha256':digest(source),
                'result':str(candidate),'result_sha256':digest(candidate),'result_image_sha256':digest(source),
                'source_record':str(record),'source_record_sha256':digest(record),
                'truth':{'amount':'99'},'ideal_roi':[1,2,3,4]}
        manifest = self.root/'candidate-manifest.json'
        def save():
            manifest.write_text(json.dumps({'items':[item]}),'utf-8')
        save()
        loaded = driver['load_inputs'](manifest,1)[0]
        self.assertFalse({'truth','ideal_roi'} & set(loaded))
        self.assertEqual(loaded['source_record_sha256'],digest(record))
        self.assertEqual(driver['actual_candidate'](loaded['result'],loaded['image_sha256'],loaded['result_image_sha256'])['text'],'observed')
        candidate.write_text(json.dumps({'text':'changed','tables':[deepcopy(TABLE)]}),'utf-8')
        with self.assertRaisesRegex(ValueError,'候选.*hash'):
            driver['load_inputs'](manifest,1)
        # Updating one digest cannot silently relabel the old model record.
        item['result_sha256'] = digest(candidate)
        save()
        with self.assertRaisesRegex(ValueError,'未绑定'):
            driver['load_inputs'](manifest,1)
        source_data['result_sha256'] = digest(candidate)
        source_data['image_sha256'] = 'stale-image-hash'
        record.write_text(json.dumps(source_data),'utf-8')
        item['source_record_sha256'] = digest(record)
        save()
        with self.assertRaisesRegex(ValueError,'未绑定'):
            driver['load_inputs'](manifest,1)


if __name__ == '__main__':
    unittest.main()

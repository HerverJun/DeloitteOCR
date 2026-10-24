"""Bounded optional routing with real durable stores and synthetic observations.

These CPU fixtures establish scheduling/identity contracts, not OCR accuracy.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import unittest
from unittest.mock import patch

from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches, validate_policy
from ocr_workbench.screen_engine_route import decide_engine_route, frozen_engine_route
from ocr_workbench.screen_evidence import normalize_screen_context
from ocr_workbench.store import Conflict, encoded


POLICY = dict(primary_engine='glm', fallback_engines=['hunyuan'], max_upgrades=1,
    mode='accuracy', upgrade_trigger='failure_only', layout_evidence=True,
    require_human_review=True, budget_seconds=360, batch_budget_seconds=3600,
    request_timeout_seconds=180, engine_route='business_language_v1',
    technical_fallback_reserve_seconds=90)
LATIN = 'abcdefghijklmnopqrstuvwxyz'
CJK = '营业收入成本利润项目部门科目金额年度单位合计说明' * 2


class EngineRouteTests(unittest.TestCase):
    def setUp(self):
        module = runpy.run_path(str(Path(__file__).with_name('test_screen_batches.py')))
        self.fixture = module['ScreenBatchTests']('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manager, self.store = self.fixture.manager, self.fixture.store
        self.project, self.root = self.fixture.project, self.fixture.root
        self.prepare = module['prepare']
        self.manager.prepare_override = self.bound_prepare

    def bound_prepare(self, source, folder, **kwargs):
        info = self.prepare(source, folder, **kwargs)
        info.update(input_path=str(source), input_sha256=hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            business_regions=[{'table_box': [0, 10, 100, 70]}])
        return info

    def raw_context(self, version, text=LATIN):
        item = next(row for row in self.manager._items(self.batch) if row['input_version_id'] == version)
        return {'status': 'success', 'component': 'synthetic-route-fixture',
            'image_version': version, 'image': {'width': 100, 'height': 70},
            'screen_context_input': {'image_version': version, 'source_sha256': item['source_hash'],
                'coordinate_space': 'exif_normalized_original'},
            'layout_blocks': [{'label': 'table', 'box': [0, 10, 100, 70]}],
            'ocr_blocks': [{'text': text, 'box': [1, 20, 99, 40], 'source': 'PP-OCRv6-auxiliary'}]}

    def submit(self, versions=None, **policy):
        self.versions = versions or self.fixture.images(1)
        self.batch = self.manager.submit(self.project, {'version_ids': self.versions,
            'request_id': 'route-request-001', 'policy': {**POLICY, **policy}})['batch_id']
        return self.batch

    def contexts(self, texts=None, cost=20, failed=False):
        texts = texts or [LATIN] * len(self.versions)
        self.manager.step()
        for version, text in zip(self.versions, texts):
            row = self.store.rows('SELECT task_id FROM screen_contexts WHERE batch_id=? AND input_version_id=?',
                (self.batch, version))[0]
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET status=?,started=NULL,finished=NULL,error=? WHERE id=?',
                    ('failed' if failed else 'succeeded', 'synthetic context failure' if failed else None, row['task_id']))
                db.execute('UPDATE screen_contexts SET raw=?,spent_seconds=? WHERE task_id=?',
                    (encoded(self.raw_context(version, text)), cost, row['task_id']))
        self.manager.step()

    def tasks(self, version=None):
        return self.store.rows('''SELECT t.*,s.pass_index,s.metadata FROM tasks t JOIN screen_tasks s ON s.task_id=t.id
            WHERE s.batch_id=? ORDER BY t.batch,t.created,t.id''', (self.batch,)) if version is None else [
            row for row in self.tasks() if row['input_version_id'] == version]

    def fail_task(self, task, cost):
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='failed',started=NULL,finished=NULL,error='synthetic technical failure' WHERE id=?", (task['id'],))
            db.execute('UPDATE screen_tasks SET spent_seconds=? WHERE task_id=?', (cost, task['id']))

    def decision_inputs(self):
        self.submit()
        version = self.versions[0]
        evidence = normalize_screen_context(self.raw_context(version))
        source = self.store.file(self.store.one('versions', version)['path'])
        info = {'input_path': str(source), 'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'business_regions': [{'table_box': [0, 10, 100, 70]}]}
        return info, evidence, info['input_sha256'], version

    def test_source_language_and_unverified_evidence_abstention(self):
        info, evidence, digest, version = self.decision_inputs()
        self.assertEqual(decide_engine_route(info, evidence, digest, version)['engines'], ['glm', 'hunyuan'])
        for text, expected in [(CJK, 'cjk_business_region'), ('12345', 'source_language_unverified_or_undecided')]:
            evidence['text'][0]['text'] = text
            route = decide_engine_route(info, evidence, digest, version)
            self.assertEqual(route['engines'], ['hunyuan'])
            self.assertEqual(route['reason'], expected)
        for key, value in [('source_sha256', '0' * 64), ('image_version', 'another-version'),
                ('input_image_version', 'another-version'), ('contains_reference_input', True),
                ('coordinate_space', 'prepared'), ('source_candidate_id', 'crop')]:
            trial = normalize_screen_context(self.raw_context(version))
            trial['provenance'][key] = value
            with self.subTest(key=key):
                self.assertEqual(decide_engine_route(info, trial, digest, version)['engines'], ['hunyuan'])
        self.assertEqual(decide_engine_route(info, {}, digest, version)['engines'], ['hunyuan'])

    def test_ui_outside_regions_and_non_pp_text_never_select_glm(self):
        info, evidence, digest, version = self.decision_inputs()
        for changes in ({'kind': 'toolbar'}, {'box': [0, 0, 100, 5]}, {'source': 'glm'}):
            trial = deepcopy(evidence)
            trial['text'][0].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(decide_engine_route(info, trial, digest, version)['engines'], ['hunyuan'])

    def test_frozen_binding_rejects_source_region_evidence_and_decision_changes(self):
        info, evidence, digest, version = self.decision_inputs()
        info['engine_route_snapshot'] = decide_engine_route(info, evidence, digest, version)
        for mutation in ('region', 'text', 'version', 'snapshot', 'source'):
            trial, observed = deepcopy(info), deepcopy(evidence)
            current_version = version
            if mutation == 'region': trial['business_regions'][0]['table_box'][1] = 11
            if mutation == 'text': observed['text'][0]['text'] = CJK
            if mutation == 'version': current_version = 'changed-version'
            if mutation == 'snapshot': trial['engine_route_snapshot']['engines'] = ['hunyuan']
            if mutation == 'source': Path(trial['input_path']).write_bytes(b'changed source bytes')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                frozen_engine_route(trial, observed, digest, current_version)

    def test_filename_candidate_and_prior_score_do_not_influence_decision(self):
        info, evidence, digest, version = self.decision_inputs()
        before = decide_engine_route(info, evidence, digest, version)
        copy = self.root / 'chinese-中文-truth-glm-worst.png'
        shutil.copyfile(info['input_path'], copy)
        info.update(input_path=str(copy), candidates=[{'text': CJK}], prior_score=0, sample_id='different')
        self.assertEqual(decide_engine_route(info, evidence, digest, version), before)

    def test_route_policy_strictness_and_root_admission(self):
        self.assertEqual(validate_policy({})['engine_route'], 'fixed')
        self.assertEqual(validate_policy(POLICY)['technical_fallback_reserve_seconds'], 90)
        for key, value in [('engine_route', []), ('engine_route', {}),
                ('technical_fallback_reserve_seconds', True), ('technical_fallback_reserve_seconds', '90'),
                ('technical_fallback_reserve_seconds', 0), ('technical_fallback_reserve_seconds', 360),
                ('upgrade_trigger', 'quality'), ('require_human_review', False), ('layout_evidence', False)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_policy({**POLICY, key: value})
        versions = self.fixture.images(2)
        with self.assertRaises(ValueError): self.submit(versions, batch_budget_seconds=719)
        self.assertEqual(self.store.rows('SELECT id FROM screen_batches'), [])

    def test_mixed_languages_run_h_first_with_distinct_reserves(self):
        self.submit(self.fixture.images(3))
        self.contexts([LATIN, CJK, '12345'])
        tasks = self.tasks()
        self.assertEqual([task['engine'] for task in tasks], ['hunyuan', 'hunyuan', 'glm'])
        self.assertEqual(self.store.claim()['engine'], 'hunyuan')
        for task in tasks:
            metadata = json.loads(task['metadata'])
            reserve = 90 if task['engine'] == 'glm' else 0
            self.assertEqual(metadata['technical_fallback_reserved_seconds'], reserve)
            self.assertGreater(metadata['timeout_seconds'], 338 - reserve)
            self.assertLessEqual(metadata['timeout_seconds'], 340 - reserve)
            self.assertEqual(metadata['engine_route_snapshot']['engines'][0], task['engine'])

    def test_glm_technical_failure_runs_one_h_with_original_remaining_budget(self):
        self.submit(); self.contexts()
        self.fail_task(self.tasks()[0], 249)
        self.manager.step()
        tasks = self.tasks()
        self.assertEqual([task['engine'] for task in tasks], ['glm', 'hunyuan'])
        metadata = json.loads(tasks[1]['metadata'])
        self.assertEqual(metadata['technical_fallback_reserved_seconds'], 0)
        self.assertGreater(metadata['timeout_seconds'], 89)
        self.assertLessEqual(metadata['timeout_seconds'], 91)
        self.fail_task(tasks[1], 89)
        self.manager.step(); self.manager.step()
        self.assertEqual(len(self.tasks()), 2)
        self.assertEqual(self.manager._items(self.batch)[0]['status'], 'failed')

    def test_success_never_triggers_quality_upgrade_or_removes_human_review(self):
        self.submit(); self.contexts()
        self.fixture.finish(); self.manager.step(); self.manager.step()
        self.assertEqual(len(self.tasks()), 1)
        self.assertEqual(self.manager._items(self.batch)[0]['status'], 'needs_review')

    def test_h_only_failure_does_not_invoke_glm(self):
        self.submit(); self.contexts([CJK])
        self.fail_task(self.tasks()[0], 10); self.manager.step()
        self.assertEqual(len(self.tasks()), 1)
        self.assertEqual(self.manager._items(self.batch)[0]['status'], 'failed')

    def test_context_failure_uses_h_without_fake_success(self):
        self.submit(); self.contexts(failed=True)
        self.assertEqual([task['engine'] for task in self.tasks()], ['hunyuan'])
        item = self.manager._items(self.batch)[0]
        self.assertEqual(json.loads(item['evidence'])['warnings'][0]['code'], 'context_failed')
        self.assertEqual(self.store.rows("SELECT status FROM tasks WHERE engine='geometry'")[0]['status'], 'failed')

    def test_context_exhausts_primary_envelope_and_h_uses_reserved_remainder(self):
        self.submit(); self.contexts(cost=270)
        self.assertEqual([task['engine'] for task in self.tasks()], ['hunyuan'])
        task = self.tasks()[0]
        self.assertEqual(task['pass_index'], 1)
        skipped = json.loads(self.manager._items(self.batch)[0]['preparation'])['engine_route_primary_skip']
        self.assertFalse(skipped['model_invoked'])
        self.assertLessEqual(json.loads(task['metadata'])['timeout_seconds'], 90)
        self.assertGreater(json.loads(task['metadata'])['timeout_seconds'], 88)

    def test_no_context_envelope_is_explicit_and_keeps_the_remaining_h_budget(self):
        self.submit()
        budget = {'remaining_seconds': 89.5, 'potential_remaining_seconds': 89.5}
        with patch.object(self.manager, '_execution_budget', return_value=budget):
            self.manager.step()
        self.assertEqual(self.store.rows('SELECT * FROM screen_contexts'), [])
        item = self.manager._items(self.batch)[0]
        self.assertEqual(json.loads(item['evidence'])['warnings'][0]['code'], 'context_budget_reserved')
        self.assertEqual([task['engine'] for task in self.tasks()], ['hunyuan'])
        self.assertEqual(json.loads(self.tasks()[0]['metadata'])['timeout_seconds'], 89)

    def test_actual_queue_routes_mixed_inputs_and_one_technical_fallback_in_order(self):
        module = runpy.run_path(str(Path(__file__).with_name('test_screen_queue.py')))
        fixture = module['ScreenQueueTests']('runTest'); fixture.setUp()
        try:
            versions = fixture.images(2)
            original_factory = fixture.queue.factory
            def factory(*args):
                adapter = original_factory(*args)
                original_recognize = adapter.recognize
                def recognize(image, output, request=None):
                    raw = original_recognize(image, output, request)
                    if adapter.engine == 'geometry':
                        raw['ocr_blocks'] = [{'text': LATIN if request['image_version'] == versions[0] else CJK,
                            'box': [10, 20, 90, 40], 'source': 'PP-OCR-synthetic-queue'}]
                    return raw
                adapter.recognize = recognize
                return adapter
            fixture.queue.factory = factory
            fixture.manager.prepare_override = self.bound_prepare
            fixture.failure = 'glm'
            batch = fixture.submit(versions, **POLICY)
            fixture.drain()
            self.assertEqual([call['engine'] for call in fixture.calls],
                ['geometry', 'geometry', 'hunyuan', 'glm', 'hunyuan'])
            self.assertEqual(fixture.manager.get(fixture.project, batch)['counts']['needs_review'], 2)
            self.assertEqual(fixture.store.rows("SELECT count(*) n FROM tasks WHERE status='failed'")[0]['n'], 1)
            self.assertEqual(fixture.events.count(('load', 'glm', 'paddle')), 1)
        finally:
            fixture.tearDown()

    def test_cached_primary_has_snapshot_and_promoted_waiter_keeps_reserve(self):
        versions = self.fixture.images(1)
        source = self.store.file(self.store.one('versions', versions[0])['path'])
        copied = self.root / 'duplicate.png'
        shutil.copyfile(source, copied)
        versions.append(add_image(self.store, self.project, 'duplicate.png', copied)['active_version'])
        self.submit(versions); self.contexts(cost=0)
        tasks = self.tasks()
        cached = next(task for task in tasks if task['kind'] == 'screen_cached')
        computing = next(task for task in tasks if task['kind'] != 'screen_cached')
        self.assertIn('engine_route_snapshot', json.loads(cached['metadata']))
        self.fail_task(computing, 10)
        self.manager.step()
        promoted = next(task for task in self.tasks() if task['id'] == cached['id'])
        self.assertEqual(promoted['kind'], 'ocr')
        self.assertLessEqual(json.loads(promoted['metadata'])['timeout_seconds'], 260)
        self.assertEqual(json.loads(promoted['metadata'])['technical_fallback_reserved_seconds'], 90)

    def test_recovery_uses_frozen_route_and_rejects_changed_evidence(self):
        self.submit(); self.contexts()
        count = len(self.tasks())
        restarted = ScreenBatches(self.store, self.fixture.queue, self.root,
            prepare=self.bound_prepare, select=self.manager.select_override)
        self.addCleanup(restarted.stop)
        restarted.defaults = lambda: {'primary_engine': 'paddlevl'}
        restarted.step()
        self.assertEqual(len(self.tasks()), count)
        item = restarted._items(self.batch)[0]
        evidence = json.loads(item['evidence']); evidence['text'][0]['text'] = CJK
        restarted._update(self.batch, self.versions[0], evidence=encoded(evidence))
        restarted.step()
        self.assertEqual(restarted._items(self.batch)[0]['stage'], 'route_binding_failed')
        self.assertEqual(self.tasks()[0]['status'], 'cancelled')
        self.assertEqual(len(self.tasks()), count)

    def test_stale_enqueue_engine_is_rejected(self):
        self.submit(); self.contexts()
        item = self.manager._items(self.batch)[0]
        with self.assertRaises(ValueError):
            self.manager._enqueue(self.manager._batch(self.project, self.batch), [item], 'hunyuan', 0)

    def test_idempotent_submit_preserves_the_original_route(self):
        self.submit()
        body = {'version_ids': self.versions, 'request_id': 'route-request-001', 'policy': POLICY}
        self.assertEqual(self.manager.submit(self.project, body)['batch_id'], self.batch)
        with self.assertRaises(Conflict):
            self.manager.submit(self.project, {**body, 'policy': {**POLICY, 'technical_fallback_reserve_seconds': 80}})
        self.assertEqual(len(self.store.rows('SELECT id FROM screen_batches')), 1)

    def test_explicit_precision_child_resets_optional_route_without_touching_parent(self):
        # Reuse only fixture setup; no inherited test methods are collected.
        module = runpy.run_path(str(Path(__file__).with_name('test_screen_resolution.py')))
        fixture = module['ResolutionTests']('runTest'); fixture.setUp()
        try:
            with fixture.store.transaction() as db:
                batch = fixture.manager._batch(fixture.project, fixture.batch)
                policy = {**batch['policy'], **POLICY}
                db.execute('UPDATE screen_batches SET policy=? WHERE id=?', (encoded(policy), fixture.batch))
            receipt = fixture.resolve(fixture.body())
            self.assertEqual(receipt['status'], 'submitted')
            child = fixture.manager._batch(fixture.project, receipt['batch_id'])
            self.assertEqual(child['policy']['engine_route'], 'fixed')
            self.assertEqual(child['policy']['technical_fallback_reserve_seconds'], 0)
            self.assertEqual(fixture.manager._batch(fixture.project, fixture.batch)['policy'], policy)
        finally:
            fixture.tearDown()

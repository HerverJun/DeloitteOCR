"""Real SQLite/queue/HTTP local candidates; the adapter is CPU-only evidence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from ocr_workbench.adapter import Cancelled, EngineLoadTimeout
from ocr_workbench.atomic_files import write_json
from ocr_workbench.imaging import add_image
from ocr_workbench.screen_batches import ScreenBatches
from ocr_workbench.screen_load_guard import cold_load_state, record_cold_load
from ocr_workbench.screen_local_candidates import (complete_local_candidate,
    control_local_candidates, local_candidate_file, read_local_candidates,
    submit_local_candidates, validate_submission)
from ocr_workbench.screen_resolution import remaining_screen_budget, task_seconds_spent
from ocr_workbench.screen_routes import register_screen_routes
from ocr_workbench.store import Conflict, SCHEMA_VERSION, Store, encoded, uid
from ocr_workbench.task_queue import TaskQueue


class CpuAdapter:
    def __init__(self, bundle, engine, root):
        self.bundle, self.engine, self.ready = bundle, engine, False
        self.process = None
        self.recognition_timeout = 900
        self.calls = []
        self.before_return = None

    def load(self):
        self.ready = True

    def recognize(self, *args, **kwargs):
        raise AssertionError('A local candidate must not use page recognition')

    def recognize_local_text(self, path, output, *, allow_empty=False):
        assert allow_empty is True
        self.calls.append((path, output))
        with Image.open(path) as opened:
            opened.convert('RGB').save(output / 'input.png')
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        prepared_hash = hashlib.sha256((output / 'input.png').read_bytes()).hexdigest()
        with Image.open(path) as opened:
            size = list(opened.size)
        raw = {'recognition_mode': 'local_text', 'layout_applied': False,
            'formatter_applied': False, 'truncated': False, 'input_image_sha256': prepared_hash,
            'input_image_size': size, 'decoder_text': '1'}
        data = {'status': 'success', 'recognition_mode': 'local_text', 'text': '1',
            'tables': [], 'blocks': [], 'content_status': 'decoded_text', 'raw': raw,
            'image': {'sha256': source_hash, 'version': prepared_hash, 'width': size[0], 'height': size[1]}}
        write_json(output / 'raw.json', raw)
        write_json(output / 'result.json', data)
        (output / 'result.txt').write_text('1', encoding='utf-8')
        if self.before_return:
            self.before_return(data, path, output)
        return data

    def unload(self):
        self.ready = False


@pytest.fixture
def candidate_fixture(tmp_path):
    assert tmp_path.resolve().drive.upper() == 'F:'
    store = Store(tmp_path / 'workspace')
    project = store.project('局部候选 CPU 契约')['id']
    queue = TaskQueue(store, tmp_path, CpuAdapter)
    manager = ScreenBatches(store, queue, tmp_path)
    versions, fixtures = [], {}
    xs, ys = [100, 185, 310, 470], [200, 242, 305, 390]
    for index in range(2):
        path = tmp_path / ('grid-%s.png' % index)
        image = Image.new('RGB', (600, 500), 'white')
        image.putpixel((0, 0), (240 + index, 240, 240))
        draw = ImageDraw.Draw(image)
        for y in ys:
            draw.line((xs[0], y, xs[-1], y), fill='black', width=2)
        for x in xs:
            draw.line((x, ys[0], x, ys[-1]), fill='black', width=2)
        image.save(path)
        version = add_image(store, project, path.name, path)['active_version']
        versions.append(version)
        source = store.file(store.one('versions', version)['path'])
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        cells = [{'row': row, 'column': column, 'row_span': 1, 'column_span': 1,
            'text': 'item-%s-%s' % (row, column)} for row in range(3) for column in range(3)]
        evidence = {'text': [{'text': cell['text'], 'box': [xs[cell['column']] + 8,
            ys[cell['row']] + 8, min(xs[cell['column'] + 1] - 8, xs[cell['column']] + 65),
            ys[cell['row']] + 27], 'confidence': .99, 'source': 'PP-OCRv6-auxiliary',
            'kind': 'text'} for cell in cells],
            'provenance': {'contains_reference_input': False, 'image_version': version}}
        cells[4]['text'], cells[8]['text'] = '10', '20'
        evidence['text'][4]['text'], evidence['text'][8]['text'] = '1', '2'
        raw = {'status': 'success', 'text': 'CPU parent', 'engine': 'hunyuan',
            'project_image_version': version, 'tables': [{'rows': 3, 'columns': 3, 'cells': cells}],
            'screen': {'input_sha256': source_hash}}
        prep = {'input_path': str(source), 'input_sha256': source_hash, 'width': 600, 'height': 500,
            'preparation_seconds': 20, 'quality': {'flags': []},
            'business_regions': [{'id': 'grid', 'table_box': [100, 200, 470, 390],
                'sources': ['pixel_grid'], 'grid_reliable': True, 'observed_grid_rows': 3,
                'observed_grid_columns': 3, 'observed_grid': {'horizontal_lines': ys,
                    'vertical_lines': xs, 'source': 'pixel_grid', 'range_semantics': 'observed_line_positions',
                    'coordinate_space': 'exif_normalized_original'}}]}
        fixtures[version] = raw, prep, evidence
    capability = patch('ocr_workbench.screen_resolution.precision_capability',
        return_value={'available': True, 'engine': 'glm', 'package_id': 'builtin', 'basis': 'CPU fixture only'})
    capability.start()
    batch = manager.submit(project, {'version_ids': versions, 'request_id': 'source-local-batch-001',
        'policy': {'primary_engine': 'hunyuan', 'fallback_engines': [], 'max_upgrades': 0,
            'require_human_review': True, 'budget_seconds': 360, 'batch_budget_seconds': 1000}})['batch_id']
    for item in manager._items(batch):
        raw, prep, evidence = fixtures[item['input_version_id']]
        result = manager._final_result(manager._batch(project, batch), item, raw)
        manager._update(batch, item['input_version_id'], status='needs_review', result_id=result,
            result_revision=0, preparation=encoded(prep), evidence=encoded(evidence),
            candidates=encoded([{'id': 'cpu-' + item['input_version_id'], 'engine': 'hunyuan', 'raw': raw}]))
    try:
        yield store, project, batch, versions, queue, manager
    finally:
        queue.unload()
        manager.stop()
        capability.stop()


def body(fixture, *, index=0, request='local-request-001', budget=120, all_cells=False):
    store, project, batch, versions, queue, manager = fixture
    detail = manager.detail(project, batch, versions[index])
    localized = [item for item in detail['diagnostics']['items']
        if item['location']['level'] == 'cell' and item['location']['row'] in (1, 2)]
    assert len(localized) >= 2, detail['diagnostics']
    assert detail['diagnostics']['grid_localization']['cell_count'] == 9
    issues = [item['id'] for item in localized] if all_cells else [localized[0]['id']]
    return {'request_id': request, 'budget_seconds': budget, 'target': {
        'input_version_id': versions[index], 'result_id': detail['result']['id'],
        'result_revision': detail['result']['revision'], 'issue_ids': issues}}


def submit(fixture, payload=None, **kwargs):
    store, project, batch, versions, queue, manager = fixture
    return submit_local_candidates(manager, project, batch, payload or body(fixture, **kwargs))


def read(fixture, request='local-request-001', **kwargs):
    store, project, batch, versions, queue, manager = fixture
    return read_local_candidates(manager, project, batch, request, **kwargs)


def control(fixture, action, request='local-request-001', operation=None):
    store, project, batch, versions, queue, manager = fixture
    return control_local_candidates(manager, project, batch, request,
        {'action': action, 'request_id': operation or 'control-' + uid()})


def test_submission_is_durable_idempotent_and_does_not_wait_for_manager_or_gpu(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    payload = body(candidate_fixture)
    before = store.rows('SELECT * FROM results')
    # Long preparation/model ownership must not block this short endpoint.
    with manager.guard, store.file_lock, queue.gpu_guard, ThreadPoolExecutor(1) as pool:
        receipt = pool.submit(submit, candidate_fixture, payload).result(timeout=3)
    assert receipt['status'] == 'submitted' and receipt['execution_status'] == 'pending'
    assert len(receipt['task_ids']) == 1 and queue.adapter is None
    assert store.rows('SELECT * FROM results') == before
    repeated = submit(candidate_fixture, payload)
    assert repeated['task_ids'] == receipt['task_ids']
    assert len(store.rows('SELECT * FROM screen_local_requests')) == 1
    changed = deepcopy(payload); changed['budget_seconds'] += 1
    with pytest.raises(Conflict):
        submit(candidate_fixture, changed)
    assert submit_local_candidates(manager, project, batch,
        body(candidate_fixture, request='never-created-001'), allow_new=False) is None


def test_parallel_submission_commits_one_task_and_response_loss_can_be_looked_up(candidate_fixture):
    payload = body(candidate_fixture)
    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(lambda _: submit(candidate_fixture, payload), range(4)))
    assert all(item['task_ids'] == receipts[0]['task_ids'] for item in receipts)
    assert len(candidate_fixture[0].rows('SELECT * FROM screen_local_requests')) == 1
    assert submit_local_candidates(candidate_fixture[5], candidate_fixture[1], candidate_fixture[2],
        payload, allow_new=False)['task_ids'] == receipts[0]['task_ids']


def test_exact_pixels_reuse_partial_overlap_and_unowned_controls(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    first = submit(candidate_fixture)
    reused = submit(candidate_fixture, request='local-repeat-002')
    assert reused['status'] == 'reused' and reused['task_ids'] == first['task_ids']
    assert reused['owned_task_ids'] == []
    assert control(candidate_fixture, 'cancel', 'local-repeat-002')['counts'] == {'queued': 1}
    partial = submit(candidate_fixture, request='local-partial-003', all_cells=True)
    assert partial['status'] == 'blocked' and partial['task_ids'] == first['task_ids']
    assert len(store.rows('SELECT * FROM screen_local_requests')) == 1


def test_unavailable_capability_receipt_is_frozen_and_never_retries_silently(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    payload = body(candidate_fixture)
    with patch('ocr_workbench.screen_resolution.precision_capability', return_value={
            'available': False, 'reason': 'glm_runtime_missing'}):
        blocked = submit(candidate_fixture, payload)
    assert blocked['status'] == 'blocked' and blocked['reason'] == 'glm_runtime_missing'
    assert submit(candidate_fixture, payload)['status'] == 'blocked'
    assert store.rows('SELECT * FROM screen_local_requests') == []
    assert submit(candidate_fixture, request='local-after-ready-002')['status'] == 'submitted'


def test_real_queue_emits_bound_crop_artifacts_and_never_changes_parent_result(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    receipt = submit(candidate_fixture)
    before = {table: store.rows('SELECT * FROM ' + table) for table in ('results', 'selections', 'reviews')}
    with patch.object(store, 'complete', side_effect=AssertionError('No page result adoption')):
        assert queue.step()
    done = read(candidate_fixture)
    assert done['execution_status'] == 'succeeded' and done['items'][0]['candidate_state'] == 'available'
    assert done['items'][0]['text'] == '1'
    assert {table: store.rows('SELECT * FROM ' + table) for table in before} == before
    task = store.one('tasks', receipt['task_ids'][0])
    assert task['kind'] == 'screen_local_text' and task['result_id'] is None
    side = store.rows('SELECT metadata FROM screen_local_requests WHERE task_id=?', (task['id'],))[0]
    crop = json.loads(side['metadata'])['crop']
    assert crop['transform']['integer_including_bbox'] == [185, 242, 310, 305]
    assert crop['transform']['output_size'] == [439, 253]
    assert crop['transform']['source_to_crop_affine'] == [3, 0, -523, 0, 3, -694]
    assert set(done['items'][0]['artifact']) == {'raw.json', 'result.json', 'input.png',
        'result.txt', 'crop.png', 'crop-binding.json'}
    for name, artifact in done['items'][0]['artifact'].items():
        path = local_candidate_file(manager, project, batch, receipt['request_id'], task['id'], name)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact['sha256']
    with pytest.raises(KeyError):
        local_candidate_file(manager, project, batch, receipt['request_id'], task['id'], '../workbench.sqlite3')


@pytest.mark.parametrize('alteration', ['edit', 'selection', 'review', 'source_removed'])
def test_late_change_preserves_raw_as_stale_history(candidate_fixture, alteration):
    store, project, batch, versions, queue, manager = candidate_fixture
    receipt = submit(candidate_fixture)
    task_id = receipt['task_ids'][0]
    parent = receipt['target']['result_id']
    queue.adapter = CpuAdapter(queue.bundle, 'glm', store.root / 'sessions')
    def change(data, path, output):
        if alteration == 'edit':
            value = store.result(parent)['edited']; value['text'] = '人工编辑'
            store.save(parent, value, 0)
        elif alteration == 'selection':
            with store.transaction() as db:
                db.execute('DELETE FROM selections WHERE image_id=?', (store.one('tasks', task_id)['image_id'],))
        elif alteration == 'review':
            manager._update(batch, versions[0], review_state='accepted')
        else:
            store.file(store.one('versions', versions[0])['path']).unlink()
    queue.adapter.before_return = change
    assert queue.step()
    done = read(candidate_fixture)
    assert done['execution_status'] == 'succeeded'
    assert done['items'][0]['candidate_state'] == 'stale'
    assert done['items'][0]['artifact']['raw.json'] and done['items'][0]['text'] == '1'
    assert done['items'][0]['selection_applied'] is False


def test_prestart_change_and_corrupted_mode_fail_without_page_adoption(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    payload = body(candidate_fixture)
    payload['target']['result_revision'] = 99
    with pytest.raises(Conflict):
        submit(candidate_fixture, payload)
    receipt = submit(candidate_fixture)
    manager._update(batch, versions[0], review_state='accepted')
    assert queue.step() and read(candidate_fixture)['execution_status'] == 'failed'
    assert queue.adapter is None
    manager._update(batch, versions[0], review_state='pending')
    receipt = submit(candidate_fixture, index=1, request='local-bad-mode-002')
    queue.adapter = CpuAdapter(queue.bundle, 'glm', store.root / 'sessions')
    queue.adapter.before_return = lambda data, *args: data.update(recognition_mode='page')
    assert queue.step()
    done = read(candidate_fixture, receipt['request_id'])
    assert done['execution_status'] == 'failed' and done['items'][0]['text'] is None
    assert done['items'][0]['artifact']['raw.json']


def test_pause_resume_cancel_and_retry_only_failed(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    receipt = submit(candidate_fixture)
    task_id = receipt['task_ids'][0]
    assert control(candidate_fixture, 'pause')['counts'] == {'paused': 1}
    assert control(candidate_fixture, 'resume')['counts'] == {'queued': 1}
    assert control(candidate_fixture, 'cancel')['counts'] == {'cancelled': 1}
    assert control(candidate_fixture, 'retry_failed')['counts'] == {'cancelled': 1}
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
    assert control(candidate_fixture, 'retry_failed')['counts'] == {'queued': 1}


def test_cancel_late_return_keeps_raw_without_publishing_candidate(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    receipt = submit(candidate_fixture)
    queue.adapter = CpuAdapter(queue.bundle, 'glm', store.root / 'sessions')
    queue.adapter.before_return = lambda *args: control(candidate_fixture, 'cancel')
    assert queue.step()
    done = read(candidate_fixture)
    assert done['counts'] == {'cancelled': 1} and done['items'][0]['text'] is None
    assert done['items'][0]['artifact']['raw.json']


def test_actual_budget_timer_keeps_late_raw_and_marks_failed(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    task_id = submit(candidate_fixture, budget=10)['task_ids'][0]
    with store.transaction() as db:
        db.execute('UPDATE screen_local_requests SET spent_seconds=8 WHERE task_id=?', (task_id,))
    queue.adapter = CpuAdapter(queue.bundle, 'glm', store.root / 'sessions')
    queue.adapter.before_return = lambda *args: queue.cancel_event.wait(4)
    assert queue.step()
    done = read(candidate_fixture)
    assert done['counts'] == {'failed': 1} and done['items'][0]['text'] is None
    assert done['items'][0]['artifact']['raw.json']
    assert '预算' in done['items'][0]['error'] and queue.adapter is None


def test_source_drift_after_completion_is_stale_and_changed_artifact_is_rejected(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    task_id = submit(candidate_fixture)['task_ids'][0]
    queue.step()
    store.file(store.one('versions', versions[0])['path']).write_bytes(b'changed after return')
    assert read(candidate_fixture)['items'][0]['candidate_state'] == 'stale'
    path = local_candidate_file(manager, project, batch, 'local-request-001', task_id, 'raw.json')
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(Conflict):
        local_candidate_file(manager, project, batch, 'local-request-001', task_id, 'raw.json')


@pytest.mark.parametrize('alteration', ['input_bytes', 'raw_file', 'claimed_other_pixels'])
def test_actual_prepared_input_and_raw_file_are_bound_before_candidate_publication(candidate_fixture, alteration):
    store, project, batch, versions, queue, manager = candidate_fixture
    submit(candidate_fixture)
    queue.adapter = CpuAdapter(queue.bundle, 'glm', store.root / 'sessions')
    def change(data, path, output):
        if alteration == 'input_bytes':
            (output / 'input.png').write_bytes(b'different prepared file')
        elif alteration == 'raw_file':
            write_json(output / 'raw.json', {'recognition_mode': 'local_text', 'decoder_text': 'different'})
        else:
            with Image.open(output / 'input.png') as opened:
                pixels = opened.convert('RGB')
            pixels.putpixel((20, 20), (12, 34, 56))
            pixels.save(output / 'input.png')
            changed_hash = hashlib.sha256((output / 'input.png').read_bytes()).hexdigest()
            data['image']['version'] = data['raw']['input_image_sha256'] = changed_hash
            write_json(output / 'raw.json', data['raw'])
            write_json(output / 'result.json', data)
    queue.adapter.before_return = change
    assert queue.step()
    done = read(candidate_fixture)
    assert done['counts'] == {'failed': 1} and done['items'][0]['text'] is None
    assert done['items'][0]['artifact']['raw.json'] and done['items'][0]['artifact']['input.png']


def test_task_and_batch_deletion_cascade_side_requests_without_orphans(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    tasks = submit(candidate_fixture, all_cells=True)['task_ids']
    with store.transaction() as db:
        db.execute('DELETE FROM tasks WHERE id=?', (tasks[0],))
        assert db.execute('SELECT count(*) FROM screen_local_requests').fetchone()[0] == 1
        db.execute('DELETE FROM screen_batches WHERE id=?', (batch,))
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
    assert store.rows('SELECT * FROM screen_local_requests') == []
    assert store.rows('SELECT * FROM screen_actions') == []


def test_lineage_reservations_terminal_parents_siblings_and_recovery_cost(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    first = submit(candidate_fixture, all_cells=True)
    assert first['reserved_seconds'] == 120 and len(first['task_ids']) == 2
    one = remaining_screen_budget(store, batch, versions[0])
    assert one['inflight_reserved_seconds'] == pytest.approx(120)
    second = submit(candidate_fixture, index=1, request='local-sibling-002', budget=160)
    two = remaining_screen_budget(store, batch, versions[0])
    assert two['inflight_reserved_seconds'] == pytest.approx(280)
    task_id = first['task_ids'][0]
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='interrupted',started='2026-10-08T00:00:00+00:00',"
            "finished='2026-10-08T00:00:17+00:00' WHERE id=?", (task_id,))
    before = task_seconds_spent(store, task_id)
    assert before == pytest.approx(17, abs=.001)
    assert control(candidate_fixture, 'resume')['counts'] == {'queued': 2}
    from ocr_workbench.store import now
    fixed = now()
    with patch('ocr_workbench.store.now', return_value=fixed):
        claimed = store.claim()
        assert claimed['id'] in first['task_ids'] + second['task_ids']
        carried = store.rows('SELECT spent_seconds FROM screen_local_requests WHERE task_id=?', (task_id,))[0]['spent_seconds']
        if claimed['id'] != task_id:
            with store.transaction() as db:
                db.execute("UPDATE tasks SET status='paused' WHERE id=?", (claimed['id'],))
            assert store.claim()['id'] == task_id
            carried = store.rows('SELECT spent_seconds FROM screen_local_requests WHERE task_id=?', (task_id,))[0]['spent_seconds']
        budget = remaining_screen_budget(store, batch, versions[0], task_id=task_id)
    assert carried == pytest.approx(17, abs=.001)
    assert 42 < budget['remaining_seconds'] <= 43.001
    assert budget['inflight_reserved_seconds'] == pytest.approx(220, abs=.001)


def test_same_source_hash_budget_is_shared_and_repeated_pixels_do_not_rerun(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    # Make the second normalized input a genuine identical-source version,
    # including all existing parent, preparation and auxiliary bindings.
    a, b = [store.one('versions', key) for key in versions]
    shutil.copyfile(store.file(a['path']), store.file(b['path']))
    with store.transaction() as db:
        db.execute('UPDATE versions SET sha256=? WHERE id=?', (a['sha256'], b['id']))
        db.execute('UPDATE screen_items SET source_hash=? WHERE batch_id=? AND input_version_id=?',
            (a['sha256'], batch, b['id']))
    item = manager._items(batch)[1]
    prep = json.loads(item['preparation']); prep['input_sha256'] = a['sha256']
    result = store.result(item['result_id'])
    raw = {**deepcopy(result['original']), **deepcopy(result['edited'])}
    raw['screen']['input_sha256'] = a['sha256']
    result_id = manager._final_result(manager._batch(project, batch), item, raw)
    manager._update(batch, b['id'], preparation=encoded(prep), result_id=result_id, result_revision=0)
    first = submit(candidate_fixture)
    reused = submit(candidate_fixture, index=1, request='same-source-pixels-002')
    assert reused['status'] == 'blocked' and reused['task_ids'] == reused['owned_task_ids'] == []
    assert reused['reason'] == 'other_input_or_batch_same_pixels_already_attempted'
    assert len(store.rows('SELECT * FROM screen_local_requests')) == 1
    budget = remaining_screen_budget(store, batch, b['id'])
    assert budget['per_source_consumed_or_reserved'][a['sha256']] == pytest.approx(160)


def test_cold_load_guard_counts_real_local_timeouts_and_resets_on_ready(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    task_id = submit(candidate_fixture)['task_ids'][0]
    assert record_cold_load(store, task_id, 'timeout', error=TimeoutError('request')) is None
    error = EngineLoadTimeout('glm', 240, store.root / 'sessions')
    for _ in range(3):
        record_cold_load(store, task_id, 'timeout', error=error)
    assert cold_load_state(store, task_id)['blocked'] is True
    record_cold_load(store, task_id, 'ready')
    assert cold_load_state(store, task_id)['consecutive_timeouts'] == 0


def test_schema19_rolls_back_failed_migration_and_v18_reader_refuses_new_data(candidate_fixture, tmp_path):
    store = candidate_fixture[0]
    other = tmp_path / 'migration'
    other.mkdir()
    legacy = other / 'workbench.sqlite3'
    with closing(sqlite3.connect(legacy)) as target, closing(sqlite3.connect(store.root / 'workbench.sqlite3')) as source:
        source.backup(target)
        target.execute('DROP TABLE screen_local_requests')
        target.execute('PRAGMA user_version=18')
        target.commit()
    with patch('ocr_workbench.screen_local_candidates.migrate_local_candidates', side_effect=RuntimeError('migration fail')):
        with pytest.raises(RuntimeError):
            Store(other)
    with closing(sqlite3.connect(legacy)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 18
        assert db.execute("SELECT name FROM sqlite_master WHERE name='screen_local_requests'").fetchone() is None
    upgraded = Store(other)
    assert upgraded.schema_version == SCHEMA_VERSION == 19
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in other.iterdir() if path.is_file()}
    with patch('ocr_workbench.store.SCHEMA_VERSION', 18):
        with pytest.raises(ValueError, match='高于'):
            Store(other)
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in other.iterdir() if path.is_file()}
    assert before == after


def test_control_operation_response_loss_never_retries_a_second_failure(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    task_id = submit(candidate_fixture)['task_ids'][0]
    operation = {'action': 'retry_failed', 'request_id': 'durable-retry-001'}
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
    first = control_local_candidates(manager, project, batch, 'local-request-001', operation)
    assert first['request_id'] == 'local-request-001' and first['control']['committed'] is True
    assert first['control']['affected_task_ids'] == [task_id]
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
    recovered = control_local_candidates(manager, project, batch, 'local-request-001', operation, allow_new=False)
    assert recovered['counts'] == {'failed': 1} and recovered['control'] == first['control']
    assert control_local_candidates(manager, project, batch, 'local-request-001', operation)['counts'] == {'failed': 1}
    unknown = {**operation, 'request_id': 'unknown-operation-002'}
    assert control_local_candidates(manager, project, batch, 'local-request-001', unknown, allow_new=False) is None
    with pytest.raises(Conflict):
        control_local_candidates(manager, project, batch, 'local-request-001', {**operation, 'action': 'resume'})


def test_future_schema_probe_replays_live_wal_without_changing_database_or_frames(tmp_path):
    root = tmp_path / 'live-wal'
    store = Store(root)
    path = root / 'workbench.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA user_version=18')
        db.commit()
    with closing(sqlite3.connect(path)) as keeper:
        keeper.execute('PRAGMA journal_mode=WAL')
        keeper.execute('PRAGMA user_version=19')
        keeper.commit()
        wal = path.with_name(path.name + '-wal')
        assert wal.stat().st_size > 0
        before = {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in (path, wal)}
        with patch('ocr_workbench.store.SCHEMA_VERSION', 18):
            with pytest.raises(ValueError, match='高于'):
                Store(root)
        assert before == {item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in (path, wal)}
        assert keeper.execute('PRAGMA user_version').fetchone()[0] == 19


def test_cross_project_foreign_result_and_unknown_geometry_are_rejected(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    payload = body(candidate_fixture)
    foreign = store.project('其他项目')['id']
    with pytest.raises(KeyError):
        submit_local_candidates(manager, foreign, batch, payload)
    payload['target']['result_id'] = manager._items(batch)[1]['result_id']
    with pytest.raises(Conflict):
        submit(candidate_fixture, payload)
    payload = body(candidate_fixture); payload['target']['issue_ids'] = ['never-issued']
    with pytest.raises(Conflict):
        submit(candidate_fixture, payload)
    for value in ({**payload, 'bbox': [0, 0, 100, 100]}, {**payload, 'budget_seconds': True}):
        with pytest.raises(ValueError):
            validate_submission(value)


def test_http_requires_compute_permission_and_returns_real_artifacts(candidate_fixture):
    store, project, batch, versions, queue, manager = candidate_fixture
    app, allowed, checks = FastAPI(), [True], []
    def require():
        checks.append('compute')
        if not allowed[0]:
            raise ValueError('Recognition unavailable')
    @app.exception_handler(ValueError)
    def bad(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)
    register_screen_routes(app, manager, require)
    client = TestClient(app)
    prefix = '/api/projects/%s/screen-batches/%s/local-candidates' % (project, batch)
    payload = body(candidate_fixture)
    allowed[0] = False
    assert client.post(prefix, json=payload).status_code == 400
    assert store.rows('SELECT * FROM screen_local_requests') == []
    allowed[0] = True
    response = client.post(prefix, json=payload)
    assert response.status_code == 200
    task_id = response.json()['task_ids'][0]
    queue.step()
    allowed[0] = False
    assert client.get(prefix + '/' + payload['request_id']).json()['execution_status'] == 'succeeded'
    assert client.post(prefix + '/lookup', json=payload).status_code == 200
    assert client.post(prefix + '/' + payload['request_id'] + '/control',
        json={'action': 'resume', 'request_id': 'permission-resume-001'}).status_code == 400
    file_url = prefix + '/' + payload['request_id'] + '/tasks/' + task_id + '/files/crop.png'
    assert client.get(file_url).headers['content-type'] == 'image/png'
    assert client.get(file_url).content == local_candidate_file(manager, project, batch,
        payload['request_id'], task_id, 'crop.png').read_bytes()

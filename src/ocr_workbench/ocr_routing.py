"""Durable engine selection and bounded fallback, sharing the existing queue."""
import json
import os
from pathlib import Path
from ocr_workbench.store import encoded, now

ENGINES = ('ppocr', 'paddlevl', 'glm', 'hunyuan', 'mineru_pro')


def mark_reader(store):
    ledger = os.environ.get('WORKBENCH_GPU_LEDGER')
    if os.environ.get('WORKBENCH_APP_ID') == 'ocr' and ledger:
        from workbench_platform.resources.reader_features import require_features
        require_features(Path(ledger).parent, 'ocr-mineru-pro-v1')


def ensure_route(db, task_id, *, fallback_engine='ppocr', baseline_result_id=None,
                 baseline_revision=None, crop_box=None):
    if fallback_engine not in ENGINES or fallback_engine == 'mineru_pro':
        raise ValueError('后备引擎必须是已有 OCR 引擎')
    task = db.execute('SELECT engine FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None:
        raise KeyError('识别任务不存在')
    db.execute('''INSERT OR IGNORE INTO task_engine_routes
        (task_id,requested_engine,fallback_engine,attempts,baseline_result_id,baseline_revision,crop_box)
        VALUES(?,?,?,'[]',?,?,?)''',
        (task_id, task['engine'], fallback_engine, baseline_result_id, baseline_revision,
         encoded(crop_box) if crop_box is not None else None))


def route_task(store, task):
    if task['engine'] == 'auto':
        mark_reader(store)
        with store.transaction() as db:
            ensure_route(db, task['id'])
    rows = store.rows('SELECT * FROM task_engine_routes WHERE task_id=?', (task['id'],))
    if not rows:
        return task, None
    route = rows[0]
    engine = route['actual_engine'] or ('mineru_pro' if route['requested_engine'] == 'auto' else task['engine'])
    return {**task, 'engine': engine, 'engine_package': 'builtin' if task['engine'] == 'auto' else task.get('engine_package', 'builtin')}, route


def load_event(store, task_id, engine, status, message=None):
    with store.transaction() as db:
        row = db.execute('SELECT * FROM task_engine_routes WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            return
        attempts = json.loads(row['attempts'])
        attempts.append({'engine': engine, 'stage': 'load', 'status': status,
                         'at': now(), **({'message': str(message)[:1000]} if message else {})})
        db.execute('UPDATE task_engine_routes SET actual_engine=?,attempts=? WHERE task_id=?',
                   (engine, encoded(attempts), task_id))


def fallback_task(store, task, route, error):
    if not route or route['requested_engine'] != 'auto' or task['engine'] != 'mineru_pro':
        raise error
    load_event(store, task['id'], task['engine'], 'failed', error)
    current = store.rows('SELECT * FROM task_engine_routes WHERE task_id=?', (task['id'],))[0]
    fallback = current['fallback_engine']
    if any(item['engine'] == fallback for item in json.loads(current['attempts'])):
        raise error
    load_event(store, task['id'], fallback, 'started')
    return {**task, 'engine': fallback, 'engine_package': 'builtin'}


def result_route(store, task_id):
    rows = store.rows('SELECT * FROM task_engine_routes WHERE task_id=?', (task_id,))
    if not rows:
        return None
    row = rows[0]
    attempts = json.loads(row['attempts'])
    return {'requested_engine': row['requested_engine'], 'actual_engine': row['actual_engine'],
            'fallback_engine': row['fallback_engine'],
            'fallback_used': row['requested_engine'] == 'auto' and row['actual_engine'] != 'mineru_pro',
            'fallback_reason': next((item.get('message') for item in attempts if item['engine'] == 'mineru_pro' and item['status'] == 'failed'), None),
            'attempts': attempts, 'baseline_result_id': row['baseline_result_id'],
            'baseline_revision': row['baseline_revision'],
            'crop_box': json.loads(row['crop_box']) if row['crop_box'] else None}


def enqueue_enhancement(services, result_id, body):
    """A new result on the same immutable version, leaving the baseline intact."""
    import math
    from ocr_workbench.store import Conflict
    store = services.store
    mark_reader(store)
    baseline = store.result(result_id)
    revision = body.get('revision')
    if type(revision) is not int or revision != baseline['revision']:
        raise Conflict('原结果修订已变化，请刷新后再增强识别')
    task = store.one('tasks', baseline['task_id'])
    version_id = baseline['original'].get('project_image_version') or task['version_id']
    version = store.one('versions', version_id)
    box = body.get('box')
    if box is not None:
        if (not isinstance(box, list) or len(box) != 4 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
            raise ValueError('区域需要四个有限数字坐标')
        box = [math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3])]
        if not (0 <= box[0] < box[2] <= version['width'] and 0 <= box[1] < box[3] <= version['height']) or min(box[2]-box[0], box[3]-box[1]) < 8:
            raise ValueError('增强识别区域太小或超出此结果的原图版本')
    with services.maintenance.guard, store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT revision FROM results WHERE id=?', (result_id,)).fetchone()[0] != revision:
            raise Conflict('原结果修订已变化')
        packages = {name: spec['package_id'] for name, spec in services.registry.engines().items()}
        ids = store._enqueue(db, task['project_id'], [version_id], ['mineru_pro'],
                             engine_packages=packages, request_id=body.get('request_id'))
        for key in ids:
            old = db.execute('SELECT * FROM task_engine_routes WHERE task_id=?', (key,)).fetchone()
            if old and (old['baseline_result_id'] != result_id or old['baseline_revision'] != revision or
                        (json.loads(old['crop_box']) if old['crop_box'] else None) != box):
                raise Conflict('增强识别请求编号已绑定其它原结果或区域')
            ensure_route(db, key, baseline_result_id=result_id, baseline_revision=revision, crop_box=box)
    services.queue.wake.set()
    return {'task_ids': ids, 'baseline_result_id': result_id, 'baseline_revision': revision,
            'version_id': version_id, 'box': box, 'engine': 'mineru_pro'}


def comparison(store, result_id):
    enhanced = store.result_snapshot(result_id)
    route = enhanced.get('original', {}).get('engine_route') or {}
    baseline_id = route.get('baseline_result_id')
    if not baseline_id:
        return {'result_id': result_id, 'has_comparison': False}
    baseline = store.result_snapshot(baseline_id, route['baseline_revision'])
    if baseline['version_id'] != enhanced['version_id'] or baseline['project_id'] != enhanced['project_id']:
        raise ValueError('增强识别对照的原件版本无法核实')
    return {'result_id': result_id, 'has_comparison': True, 'baseline': baseline,
            'enhanced': enhanced, 'box': route.get('crop_box'), 'actual_engine': route.get('actual_engine')}

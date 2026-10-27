"""Queued, source-bound GLM crop candidates, separate from adopted OCR results."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time

from ocr_workbench.store import Conflict, encoded, now, uid
from ocr_workbench.paths import require_path

VERSION = 'screen-local-candidates-v1'
CONTROL_VERSION = 'screen-local-control-v1'
IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,120}$')
TERMINAL_ITEMS = {'completed', 'needs_review', 'failed', 'cancelled'}
ACTIVE_TASKS = {'queued', 'running', 'paused', 'interrupted'}
ARTIFACT_NAMES = {'raw.json', 'result.json', 'error.json', 'input.png', 'result.txt', 'result.md',
    'crop.png', 'crop-binding.json'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def migrate_local_candidates(db):
    db.execute('''CREATE TABLE screen_local_requests(
        task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
        batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
        root_batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, input_version_id TEXT NOT NULL REFERENCES versions(id),
        parent_result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
        pixel_key TEXT NOT NULL, snapshot TEXT NOT NULL, metadata TEXT NOT NULL,
        spent_seconds REAL NOT NULL DEFAULT 0 CHECK(spent_seconds>=0),
        candidate TEXT, artifact TEXT, candidate_state TEXT NOT NULL DEFAULT 'pending',
        UNIQUE(root_batch_id,pixel_key))''')
    db.execute('CREATE INDEX screen_local_requests_group ON screen_local_requests(batch_id,request_id,task_id)')


def local_candidate_capability(manager):
    from ocr_workbench.screen_resolution import precision_capability
    value = precision_capability(manager)
    return {**value, 'recognition_mode': 'local_text', 'explicit_request_only': True,
        'automatic_apply': False, 'inference_verified_by_this_check': False}


def validate_submission(body):
    if not isinstance(body, dict) or set(body) != {'request_id', 'budget_seconds', 'target'}:
        raise ValueError('局部候选必须使用已登记的问题、结果版本及预算；不接受自由坐标')
    request, budget, target = body['request_id'], body['budget_seconds'], body['target']
    if not isinstance(request, str) or not 8 <= len(request) <= 120 or not IDENTIFIER.fullmatch(request):
        raise ValueError('局部候选请求编号无效')
    if type(budget) is not int or not 10 <= budget <= 900:
        raise ValueError('局部候选总预算必须为10–900秒')
    if not isinstance(target, dict) or set(target) != {'input_version_id', 'result_id', 'result_revision', 'issue_ids'}:
        raise ValueError('局部候选必须绑定一张原图、当前结果修订和实际问题')
    if any(not isinstance(target[key], str) or not IDENTIFIER.fullmatch(target[key])
           for key in ('input_version_id', 'result_id')):
        raise ValueError('局部候选原图或结果身份无效')
    if type(target['result_revision']) is not int or target['result_revision'] < 0:
        raise ValueError('局部候选结果修订号无效')
    issues = target['issue_ids']
    if (not isinstance(issues, list) or not 1 <= len(issues) <= 12
            or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in issues)
            or len(set(issues)) != len(issues)):
        raise ValueError('每次须选择1–12个不同的当前疑点')
    return deepcopy(body)


def _binding(db, batch_id, version_id, result_id):
    item = db.execute('''SELECT i.image_id,i.source_hash,i.result_id item_result_id,i.review_state,
        im.active_version,v.sha256 version_sha,r.revision,r.edited,s.result_id selected_result_id,
        rv.result_id review_result_id,rv.version_id review_version_id,rv.revision review_revision,
        rv.status human_review_status,rv.updated review_epoch
        FROM screen_items i JOIN images im ON im.id=i.image_id
        JOIN versions v ON v.id=i.input_version_id JOIN results r ON r.id=?
        LEFT JOIN selections s ON s.image_id=i.image_id LEFT JOIN reviews rv ON rv.image_id=i.image_id
        WHERE i.batch_id=? AND i.input_version_id=?''', (result_id, batch_id, version_id)).fetchone()
    if item is None:
        raise Conflict('局部候选所绑定的原图或结果已不存在')
    value = dict(item)
    value['edited_sha256'] = digest(json.loads(value.pop('edited')))
    return value


def _current(store, snapshot, db=None):
    if db is None:
        with store.transaction() as connection:
            connection.execute('BEGIN')
            return _current(store, snapshot, connection)
    try:
        current = _binding(db, snapshot['batch_id'], snapshot['input_version_id'], snapshot['result_id'])
    except Conflict:
        return False
    return current == snapshot['binding']


def _source_current(store, snapshot):
    try:
        return hashlib.sha256(store.file(snapshot['source_path']).read_bytes()).hexdigest() == snapshot['source_sha256']
    except (OSError, ValueError):
        return False


def _receipt(manager, project_id, batch_id, request_id):
    manager._batch(project_id, batch_id)
    rows = manager.store.rows('SELECT payload_hash,receipt FROM screen_actions WHERE batch_id=? AND request_id=?',
        (batch_id, request_id))
    if not rows:
        return None
    receipt = json.loads(rows[0]['receipt'])
    if receipt.get('version') != VERSION:
        raise Conflict('请求编号已被其他批次操作使用')
    return rows[0]['payload_hash'], receipt


def _freeze(manager, project_id, batch_id, target):
    batch = manager._batch(project_id, batch_id)
    item = next((item for item in manager._items(batch_id) if item['input_version_id'] == target['input_version_id']), None)
    if not item or item['status'] not in TERMINAL_ITEMS or item['result_id'] != target['result_id']:
        raise Conflict('请读取当前已完成图片的实际结果及疑点')
    with manager.store.transaction() as db:
        binding = _binding(db, batch_id, target['input_version_id'], target['result_id'])
    if (binding['revision'] != target['result_revision'] or binding['active_version'] != target['input_version_id']
            or binding['selected_result_id'] not in (None, target['result_id'])
            or binding['source_hash'] != binding['version_sha']):
        raise Conflict('原图、结果修订或选定候选已变化')
    version = manager.store.one('versions', target['input_version_id'])
    source = manager.store.file(version['path'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != binding['source_hash']:
        raise Conflict('原图文件已变化')
    detail = manager.detail(project_id, batch_id, target['input_version_id'])
    diagnostics = detail.get('diagnostics', {})
    if diagnostics.get('result_id') != target['result_id'] or diagnostics.get('result_revision') != target['result_revision']:
        raise Conflict('诊断结果版本已变化')
    issues = {issue['id']: issue for issue in diagnostics.get('items', [])}
    if not set(target['issue_ids']) <= set(issues):
        raise Conflict('疑点编号已变化，请重新读取')
    localization = diagnostics.get('grid_localization', {})
    if localization.get('binding', {}).get('input_sha256') != binding['source_hash']:
        raise Conflict('格位没有当前原图像素证明')
    cells = {}
    for issue_id in target['issue_ids']:
        location = issues[issue_id].get('location', {})
        if location.get('level') != 'cell' or location.get('range_semantics') != 'full_cell':
            raise Conflict('此疑点尚无可靠原图格位，不能局部复识')
        table = next((table for table in localization.get('tables', [])
            if table.get('table') == location.get('table') and table.get('status') == 'verified'), None)
        geometry = next((cell for cell in (table or {}).get('cells', [])
            if cell.get('row') == location.get('row') and cell.get('column') == location.get('column')
            and cell.get('box') == location.get('box')), None)
        if not geometry:
            raise Conflict('格位缺少像素分区及独立行列锚点证明')
        key = digest([location['table'], geometry])
        if key not in cells:
            cells[key] = {'location': deepcopy(location), 'geometry': deepcopy(geometry), 'issue_ids': []}
        cells[key]['issue_ids'].append(issue_id)
    snapshot = {'batch_id': batch_id, 'input_version_id': target['input_version_id'],
        'result_id': target['result_id'], 'result_revision': target['result_revision'],
        'binding': binding, 'source_sha256': binding['source_hash'],
        'source_path': str(source.relative_to(manager.store.root)),
        'grid_proof_sha256': digest(localization), 'diagnostics_binding': diagnostics['binding'],
        'source_size': [version['width'], version['height']], 'scale': 3, 'border': 32}
    return batch, snapshot, list(cells.values())


def submit_local_candidates(manager, project_id, batch_id, body, *, allow_new=True):
    body = validate_submission(body)
    old = _receipt(manager, project_id, batch_id, body['request_id'])
    signature = digest(body)
    if old:
        if old[0] != signature:
            raise Conflict('请求编号已绑定不同局部候选参数')
        return read_local_candidates(manager, project_id, batch_id, body['request_id'])
    if not allow_new:
        return None
    capability = local_candidate_capability(manager)
    if capability.get('available') is not True:
        receipt = {'version': VERSION, 'request_id': body['request_id'], 'batch_id': batch_id,
            'status': 'blocked', 'reason': capability.get('reason'), 'capability': capability,
            'task_ids': [], 'owned_task_ids': [], 'target': body['target'],
            'requested_budget_seconds': body['budget_seconds'], 'reserved_seconds': 0,
            'values_modified': False, 'selection_applied': False, 'human_review': 'not_performed',
            'automatic_apply': False}
        with manager.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT payload_hash,receipt FROM screen_actions WHERE batch_id=? AND request_id=?',
                (batch_id, body['request_id'])).fetchone()
            if prior:
                if prior['payload_hash'] != signature or json.loads(prior['receipt']).get('version') != VERSION:
                    raise Conflict('请求编号已绑定不同操作或参数')
            else:
                db.execute('INSERT INTO screen_actions VALUES(?,?,?,?)', (batch_id, body['request_id'], signature, encoded(receipt)))
        return read_local_candidates(manager, project_id, batch_id, body['request_id'])
    batch, snapshot, cells = _freeze(manager, project_id, batch_id, body['target'])
    from ocr_workbench.screen_resolution import _lineage, remaining_screen_budget
    root, _ = _lineage(manager, project_id, batch_id)
    if any(item['status'] not in TERMINAL_ITEMS for item in manager._items(root)):
        raise Conflict('主批次尚未完成，局部候选不会抢占其预算')
    keys = [digest([snapshot['source_sha256'], cell['geometry'], manager.model_identity,
        snapshot['scale'], snapshot['border'], 'local_text']) for cell in cells]
    with manager.store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT payload_hash,receipt FROM screen_actions WHERE batch_id=? AND request_id=?',
            (batch_id, body['request_id'])).fetchone()
        if prior:
            if prior['payload_hash'] != signature or json.loads(prior['receipt']).get('version') != VERSION:
                raise Conflict('请求编号已绑定不同局部候选参数')
        else:
            if not _current(manager.store, snapshot, db):
                raise Conflict('局部候选入队前版本或人工复核状态已变化')
            if hashlib.sha256(manager.store.file(snapshot['source_path']).read_bytes()).hexdigest() != snapshot['source_sha256']:
                raise Conflict('局部候选入队前原图文件已变化')
            existing = [db.execute('SELECT task_id,input_version_id,batch_id FROM screen_local_requests WHERE root_batch_id=? AND pixel_key=?',
                (root, key)).fetchone() for key in keys]
            tasks, owned = [], []
            if any(existing):
                if any(row and (row['input_version_id'] != snapshot['input_version_id'] or row['batch_id'] != batch_id)
                        for row in existing):
                    status, reason = 'blocked', 'other_input_or_batch_same_pixels_already_attempted'
                else:
                    tasks = [row['task_id'] for row in existing if row]
                    status = 'reused' if all(existing) else 'blocked'
                    reason = 'same_pixels_strategy_already_attempted'
                reserved = 0
            else:
                budget = remaining_screen_budget(manager.store, batch_id, snapshot['input_version_id'])
                allowance = min(180, body['budget_seconds'] // len(cells),
                    math.floor(budget['remaining_seconds'] / len(cells)))
                if allowance < 1:
                    status, reason, reserved = 'blocked', 'lineage_budget_exhausted', 0
                else:
                    status, reason, reserved = 'submitted', 'candidate_requires_review', allowance * len(cells)
                    for cell, key in zip(cells, keys):
                        task_id = uid()
                        frozen = {**deepcopy(snapshot), 'cell': cell}
                        metadata = {'model_identity': manager.model_identity,
                            'budget_reservation_seconds': allowance,
                            'request_timeout_seconds': min(allowance, batch['policy'].get('request_timeout_seconds', 900)),
                            'recognition_mode': 'local_text'}
                        db.execute('''INSERT INTO tasks(id,project_id,image_id,version_id,input_version_id,
                            engine,batch,status,phase,created,kind,preprocess,engine_package)
                            VALUES(?,?,?,?,?,? ,?,'queued','等待局部文字候选',?,'screen_local_text','[]',?)''',
                            (task_id, project_id, snapshot['binding']['image_id'], snapshot['input_version_id'],
                             snapshot['input_version_id'], 'glm', now() + '-local-' + body['request_id'], now(),
                             capability.get('package_id', 'builtin')))
                        db.execute('''INSERT INTO screen_local_requests(task_id,batch_id,root_batch_id,request_id,
                            input_version_id,parent_result_id,pixel_key,snapshot,metadata) VALUES(?,?,?,?,?,?,?,?,?)''',
                            (task_id, batch_id, root, body['request_id'], snapshot['input_version_id'], snapshot['result_id'],
                             key, encoded(frozen), encoded(metadata)))
                        tasks.append(task_id)
                    owned = list(tasks)
            receipt = {'version': VERSION, 'request_id': body['request_id'], 'batch_id': batch_id,
                'root_batch_id': root, 'status': status, 'reason': reason, 'task_ids': tasks,
                'owned_task_ids': owned, 'target': body['target'], 'requested_budget_seconds': body['budget_seconds'],
                'reserved_seconds': reserved, 'values_modified': False, 'selection_applied': False,
                'human_review': 'not_performed', 'automatic_apply': False}
            db.execute('INSERT INTO screen_actions VALUES(?,?,?,?)', (batch_id, body['request_id'], signature, encoded(receipt)))
    manager.queue.wake.set()
    return read_local_candidates(manager, project_id, batch_id, body['request_id'])


def read_local_candidates(manager, project_id, batch_id, request_id, *, offset=0, limit=12):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12:
        raise ValueError('局部候选分页范围无效')
    saved = _receipt(manager, project_id, batch_id, request_id)
    if saved is None:
        raise KeyError('局部候选请求不存在')
    receipt = saved[1]
    items, counts = [], {}
    for task_id in receipt['task_ids']:
        row = manager.store.rows('''SELECT l.*,t.status,t.phase,t.error,t.started,t.finished FROM screen_local_requests l
            JOIN tasks t ON t.id=l.task_id WHERE l.task_id=? AND t.project_id=?''', (task_id, project_id))
        if not row:
            raise Conflict('局部候选任务身份已不可用')
        row = row[0]
        snapshot = json.loads(row['snapshot'])
        candidate = json.loads(row['candidate']) if row['candidate'] else None
        current = _current(manager.store, snapshot) and _source_current(manager.store, snapshot)
        counts[row['status']] = counts.get(row['status'], 0) + 1
        items.append({'task_id': task_id, 'batch_id': row['batch_id'], 'root_batch_id': row['root_batch_id'],
            'request_id': row['request_id'], 'input_version_id': row['input_version_id'],
            'parent_result_id': row['parent_result_id'], 'pixel_key': row['pixel_key'],
            'status': row['status'], 'phase': row['phase'], 'error': row['error'],
            'candidate_state': 'stale' if candidate and not current else row['candidate_state'],
            'current_binding': current, 'binding': snapshot, 'text': candidate['text'][:512] if candidate else None,
            'text_preview_only': bool(candidate and len(candidate['text']) > 512),
            'content_status': candidate.get('content_status') if candidate else None,
            'artifact': json.loads(row['artifact']) if row['artifact'] else None,
            'values_modified': False, 'selection_applied': False, 'human_review': 'not_performed'})
    active = sum(counts.get(state, 0) for state in ACTIVE_TASKS)
    status = 'pending' if active else 'succeeded' if items and counts.get('succeeded', 0) == len(items) else receipt['status'] if not items else 'partial' if counts.get('succeeded') else 'failed'
    return {**receipt, 'execution_status': status, 'counts': counts, 'total': len(items),
        'items': items[offset:offset + limit], 'offset': offset, 'limit': limit,
        'next_offset': offset + limit if offset + limit < len(items) else None}


def control_local_candidates(manager, project_id, batch_id, request_id, body, *, allow_new=True):
    if (not isinstance(body, dict) or set(body) != {'action', 'request_id'}
            or body['action'] not in {'pause', 'resume', 'cancel', 'retry_failed'}
            or not isinstance(body['request_id'], str) or not 8 <= len(body['request_id']) <= 120
            or not IDENTIFIER.fullmatch(body['request_id'])):
        raise ValueError('局部候选控制需要有效操作请求编号及暂停、继续、取消或重试失败')
    receipt = _receipt(manager, project_id, batch_id, request_id)
    if receipt is None:
        raise KeyError('局部候选请求不存在')
    action = 'retry' if body['action'] == 'retry_failed' else body['action']
    signature = digest({'submission_request_id': request_id, **body})
    affected, created = [], False
    with manager.store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT payload_hash,receipt FROM screen_actions WHERE batch_id=? AND request_id=?',
            (batch_id, body['request_id'])).fetchone()
        if prior:
            operation = json.loads(prior['receipt'])
            if prior['payload_hash'] != signature or operation.get('version') != CONTROL_VERSION:
                raise Conflict('操作请求编号已绑定不同操作或参数')
        elif not allow_new:
            return None
        else:
            tasks = receipt[1]['owned_task_ids']
            if body['action'] == 'retry_failed':
                tasks = [task_id for task_id in tasks if db.execute('SELECT status FROM tasks WHERE id=?',
                    (task_id,)).fetchone()['status'] == 'failed']
            affected = manager.queue._action_changes(db, project_id, action, tasks)
            operation = {'version': CONTROL_VERSION, 'request_id': body['request_id'],
                'submission_request_id': request_id, 'batch_id': batch_id, 'action': body['action'],
                'affected_task_ids': affected, 'status': 'applied', 'committed': True, 'values_modified': False,
                'selection_applied': False, 'human_review': 'not_performed'}
            db.execute('INSERT INTO screen_actions VALUES(?,?,?,?)',
                (batch_id, body['request_id'], signature, encoded(operation)))
            created = True
    if created:
        manager.queue._signal_action(action, affected)
    return {**read_local_candidates(manager, project_id, batch_id, request_id), 'control': operation}


def prepare_local_input(store, task):
    from PIL import Image
    from ocr_workbench.atomic_files import write_json
    from ocr_workbench.image_utils import normalized_rgb
    row = store.rows('SELECT snapshot FROM screen_local_requests WHERE task_id=?', (task['id'],))
    if not row:
        raise Conflict('局部候选缺少冻结请求')
    snapshot = json.loads(row[0]['snapshot'])
    if not _current(store, snapshot):
        raise Conflict('局部候选开始前原图、结果或人工复核已变化')
    source = store.file(snapshot['source_path'])
    if hashlib.sha256(source.read_bytes()).hexdigest() != snapshot['source_sha256']:
        raise Conflict('局部候选原图文件已变化')
    with Image.open(source) as opened:
        image = normalized_rgb(opened)
    if list(image.size) != snapshot['source_size']:
        raise Conflict('局部候选原图尺寸已变化')
    box = snapshot['cell']['geometry']['box']
    rectangle = [math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3])]
    if not (0 <= rectangle[0] < rectangle[2] <= image.width and 0 <= rectangle[1] < rectangle[3] <= image.height):
        raise Conflict('局部裁剪超出可靠原图范围')
    scale, border = snapshot['scale'], snapshot['border']
    crop = image.crop(rectangle)
    scaled = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    padded = Image.new('RGB', (scaled.width + border * 2, scaled.height + border * 2), 'white')
    padded.paste(scaled, (border, border))
    folder = require_path(store.root / 'task-results' / task['id'] / ('crop-' + str(time.time_ns())), '局部候选输出')
    folder.mkdir(parents=True)
    path = folder / 'crop.png'
    padded.save(path)
    transform = {'source_bbox': box, 'integer_including_bbox': rectangle, 'scale': scale,
        'white_border_pixels': border, 'resampler': 'Pillow.LANCZOS', 'output_size': list(padded.size),
        'source_coordinate_space': 'exif_normalized_original', 'source_to_crop_affine':
            [scale, 0, border - scale * rectangle[0], 0, scale, border - scale * rectangle[1]],
        'pixels_reconstructed_or_corrected': False}
    receipt = {'crop_path': str(path.relative_to(store.root)),
        'crop_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'transform': transform}
    write_json(folder / 'crop-binding.json', receipt, durable=True)
    with store.transaction() as db:
        row = db.execute('SELECT metadata FROM screen_local_requests WHERE task_id=?', (task['id'],)).fetchone()
        metadata = json.loads(row['metadata'])
        metadata['crop'] = receipt
        db.execute('UPDATE screen_local_requests SET metadata=? WHERE task_id=?', (encoded(metadata), task['id']))
    retain_local_artifact(store, task, folder)
    return path


def retain_local_artifact(store, task, output):
    output = require_path(output, '局部候选产物', role='read')
    if not output.is_relative_to((store.root / 'task-results' / task['id']).resolve()):
        raise ValueError('局部候选产物不属于当前任务')
    paths = {name: output / name for name in ARTIFACT_NAMES - {'crop.png', 'crop-binding.json'}}
    rows = store.rows('SELECT metadata FROM screen_local_requests WHERE task_id=?', (task['id'],))
    crop = json.loads(rows[0]['metadata']).get('crop') if rows else None
    if crop:
        path = store.file(crop['crop_path'])
        paths.update({'crop.png': path, 'crop-binding.json': path.with_name('crop-binding.json')})
    files = {}
    for name, path in paths.items():
        if not path.is_file():
            continue
        resolved = require_path(path, '局部候选产物', role='read')
        if not resolved.is_relative_to((store.root / 'task-results' / task['id']).resolve()):
            raise ValueError('局部候选产物不属于当前任务')
        files[name] = {'path': str(resolved.relative_to(store.root)),
            'sha256': hashlib.sha256(resolved.read_bytes()).hexdigest()}
    with store.transaction() as db:
        db.execute('UPDATE screen_local_requests SET artifact=? WHERE task_id=?', (encoded(files), task['id']))
    return files


def local_candidate_file(manager, project_id, batch_id, request_id, task_id, file_id):
    saved = _receipt(manager, project_id, batch_id, request_id)
    if saved is None or task_id not in saved[1]['task_ids'] or file_id not in ARTIFACT_NAMES:
        raise KeyError('局部候选产物不存在')
    rows = manager.store.rows('''SELECT l.artifact FROM screen_local_requests l JOIN tasks t ON t.id=l.task_id
        WHERE l.task_id=? AND t.project_id=?''', (task_id, project_id))
    files = json.loads(rows[0]['artifact'] or '{}') if rows else {}
    artifact = files.get(file_id)
    if not artifact:
        raise KeyError('局部候选产物尚未生成')
    path = require_path(manager.store.file(artifact['path']), '局部候选产物', role='read')
    expected = (manager.store.root / 'task-results' / task_id).resolve()
    if not path.is_relative_to(expected):
        raise Conflict('局部候选产物路径不属于当前任务')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
        raise Conflict('局部候选产物已变化或不存在')
    return path


def complete_local_candidate(store, task, data, output):
    row = store.rows('SELECT snapshot,metadata FROM screen_local_requests WHERE task_id=?', (task['id'],))[0]
    snapshot, metadata = json.loads(row['snapshot']), json.loads(row['metadata'])
    crop = metadata['crop']
    raw, image = data.get('raw', {}), data.get('image', {})
    if (data.get('status') != 'success' or not isinstance(data.get('text'), str)
            or data.get('recognition_mode') != 'local_text'
            or raw.get('recognition_mode') != 'local_text' or raw.get('layout_applied') is not False
            or raw.get('formatter_applied') is not False or raw.get('truncated') is not False
            or data.get('tables') != [] or image.get('sha256') != crop['crop_sha256']
            or raw.get('input_image_sha256') != image.get('version')
            or [image.get('width'), image.get('height')] != crop['transform']['output_size']
            or raw.get('input_image_size') != crop['transform']['output_size']):
        raise Conflict('局部候选回执没有绑定实际裁剪及文字模式')
    if hashlib.sha256(store.file(crop['crop_path']).read_bytes()).hexdigest() != crop['crop_sha256']:
        raise Conflict('局部候选裁剪像素已变化')
    artifact = retain_local_artifact(store, task, output)
    if (artifact.get('input.png', {}).get('sha256') != image.get('version')
            or not artifact.get('raw.json') or not artifact.get('result.json')
            or json.loads(store.file(artifact['raw.json']['path']).read_text('utf-8')) != raw):
        raise Conflict('局部候选实际输入或原始解码产物与回执不一致')
    from PIL import Image
    with Image.open(store.file(crop['crop_path'])) as crop_image, Image.open(store.file(artifact['input.png']['path'])) as prepared:
        if crop_image.size != prepared.size or crop_image.convert('RGB').tobytes() != prepared.convert('RGB').tobytes():
            raise Conflict('局部候选识别器实际读取的像素与冻结裁剪不一致')
    source_valid = _source_current(store, snapshot)
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        current = source_valid and _current(store, snapshot, db)
        if db.execute("UPDATE tasks SET status='succeeded',phase=?,finished=? WHERE id=? AND status='running'",
            ('局部候选待复核' if current else '版本变化；局部候选为历史建议', now(), task['id'])).rowcount != 1:
            return False
        db.execute('UPDATE screen_local_requests SET candidate=?,artifact=?,candidate_state=? WHERE task_id=?',
            (encoded(data), encoded(artifact), 'available' if current else 'stale', task['id']))
    return True

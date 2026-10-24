"""Explicit human confirmation/correction of byte-identical original cell crops.

Proposals have no review authority. One deliberate action covers exactly its
listed, pinned cells; every target is rechecked before one atomic transaction.
Whole images and business metadata remain unconfirmed.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
import io
import json
import math
import re

from PIL import Image

from ocr_workbench.image_utils import normalized_rgb
from ocr_workbench.store import Conflict, encoded, now

VERSION = 'screen-cell-confirmation-v1'
HEX = re.compile(r'[0-9a-f]{64}\Z')


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def migrate_cell_reviews(db):
    db.execute('''CREATE TABLE IF NOT EXISTS screen_cell_groups(
        group_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES screen_batches(id) ON DELETE CASCADE,
        body TEXT NOT NULL,created TEXT NOT NULL)''')


def axis_box(polygon):
    if not isinstance(polygon, list) or len(polygon) != 4: return None
    try:
        points = [[float(x), float(y)] for x, y in polygon]
        if not all(math.isfinite(v) for p in points for v in p): return None
        xs, ys = sorted({p[0] for p in points}), sorted({p[1] for p in points})
        if len(xs) != 2 or len(ys) != 2 or {tuple(p) for p in points} != {(x, y) for x in xs for y in ys}: return None
        return [math.floor(xs[0]), math.floor(ys[0]), math.ceil(xs[1]), math.ceil(ys[1])]
    except (TypeError, ValueError): return None


def _capture(manager, batch_id, version_id, db):
    row = db.execute('SELECT * FROM screen_items WHERE batch_id=? AND input_version_id=?', (batch_id, version_id)).fetchone()
    if row is None: raise KeyError('单元格不属于当前批次')
    item = dict(row)
    if item['status'] != 'needs_review' or item['review_state'] == 'accepted' or not item['result_id']:
        raise Conflict('仅能集中核对当前待复核结果')
    result = db.execute('SELECT * FROM results WHERE id=?', (item['result_id'],)).fetchone()
    version = db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()
    context = manager._review_context(db, item)
    if (not result or not version or result['revision'] != item['result_revision']
            or version['image_id'] != item['image_id'] or version['sha256'] != item['source_hash']
            or context['active_version'] != version_id or context['selected_result_id'] not in {None, item['result_id']}):
        raise Conflict('原图、采用结果或修订已变化，请重新加载')
    original, edited = json.loads(result['original']), json.loads(result['edited'])
    if original.get('origin') != 'screen_batch': raise Conflict('只有最终屏拍结果可以集中核对')
    binding = {'input_version_id': version_id, 'image_id': item['image_id'], 'source_sha256': item['source_hash'],
        'result_id': item['result_id'], 'result_revision': result['revision'], 'review_binding_token': digest(context),
        'preparation_sha256': digest(item['preparation']), 'evidence_sha256': digest(item.get('evidence'))}
    return {'item': item, 'result': {'id': result['id'], 'revision': result['revision'], 'edited': edited, 'original': original},
            'version': dict(version), 'context': context, 'binding': binding}


def _valid_cells(capture):
    result, binding = capture['result'], capture['binding']
    state = (result['edited'].get('screen') or {}).get('cell_review_state') or {}
    if (state.get('version') != VERSION or state.get('result_revision') != result['revision']
            or any(state.get(key) != binding[key] for key in ('input_version_id', 'source_sha256', 'review_binding_token'))):
        return []
    tables = result['edited'].get('tables', [])
    valid = []
    for part in state.get('cells', []):
        ti, row, column = (part.get(key) for key in ('table', 'row', 'column'))
        if any(type(value) is not int or value < 0 for value in (ti, row, column)) or ti >= len(tables): continue
        cells = [cell for cell in tables[ti]['cells'] if (cell['row'], cell['column']) == (row, column)]
        if len(cells) == 1 and cells[0]['text'] == part.get('confirmed_text'):
            valid.append(deepcopy(part))
    return valid


def review_summary(manager, batch_id, version_id):
    try:
        with manager.store.transaction() as db:
            capture = _capture(manager, batch_id, version_id, db)
    except (Conflict, KeyError):
        return {'version': VERSION, 'confirmed_cells': [], 'confirmed_count': 0, 'binding_current': False}
    confirmed = _valid_cells(capture)
    total = sum(len(table['cells']) for table in capture['result']['edited'].get('tables', []))
    return {'version': VERSION, 'confirmed_cells': confirmed, 'confirmed_count': len(confirmed),
            'remaining_cells': max(0, total - len(confirmed)), 'total_cells': total,
            'binding_current': bool(confirmed), 'whole_image_confirmed': False,
            'business_metadata_confirmed': False}


def _source_pixels(manager, capture):
    path = manager.store.file(capture['version']['path'])
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != capture['binding']['source_sha256']:
        raise Conflict('原图字节已变化，未复用核对')
    with Image.open(io.BytesIO(data)) as image:
        return normalized_rgb(image)


def _members(manager, capture):
    from ocr_workbench.screen_diagnostics import observe_screen_grid_cells
    result, item = capture['result'], capture['item']
    raw = {**deepcopy(result['original']), **deepcopy(result['edited'])}
    raw['screen'] = {**deepcopy(result['original'].get('screen') or {}), **deepcopy(result['edited'].get('screen') or {})}
    prep = json.loads(item['preparation']) if item['preparation'] else {}
    evidence = json.loads(item['evidence']) if item.get('evidence') else {}
    proof = observe_screen_grid_cells(raw, prep, evidence)
    if proof.get('binding', {}).get('input_sha256') != item['source_hash']: return []
    pixels = _source_pixels(manager, capture)
    done = {(p['table'], p['row'], p['column']) for p in _valid_cells(capture)}
    members = []
    for table in proof['tables']:
        if table.get('status') != 'verified': continue
        ti = table['table']
        actual = {(c['row'], c['column'], c.get('row_span', 1), c.get('column_span', 1)): c for c in raw['tables'][ti]['cells']}
        for geometry in table['cells']:
            position = (ti, geometry['row'], geometry['column'])
            if position in done or geometry.get('range_semantics') != 'full_cell': continue
            polygon = geometry.get('source_polygon', geometry.get('polygon'))
            box = axis_box(polygon)
            if (not box or box[0] < 0 or box[1] < 0 or box[2] > pixels.width or box[3] > pixels.height
                    or box[0] >= box[2] or box[1] >= box[3]): continue
            key = (geometry['row'], geometry['column'], geometry.get('row_span', 1), geometry.get('column_span', 1))
            cell = actual[key]
            if not isinstance(cell['text'], str) or not cell['text'].strip(): continue
            crop = pixels.crop(tuple(box))
            members.append({**capture['binding'], 'source_name': item['source_name'], 'table': ti,
                'row': key[0], 'column': key[1], 'row_span': key[2], 'column_span': key[3], 'before': cell['text'],
                'source_polygon': deepcopy(polygon), 'crop_box': box, 'width': crop.width, 'height': crop.height,
                'pixel_sha256': hashlib.sha256(crop.tobytes()).hexdigest(), 'geometry_basis': geometry.get('basis')})
    return members


def list_groups(manager, project_id, batch_id, *, offset=0, limit=8):
    manager._batch(project_id, batch_id)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('集中核对分页范围无效')
    items = manager._items(batch_id)
    grouped, unavailable = defaultdict(list), []
    for item in items:
        try:
            with manager.store.transaction() as db:
                capture = _capture(manager, batch_id, item['input_version_id'], db)
            members = _members(manager, capture)
            for member in members:
                grouped[(member['width'], member['height'], member['pixel_sha256'])].append(member)
            if not members: unavailable.append(item['input_version_id'])
        except (Conflict, OSError, ValueError):
            unavailable.append(item['input_version_id'])
    candidates, conflicting = [], 0
    for members in grouped.values():
        if len({p['source_sha256'] for p in members}) < 2: continue
        if len({(p['before'], p['row_span'], p['column_span']) for p in members}) != 1:
            conflicting += 1; continue
        members.sort(key=lambda p: (p['table'], p['row'], p['column'], p['input_version_id']))
        for start in range(0, len(members), 64):
            scope = members[start:start + 64]
            if len({p['source_sha256'] for p in scope}) < 2: continue
            body = {'version': VERSION, 'batch_id': batch_id, 'project_id': project_id, 'members': scope}
            candidates.append({'group_id': digest(body), 'literal': scope[0]['before'], 'occurrences': len(scope),
                'members': scope, 'body': body, 'requires_explicit_confirmation': True})
    candidates.sort(key=lambda g: g['group_id'])
    page = candidates[offset:offset + limit]
    with manager.store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        for group in page:
            for member in group['members']:
                if _capture(manager, batch_id, member['input_version_id'], db)['binding'] != {
                        key: member[key] for key in ('input_version_id', 'image_id', 'source_sha256', 'result_id',
                            'result_revision', 'review_binding_token', 'preparation_sha256', 'evidence_sha256')}:
                    raise Conflict('集中核对准备期间结果已变化，请重新加载')
            db.execute('INSERT OR IGNORE INTO screen_cell_groups VALUES(?,?,?,?)',
                       (group['group_id'], batch_id, encoded(group['body']), now()))
            group.pop('body')
            group['preview_url'] = f'/api/projects/{project_id}/screen-batches/{batch_id}/cell-groups/{group["group_id"]}/image'
    return {'version': VERSION, 'batch_id': batch_id, 'groups': page, 'total_groups': len(candidates),
        'offset': offset, 'limit': limit, 'total_inputs': len(items), 'unavailable_inputs': len(unavailable),
        'conflicting_pixel_groups_rejected': conflicting, 'automatic_confirmations': 0,
        'potential_repeated_checks_after_confirmation': sum(g['occurrences'] - 1 for g in candidates)}


def _proposal(manager, project_id, batch_id, group_id, db):
    manager._batch(project_id, batch_id)
    if not isinstance(group_id, str) or not HEX.fullmatch(group_id): raise ValueError('单元格组编号无效')
    row = db.execute('SELECT body FROM screen_cell_groups WHERE group_id=? AND batch_id=?', (group_id, batch_id)).fetchone()
    if not row: raise KeyError('集中核对候选不存在，请重新加载')
    body = json.loads(row['body'])
    if body['project_id'] != project_id or digest(body) != group_id: raise Conflict('集中核对候选来源绑定无效')
    return body


def preview(manager, project_id, batch_id, group_id):
    with manager.store.transaction() as db:
        body = _proposal(manager, project_id, batch_id, group_id, db)
        member = body['members'][0]
        capture = _capture(manager, batch_id, member['input_version_id'], db)
    if capture['binding']['source_sha256'] != member['source_sha256']: raise Conflict('原图版本已变化')
    crop = _source_pixels(manager, capture).crop(tuple(member['crop_box']))
    if hashlib.sha256(crop.tobytes()).hexdigest() != member['pixel_sha256']: raise Conflict('原图格位像素已变化')
    output = io.BytesIO(); crop.save(output, format='PNG'); return output.getvalue()


def confirm(manager, project_id, batch_id, body, *, allow_new=True):
    if (not isinstance(body, dict) or set(body) != {'request_id', 'group_id', 'after', 'apply_to_all_listed_cells'}
            or not isinstance(body.get('request_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,120}', body['request_id'])
            or body.get('apply_to_all_listed_cells') is not True
            or not isinstance(body.get('after'), str) or not body['after'].strip() or len(body['after']) > 16000):
        raise ValueError('请明确核对整组原图、确认文字和列出的全部格位')
    manager._batch(project_id, batch_id)
    request_id, payload_hash = body['request_id'], digest({'cell_confirmation_version': VERSION, **body})
    with manager.guard, manager.store.file_lock:
        with manager.store.transaction() as db:
            old = db.execute('SELECT * FROM screen_actions WHERE batch_id=? AND request_id=?', (batch_id, request_id)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash: raise Conflict('请求编号已绑定不同的集中核对操作')
                return json.loads(old['receipt'])
            if not allow_new: return None
            proposal = _proposal(manager, project_id, batch_id, body['group_id'], db)
        # Recompute current source geometry and exact pixels before taking the
        # write transaction. The commit then compares all captured identities.
        captures = {}
        for member in proposal['members']:
            vid = member['input_version_id']
            if vid not in captures:
                with manager.store.transaction() as db: captures[vid] = _capture(manager, batch_id, vid, db)
                captures[vid]['current_members'] = _members(manager, captures[vid])
            if member not in captures[vid]['current_members']:
                raise Conflict('原图、格位、结果或复核状态已变化；整组未保存')
        stamp, committed = now(), []
        with manager.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM screen_actions WHERE batch_id=? AND request_id=?', (batch_id, request_id)).fetchone()
            if old:
                if old['payload_hash'] != payload_hash: raise Conflict('请求编号已绑定其他操作')
                return json.loads(old['receipt'])
            for vid, captured in captures.items():
                if _capture(manager, batch_id, vid, db)['binding'] != captured['binding']:
                    raise Conflict('保存前结果或人工确认已变化；整组未保存')
                _source_pixels(manager, captured)
            for vid, captured in captures.items():
                result, item = captured['result'], captured['item']
                value = deepcopy(result['edited'])
                confirmed = _valid_cells(captured)
                targets = [p for p in proposal['members'] if p['input_version_id'] == vid]
                for target in targets:
                    cells = [c for c in value['tables'][target['table']]['cells']
                             if (c['row'], c['column']) == (target['row'], target['column'])]
                    if len(cells) != 1 or cells[0]['text'] != target['before']: raise Conflict('单元格内容已变化；整组未保存')
                    cells[0]['text'] = body['after']
                    confirmed.append({**deepcopy(target), 'confirmed_text': body['after'], 'group_id': body['group_id'],
                                      'request_id': request_id, 'actor': 'human', 'confirmed_at': stamp})
                revision = result['revision'] + 1
                context = deepcopy(captured['context'])
                context.update(selected_result_id=result['id'], review_result_id=result['id'], review_version_id=vid,
                               review_revision=revision, human_review_status='question', review_epoch=stamp)
                value.setdefault('screen', {})['cell_review_state'] = {
                    'version': VERSION, 'result_revision': revision, 'input_version_id': vid,
                    'source_sha256': item['source_hash'], 'review_binding_token': digest(context), 'cells': confirmed}
                screen = value.pop('screen')
                manager.store._save_in_transaction(db, result['id'], value, result['revision'], screen_metadata=screen)
                db.execute('INSERT INTO selections VALUES(?,?) ON CONFLICT(image_id) DO UPDATE SET result_id=excluded.result_id',
                           (item['image_id'], result['id']))
                db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?) ON CONFLICT(image_id) DO UPDATE SET result_id=excluded.result_id,version_id=excluded.version_id,revision=excluded.revision,status=excluded.status,updated=excluded.updated',
                           (item['image_id'], result['id'], vid, revision, 'question', stamp))
                db.execute("UPDATE screen_items SET result_revision=?,review_state='retain',status='needs_review',stage='cell_review',updated=? WHERE batch_id=? AND input_version_id=?",
                           (revision, stamp, batch_id, vid))
                if manager._review_context(db, item) != context: raise Conflict('保存期间复核绑定变化；整组未保存')
                committed.append({'input_version_id': vid, 'result_id': result['id'], 'result_revision': revision,
                                  'confirmed_cells': len(targets), 'whole_image_confirmed': False})
            receipt = {'cell_confirmation_version': VERSION, 'request_id': request_id, 'group_id': body['group_id'],
                'batch_id': batch_id, 'project_id': project_id, 'status': 'committed', 'after': body['after'],
                'scope_cells': len(proposal['members']), 'scope_images': len(captures), 'items': committed,
                'actor': 'human', 'explicit_scope_consent': True, 'whole_images_confirmed': False,
                'business_metadata_confirmed': False, 'created': stamp}
            db.execute('INSERT INTO screen_actions VALUES(?,?,?,?)', (batch_id, request_id, payload_hash, encoded(receipt)))
        return receipt

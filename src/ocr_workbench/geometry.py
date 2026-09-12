"""Conservative cell correspondence and immutable, version-bound geometry evidence."""

from collections import Counter
from copy import deepcopy
import json
import math
import time

from ocr_workbench.coordinates import bounds, box_polygon, validate_polygon
from ocr_workbench.document_store import fingerprint, structure_fingerprint
from ocr_workbench.store import uid, now, encoded, Conflict
from ocr_workbench.tables import parse_tables


def iou(a, b):
    intersection = max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))
    union = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union > 0 else 0


def topology(table):
    return (table['rows'], table['columns'], [(c['row'], c['column'], c['row_span'], c['column_span']) for c in table['cells']])


def conservative_mapping(edit, prediction, width, height):
    """Only map a unique structure with actual detector support and text anchors.

    Equal cell counts alone cannot establish a mapping. Postprocessed cells are
    kept as evidence but unsupported/supplemented boxes downgrade to a table.
    """
    parsed = []
    for item in prediction.get('tables', []):
        try:
            tables = parse_tables(item['final']['pred_html'])
        except ValueError:
            # Raw malformed structure stays in the artifact; no guessed cells.
            continue
        if len(tables) == 1:
            parsed.append((item, tables[0]))
    mappings = []
    normalize = lambda text: ''.join(text.split())
    used = set()
    for table_index, table in enumerate(edit.get('tables', [])):
        expected = Counter(normalize(c['text']) for c in table['cells'] if c['text'].strip())
        candidates = []
        for pred_index, (item, predicted) in enumerate(parsed):
            actual = Counter(normalize(c['text']) for c in predicted['cells'] if c['text'].strip())
            shared = sum((expected & actual).values()) / max(1, sum(expected.values()))
            if topology(table) == topology(predicted) and shared >= .6:
                candidates.append((shared, pred_index, item, predicted))
        candidates.sort(key=lambda x: x[0], reverse=True)
        chosen = candidates[0] if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]+.2) else None
        if chosen and chosen[1] in used:
            chosen = None
        if chosen:
            used.add(chosen[1])
            _, pred_index, item, predicted = chosen
            table_polygon = box_polygon(item['table_box'])
            reason = None
        else:
            # With multiple ambiguous tables even a table-level guess is unsafe.
            item = parsed[0][0] if len(parsed) == len(edit.get('tables', [])) == 1 else None
            table_polygon = box_polygon(item['table_box']) if item else None
            reason = 'structure_or_table_identity_ambiguous'
        try:
            if table_polygon:
                validate_polygon(table_polygon, width, height)
        except ValueError:
            table_polygon = None
        mappings.append({'target': {'kind': 'table', 'table': table_index}, 'polygon': table_polygon,
                         'level': 'region' if table_polygon else 'image', 'reason': reason or 'table_region',
                         'region_id': item.get('region_id') if item else None})
        direct_used = set()
        for cell_index, cell in enumerate(table['cells']):
            entry = {'target': {'kind': 'cell', 'table': table_index, 'row': cell['row'], 'column': cell['column']},
                     'table_polygon': table_polygon,
                     'polygon': table_polygon, 'level': 'region' if table_polygon else 'image',
                     'reason': reason or 'no_reliable_cell_correspondence', 'region_id': item.get('region_id') if item else None}
            if chosen:
                boxes = item['final']['cell_box_list']
                if len(boxes) == len(predicted['cells']):
                    box = boxes[cell_index]
                    px, py = item['table_box'][:2]
                    raw_boxes = item.get('raw', {}).get('det', {}).get('boxes', [])
                    detectors = [(n, detection) for n, detection in enumerate(raw_boxes)
                        if detection.get('score', 0) >= .5 and iou(box, [detection['coordinate'][0]+px,
                            detection['coordinate'][1]+py, detection['coordinate'][2]+px, detection['coordinate'][3]+py]) >= .95]
                    value = normalize(cell['text'])
                    agreed = normalize(predicted['cells'][cell_index]['text']) == value
                    unique = bool(value) and expected[value] == 1
                    # Repeated/empty values require a unique agreeing anchor in
                    # both their row and column; row/column index is not evidence.
                    row_anchor = any(c['row'] == cell['row'] and c['column'] != cell['column'] and
                        normalize(c['text']) and expected[normalize(c['text'])] == 1 and
                        normalize(c['text']) == normalize(predicted['cells'][n]['text']) for n, c in enumerate(table['cells']))
                    column_anchor = any(c['column'] == cell['column'] and c['row'] != cell['row'] and
                        normalize(c['text']) and expected[normalize(c['text'])] == 1 and
                        normalize(c['text']) == normalize(predicted['cells'][n]['text']) for n, c in enumerate(table['cells']))
                    try:
                        poly = validate_polygon(box_polygon(box), width, height)
                    except ValueError:
                        poly = None
                    if len(detectors) == 1 and detectors[0][0] not in direct_used and poly and agreed and (unique or row_anchor and column_anchor):
                        direct_used.add(detectors[0][0])
                        entry.update(polygon=poly, level='cell', reason='unique_structure_text_and_detection',
                                     prediction_table=pred_index, detection_index=detectors[0][0], geometry_origin='direct_detection')
                    elif len(detectors) != 1:
                        entry['reason'] = 'postprocessed_box_without_unique_detection_support'
                    elif not agreed:
                        entry['reason'] = 'text_anchor_disagrees'
                    else:
                        entry['reason'] = 'repeated_or_empty_content_ambiguous'
            mappings.append(entry)
    return mappings


def enqueue_geometry(store, result_id, revision, *, region_ids=None, force=False):
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM results WHERE id=?', (result_id,)).fetchone()
        if row is None:
            raise KeyError('识别结果不存在')
        if row['revision'] != revision:
            raise Conflict('文字修订已变化，请保存后重新请求定位')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
        raw, edit = json.loads(row['original']), json.loads(row['edited'])
        version_id = raw.get('project_image_version') or task['version_id']
        version = db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()
        if version is None:
            raise ValueError('结果没有可靠的图像版本')
        image = db.execute('SELECT * FROM images WHERE id=?', (task['image_id'],)).fetchone()
        if image['active_version'] != version_id:
            raise Conflict('请切回识别结果对应的图像版本后补充定位')
        structure = structure_fingerprint(edit)
        cached = db.execute("SELECT id FROM geometry_evidence WHERE result_id=? AND version_id=? AND structure_sha256=? AND status='valid' AND source='paddle-table-v2' LIMIT 1", (result_id, version_id, structure)).fetchone()
        if cached and not force:
            return {'cached': True, 'evidence_id': cached['id']}
        previous = db.execute("""SELECT t.id,t.phase FROM tasks t JOIN geometry_requests g ON g.task_id=t.id
            WHERE g.result_id=? AND t.version_id=? AND t.status='succeeded' AND g.structure_sha256=?
            AND json_extract(g.snapshot,'$.revision')=? ORDER BY t.finished DESC LIMIT 1""", (result_id,version_id,structure,revision)).fetchone()
        if previous and not force:
            return {'cached': True, 'task_id': previous['id'], 'phase': previous['phase']}
        pending = db.execute("SELECT t.id FROM tasks t JOIN geometry_requests g ON g.task_id=t.id WHERE g.result_id=? AND t.status IN ('queued','running','paused','interrupted')", (result_id,)).fetchone()
        if pending:
            return {'cached': False, 'task_id': pending['id']}
        regions = [dict(r) for r in db.execute("SELECT * FROM regions WHERE version_id=? AND kind='table' ORDER BY reading_order", (version_id,))]
        if region_ids is not None:
            if not isinstance(region_ids, list) or not region_ids or not set(region_ids) <= {r['id'] for r in regions}:
                raise ValueError('表格区域不属于当前版本')
            regions = [r for r in regions if r['id'] in region_ids]
        regions = [{'id': r['id'], 'polygon': json.loads(r['polygon']), 'source': r['source']} for r in regions]
        if not regions:
            regions = [{'polygon': b['polygon'], 'source': raw.get('engine')} for b in raw.get('blocks', []) if b.get('kind') == 'table' and b.get('polygon')]
        ocr_blocks, ocr_source = [], None
        if raw.get('engine') == 'ppocr' or raw.get('origin') == 'document':
            ocr_blocks, ocr_source = raw.get('blocks', []), result_id
        if not ocr_blocks:
            prior = db.execute("SELECT r.id,r.original FROM results r JOIN tasks t ON t.id=r.task_id WHERE t.image_id=? AND t.version_id=? AND t.engine='ppocr' AND t.status='succeeded' ORDER BY t.created DESC LIMIT 1", (task['image_id'], version_id)).fetchone()
            if prior:
                ocr_blocks, ocr_source = json.loads(prior['original']).get('blocks', []), prior['id']
        key = uid()
        snapshot = {'edited': edit, 'revision': revision, 'image_sha256': version['sha256'],
                    'ocr_blocks': ocr_blocks, 'ocr_source': ocr_source}
        snapshot['native_structure'] = raw.get('origin') == 'document' and raw['text'] == edit['text'] and not edit.get('tables') and any(b.get('source') == 'pdf-native' for b in ocr_blocks)
        db.execute("""INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind)
            VALUES(?,?,?,?,? ,?,'queued','等待补充表格定位',?,'geometry')""",
            (key, task['project_id'], task['image_id'], version_id, 'geometry', 'geometry-'+key, now()))
        db.execute('INSERT INTO geometry_requests VALUES(?,?,?,?,?)', (key, result_id, encoded(snapshot), structure, encoded(regions)))
        return {'cached': False, 'task_id': key}


def complete_geometry(store, task, prediction, artifact):
    request = store.rows('SELECT * FROM geometry_requests WHERE task_id=?', (task['id'],))[0]
    snapshot = json.loads(request['snapshot'])
    version = store.one('versions', task['version_id'])
    mappings = conservative_mapping(snapshot['edited'], prediction, version['width'], version['height'])
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()[0] != 'running':
            return False
        current = db.execute('SELECT edited FROM results WHERE id=?', (request['result_id'],)).fetchone()
        valid = structure_fingerprint(json.loads(current['edited'])) == request['structure_sha256']
        db.execute("UPDATE geometry_evidence SET status='superseded' WHERE result_id=? AND version_id=? AND source='paddle-table-v2' AND status='valid'", (request['result_id'], task['version_id']))
        for mapping in mappings:
            details = {**mapping, 'artifact': str(artifact.relative_to(store.root)),
                       'inference_seconds': prediction['inference_seconds'], 'contributes_to_votes': False}
            db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (uid(), request['result_id'], task['version_id'], mapping.get('region_id'), version['sha256'],
                 request['structure_sha256'], 'paddle-table-v2', encoded(prediction['model_revisions']),
                 encoded(mapping['target']), encoded(mapping['polygon']) if mapping['polygon'] else None,
                 encoded(details), 'valid' if valid else 'stale', now()))
        db.execute("UPDATE tasks SET status='succeeded',phase=?,finished=? WHERE id=?", ('定位完成' if valid else '结构已变更，定位过期', now(), task['id']))
        if valid and snapshot.get('native_structure'):
            from ocr_workbench.native_tables import native_table_preview
            from ocr_workbench.store import history_encoded
            source = db.execute('SELECT * FROM results WHERE id=?', (request['result_id'],)).fetchone()
            # A preview is derived only from the exact requested text revision.
            if source['revision'] == snapshot['revision']:
                raw = json.loads(source['original'])
                raw['text'],raw['tables'] = snapshot['edited']['text'],snapshot['edited']['tables']
                preview = native_table_preview(raw,prediction,version['width'],version['height'])
                if preview:
                    key, result_key = uid(),uid()
                    preview['document']['structure_parent_result']=request['result_id']
                    preview['document']['geometry_artifact']=str(artifact.relative_to(store.root))
                    db.execute("""INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,finished,result_id,kind)
                        VALUES(?,?,?,?,?,?,'succeeded','原生表格结构预览',?,?,?,'document')""",
                        (key,task['project_id'],task['image_id'],task['version_id'],'pdf-native','geometry-'+task['id'],now(),now(),result_key))
                    edit={'text':preview['text'],'tables':preview['tables']}
                    db.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?)',(result_key,key,encoded(preview),encoded(edit),0,0,now()))
                    db.execute('INSERT INTO edits VALUES(?,?,?,?)',(result_key,0,history_encoded(edit),now()))
                    for mapping in conservative_mapping(edit,prediction,version['width'],version['height']):
                        db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (uid(),result_key,task['version_id'],mapping.get('region_id'),version['sha256'],structure_fingerprint(edit),'paddle-table-v2',encoded(prediction['model_revisions']),encoded(mapping['target']),encoded(mapping['polygon']) if mapping['polygon'] else None,encoded(dict(mapping,artifact=str(artifact.relative_to(store.root)),contributes_to_votes=False)),'valid',now()))
                    db.execute("UPDATE tasks SET phase='定位完成；原生表格预览待采用' WHERE id=?",(task['id'],))
                else:
                    db.execute("UPDATE tasks SET phase='定位完成；未发现可可靠填充的原生表格' WHERE id=?",(task['id'],))
    return True


def reconcile_geometry(db, result_id, before, after):
    from ocr_workbench.review_issues import relocate_text
    changed = structure_fingerprint(before) != structure_fingerprint(after)
    for row in db.execute("SELECT id,target FROM geometry_evidence WHERE result_id=? AND status='valid'", (result_id,)).fetchall():
        target = json.loads(row['target'])
        stale = changed and target.get('kind') in ('cell', 'table')
        if target.get('kind') == 'text':
            target, stable = relocate_text(before['text'], after['text'], target)
            stale = target.get('unlocatable', False)
        db.execute('UPDATE geometry_evidence SET target=?,status=? WHERE id=?', (encoded(target), 'stale' if stale else 'valid', row['id']))


def bind_manual(store, result_id, body):
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        result = store.result(result_id)
        if result['revision'] != body.get('revision'):
            raise Conflict('修订已变化，请重新读取后绑定')
        task = store.one('tasks', result['task_id'])
        version_id = result['original'].get('project_image_version') or task['version_id']
        if body.get('version_id') != version_id or store.one('images', task['image_id'])['active_version'] != version_id:
            raise Conflict('人工定位必须绑定当前结果的图像版本')
        version = store.one('versions', version_id)
        poly = validate_polygon(body.get('polygon'), version['width'], version['height'])
        target = body.get('target', {})
        if target.get('kind') == 'cell':
            tables = result['edited']['tables']
            if type(target.get('table')) is not int or not 0 <= target['table'] < len(tables) or not any(c['row'] == target.get('row') and c['column'] == target.get('column') for c in tables[target['table']]['cells']):
                raise ValueError('人工绑定的单元格不存在')
            target = {k: target[k] for k in ('kind', 'table', 'row', 'column')}
        elif target.get('kind') == 'text':
            if any(type(target.get(k)) is not int for k in ('start', 'end')) or not 0 <= target['start'] < target['end'] <= len(result['edited']['text']):
                raise ValueError('人工绑定的文字范围无效')
            target = {k: target[k] for k in ('kind', 'start', 'end')}
        else:
            raise ValueError('请选择单元格或文字范围进行人工定位')
        key = uid()
        db.execute("UPDATE geometry_evidence SET status='superseded' WHERE result_id=? AND target=? AND source='manual' AND status='valid'", (result_id, encoded(target)))
        db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key, result_id, version_id, None, version['sha256'], structure_fingerprint(result['edited']), 'manual', 'human',
             encoded(target), encoded(poly), encoded({'level': 'cell' if target['kind'] == 'cell' else 'region', 'reason': 'manual_binding'}), 'valid', now()))
        return {'evidence_id': key, 'source': 'manual'}


def geometry_view(store, result_id, target=None, *, db=None):
    started = time.perf_counter()
    if db is None:
        with store.transaction() as connection:
            return geometry_view(store, result_id, target, db=connection)
    row = db.execute('SELECT r.*,t.version_id FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=?', (result_id,)).fetchone()
    if not row:
        raise KeyError('识别结果不存在')
    raw, edit = json.loads(row['original']), json.loads(row['edited'])
    version_id = raw.get('project_image_version') or row['version_id']
    structure = structure_fingerprint(edit)
    result = []
    clauses, params = ["result_id=?", "version_id=?", "status='valid'"], [result_id, version_id]
    if target is not None:
        # The serialized targets are canonical within this store. Filter before
        # loading payloads so one focused cell never decodes every table cell.
        clauses.append('target=?')
        keys = ('kind', 'table', 'row', 'column') if target.get('kind') == 'cell' else ('kind', 'table') if target.get('kind') == 'table' else ('kind', 'start', 'end')
        params.append(encoded({key: target[key] for key in keys if key in target}))
    for evidence in db.execute("SELECT * FROM geometry_evidence WHERE " + ' AND '.join(clauses) + " ORDER BY source='manual' DESC,created DESC", params):
        item = dict(evidence)
        if item['structure_sha256'] != structure and json.loads(item['target']).get('kind') != 'text':
            continue
        item['target'], item['details'] = json.loads(item['target']), json.loads(item['details'])
        item['polygon'] = json.loads(item['polygon']) if item['polygon'] else None
        if target is None or item['target'] == target:
            result.append(item)
    return {'result_id': result_id, 'revision': row['revision'], 'version_id': version_id,
            'experimental': True, 'evidence': result, 'cache_read_ms': (time.perf_counter()-started)*1000}

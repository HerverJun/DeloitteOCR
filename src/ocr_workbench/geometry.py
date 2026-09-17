"""Conservative cell correspondence and immutable, version-bound geometry evidence."""

from collections import Counter
from copy import deepcopy
import json
import math
import hashlib
from pathlib import Path
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


def geometry_source(provider, algorithm):
    if provider == 'paddle':
        return 'paddle-table-v2' if algorithm == 'legacy' else 'paddle-table-' + algorithm
    return provider + '-' + algorithm


def enqueue_geometry(store, result_id, revision, *, region_ids=None, force=False, algorithm=None, provider='paddle'):
    from ocr_workbench.table_matching import policy_for_algorithm, policy_identity
    from ocr_workbench.geometry_provider_config import provider_identity
    policy = policy_for_algorithm(algorithm)
    algorithm = policy['algorithm']
    if algorithm == 'legacy' and provider != 'paddle':
        raise ValueError('旧对应器仅支持 Paddle，实验提供方请选择 local-v2 或 local-v3')
    selected_provider = provider_identity(provider)
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
        model_config = Path(__file__).resolve().parents[2] / 'config/table-model-lock.json'
        candidate_identity = {'image_sha256': version['sha256'], 'image_version': version_id,
            'geometry_provider': selected_provider,
            'regions': regions, 'ocr_blocks': ocr_blocks, 'ocr_source': ocr_source,
            'model_lock_sha256': hashlib.sha256(model_config.read_bytes()).hexdigest(),
            'provider_code_sha256': fingerprint({name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ('table_geometry_worker.py','geometry_providers.py','geometry_contract.py','coordinates.py')})}
        candidate_key = fingerprint(candidate_identity)
        snapshot = {'edited': edit, 'revision': revision, 'image_sha256': version['sha256'],
                    'image_version': version_id, 'ocr_blocks': ocr_blocks, 'ocr_source': ocr_source,
                    'policy': policy, **policy_identity(policy), 'algorithm': algorithm,
                    'geometry_provider': provider, 'provider_identity': selected_provider,
                    'candidate_cache_key': candidate_key, 'text_snapshot_sha256': fingerprint(edit)}
        snapshot['mapping_cache_key'] = fingerprint({'candidate_key': candidate_key, 'edit': edit,
            'revision': revision, 'algorithm': snapshot['algorithm_version'], 'algorithm_sha256':snapshot['algorithm_sha256'],'policy': policy})
        request_key = snapshot['mapping_cache_key']
        if not force:
            cached = db.execute("SELECT id FROM geometry_evidence WHERE result_id=? AND version_id=? AND status='valid' AND json_extract(details,'$.mapping_cache_key')=? LIMIT 1", (result_id,version_id,request_key)).fetchone()
            if cached:
                return {'cached': True, 'evidence_id': cached['id']}
            previous = db.execute("""SELECT t.id,t.phase FROM tasks t JOIN geometry_requests g ON g.task_id=t.id
                WHERE g.result_id=? AND t.status='succeeded' AND json_extract(g.snapshot,'$.mapping_cache_key')=?
                ORDER BY t.finished DESC LIMIT 1""", (result_id,request_key)).fetchone()
            if previous:
                return {'cached': True, 'task_id': previous['id'], 'phase': previous['phase']}
        pending = db.execute("""SELECT t.id,g.snapshot FROM tasks t JOIN geometry_requests g ON g.task_id=t.id
            WHERE g.result_id=? AND t.status IN ('queued','running','paused','interrupted')""", (result_id,)).fetchall()
        for item in pending:
            if json.loads(item['snapshot']).get('mapping_cache_key') == request_key:
                return {'cached': False, 'task_id': item['id']}
            db.execute("UPDATE tasks SET status='cancelled',phase='输入快照已更新',finished=? WHERE id=?", (now(),item['id']))
        # The model cache is independent of adopted edits/policy. Preserve the
        # original artifact and verify its bytes again in the CPU replay path.
        prior = db.execute("""SELECT details FROM geometry_evidence WHERE version_id=?
            AND json_extract(details,'$.candidate_cache_key')=? ORDER BY created DESC LIMIT 1""", (version_id,candidate_key)).fetchone()
        if prior:
            details = json.loads(prior['details'])
            if details.get('artifact_sha256'):
                snapshot['candidate_artifact'] = {'path': details['artifact'], 'sha256': details['artifact_sha256']}
        key = uid()
        snapshot['native_structure'] = raw.get('origin') == 'document' and raw['text'] == edit['text'] and not edit.get('tables') and any(b.get('source') == 'pdf-native' for b in ocr_blocks)
        db.execute("""INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind)
            VALUES(?,?,?,?,? ,?,'queued','等待补充表格定位',?,'geometry')""",
            (key, task['project_id'], task['image_id'], version_id, 'geometry', 'geometry-'+key, now()))
        db.execute('INSERT INTO geometry_requests VALUES(?,?,?,?,?)', (key, result_id, encoded(snapshot), structure, encoded(regions)))
        return {'cached': False, 'task_id': key}


def complete_geometry(store, task, prediction, artifact, *, candidate_cache_hit=False):
    if store.one('tasks', task['id'])['status'] != 'running':
        return False
    request = store.rows('SELECT * FROM geometry_requests WHERE task_id=?', (task['id'],))[0]
    snapshot = json.loads(request['snapshot'])
    version = store.one('versions', task['version_id'])
    algorithm = snapshot.get('algorithm','legacy')
    provider = snapshot.get('geometry_provider', 'paddle')
    source_name = geometry_source(provider, algorithm)
    if provider != 'paddle':
        from ocr_workbench.geometry_contract import fingerprint as contract_fingerprint
        from ocr_workbench.geometry_provider_config import verify_provider
        if prediction.get('component') != provider or prediction.get('model_revisions') != verify_provider(provider):
            raise ValueError('几何产物提供方或模型锁不一致')
        if prediction.get('image_sha256') != snapshot['image_sha256'] or prediction.get('image_version') != task['version_id']:
            raise ValueError('几何产物不属于当前图像')
        if snapshot.get('ocr_blocks') and prediction.get('ocr_blocks') != snapshot['ocr_blocks']:
            raise ValueError('几何产物共享 OCR 不一致')
        if prediction.get('shared_ocr_sha256') != contract_fingerprint(prediction.get('ocr_blocks', [])):
            raise ValueError('几何产物 OCR 哈希不一致')
        upstream = prediction.get('upstream_artifact', {})
        if upstream.get('path') != 'upstream.json':
            raise ValueError('缺少原始几何产物')
        raw_path = artifact.parent / 'upstream.json'
        if not raw_path.is_file() or hashlib.sha256(raw_path.read_bytes()).hexdigest() != upstream.get('sha256'):
            raise ValueError('原始几何产物哈希不一致')
    if algorithm in ('local-v2', 'local-v3'):
        from ocr_workbench.table_matching import local_mapping
        mappings = local_mapping(snapshot['edited'], prediction, version['width'], version['height'],
            policy=snapshot['policy'], result_id=request['result_id'], revision=snapshot['revision'],
            image_version=task['version_id'], ocr_blocks=prediction.get('ocr_blocks',snapshot.get('ocr_blocks',[])))
    else:
        mappings = conservative_mapping(snapshot['edited'], prediction, version['width'], version['height'])
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact.exists() else None
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()[0] != 'running':
            return False
        current = db.execute('SELECT edited,revision FROM results WHERE id=?', (request['result_id'],)).fetchone()
        active = db.execute('SELECT active_version FROM images WHERE id=?',(task['image_id'],)).fetchone()
        valid = (current['revision'] == snapshot['revision'] and fingerprint(json.loads(current['edited'])) == fingerprint(snapshot['edited'])
            and structure_fingerprint(json.loads(current['edited'])) == request['structure_sha256']
            and active['active_version'] == task['version_id'] and version['sha256'] == snapshot['image_sha256']
            and prediction.get('image_version') in (None,task['version_id']))
        if valid:
            db.execute("UPDATE geometry_evidence SET status='superseded' WHERE result_id=? AND version_id=? AND source=? AND status='valid'", (request['result_id'], task['version_id'], source_name))
            from ocr_workbench.structure_store import record_candidates
            record_candidates(db, request['result_id'], version, prediction,
                prediction.get('ocr_blocks', snapshot.get('ocr_blocks', [])),
                source_result=prediction.get('ocr_source') or snapshot.get('ocr_source') or request['result_id'],
                artifact=str(artifact.relative_to(store.root)))
        for mapping in mappings:
            details = {**mapping, 'artifact': str(artifact.relative_to(store.root)),
                       'inference_seconds': prediction.get('inference_seconds'), 'contributes_to_votes': False,
                       'geometry_provider': provider, 'algorithm': algorithm, 'candidate_cache_hit': candidate_cache_hit,
                       'context_seconds': prediction.get('context_seconds'), 'load_seconds': prediction.get('load_seconds'),
                       'artifact_sha256': artifact_sha256, 'candidate_cache_key': snapshot.get('candidate_cache_key'),
                       'mapping_cache_key': snapshot.get('mapping_cache_key'), 'policy_sha256': snapshot.get('policy_sha256'),
                       'anchor_snapshot': mapping.get('anchor_snapshot') or {'revision': snapshot['revision'], 'text_sha256': fingerprint(snapshot['edited'])}}
            db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (uid(), request['result_id'], task['version_id'], mapping.get('region_id'), version['sha256'],
                 request['structure_sha256'], source_name, encoded(prediction.get('model_revisions',{})),
                 encoded(mapping['target']), encoded(mapping['polygon']) if mapping['polygon'] else None,
                 encoded(details), 'valid' if valid else 'stale', now()))
        db.execute("UPDATE tasks SET status='succeeded',phase=?,finished=? WHERE id=?", ('定位完成' if valid else '文字、结构或图像版本已变更，定位过期', now(), task['id']))
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
                    record_candidates(db, result_key, version, prediction,
                        prediction.get('ocr_blocks', snapshot.get('ocr_blocks', [])),
                        source_result=prediction.get('ocr_source') or snapshot.get('ocr_source') or request['result_id'],
                        artifact=str(artifact.relative_to(store.root)))
                    preview_mappings = local_mapping(edit,prediction,version['width'],version['height'],policy=snapshot['policy'],
                        result_id=result_key,revision=0,image_version=task['version_id'],ocr_blocks=prediction.get('ocr_blocks', snapshot.get('ocr_blocks',[]))) if algorithm in ('local-v2', 'local-v3') else conservative_mapping(edit,prediction,version['width'],version['height'])
                    for mapping in preview_mappings:
                        db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (uid(),result_key,task['version_id'],mapping.get('region_id'),version['sha256'],structure_fingerprint(edit),source_name,encoded(prediction['model_revisions']),encoded(mapping['target']),encoded(mapping['polygon']) if mapping['polygon'] else None,encoded(dict(mapping,artifact=str(artifact.relative_to(store.root)),contributes_to_votes=False,policy_sha256=snapshot.get('policy_sha256'),geometry_provider=provider,algorithm=algorithm)),'valid',now()))
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
        from ocr_workbench.geometry_contract import evidence_v2
        manual_details = evidence_v2(content_polygons=[poly] if target['kind']=='text' else [],
            cell_polygons=[poly] if target['kind']=='cell' else [],display_polygon=poly,origin='manual',reason='manual_binding',
            anchor_snapshot={'revision':result['revision'],'text_sha256':fingerprint(result['edited'])})
        db.execute("UPDATE geometry_evidence SET status='superseded' WHERE result_id=? AND target=? AND source='manual' AND status='valid'", (result_id, encoded(target)))
        db.execute('INSERT INTO geometry_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (key, result_id, version_id, None, version['sha256'], structure_fingerprint(result['edited']), 'manual', 'human',
             encoded(target), encoded(poly), encoded(manual_details), 'valid', now()))
        return {'evidence_id': key, 'source': 'manual'}


def geometry_view(store, result_id, target=None, *, db=None, provider=None, algorithm=None):
    started = time.perf_counter()
    if db is None:
        with store.transaction() as connection:
            return geometry_view(store, result_id, target, db=connection, provider=provider, algorithm=algorithm)
    row = db.execute('SELECT r.*,t.version_id FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=?', (result_id,)).fetchone()
    if not row:
        raise KeyError('识别结果不存在')
    raw, edit = json.loads(row['original']), json.loads(row['edited'])
    version_id = raw.get('project_image_version') or row['version_id']
    structure = structure_fingerprint(edit)
    from ocr_workbench.table_matching import policy_for_algorithm,matching_code_fingerprint
    if provider is None and algorithm is None:
        remapped = db.execute("""SELECT details,created FROM geometry_evidence WHERE result_id=? AND version_id=? AND status='valid'
            AND json_extract(details,'$.structure_revision') IS NOT NULL ORDER BY created DESC LIMIT 1""", (result_id,version_id)).fetchone()
        latest = db.execute("""SELECT g.snapshot,t.finished FROM geometry_requests g JOIN tasks t ON t.id=g.task_id
            WHERE g.result_id=? AND t.status='succeeded' ORDER BY t.finished DESC LIMIT 1""", (result_id,)).fetchone()
        if remapped and (not latest or remapped['created'] > latest['finished']):
            details = json.loads(remapped['details'])
            provider, algorithm = details.get('geometry_provider','paddle'), details.get('algorithm','local-v3')
        elif latest:
            snapshot = json.loads(latest['snapshot'])
            provider, algorithm = snapshot.get('geometry_provider', 'paddle'), snapshot.get('algorithm')
        else:
            latest = db.execute("SELECT details FROM geometry_evidence WHERE result_id=? AND version_id=? AND status='valid' AND source!='manual' ORDER BY created DESC LIMIT 1", (result_id, version_id)).fetchone()
            if latest:
                details = json.loads(latest['details'])
                provider, algorithm = details.get('geometry_provider', 'paddle'), details.get('algorithm')
    provider = provider or 'paddle'
    active_policy = policy_for_algorithm(algorithm)
    active_policy_hash = fingerprint(active_policy)
    active_algorithm_hash = matching_code_fingerprint()
    text_snapshot_hash = fingerprint(edit)
    result = []
    clauses, params = ["result_id=?", "version_id=?", "status='valid'"], [result_id, version_id]
    if target is not None:
        # The serialized targets are canonical within this store. Filter before
        # loading payloads so one focused cell never decodes every table cell.
        clauses.append('target=?')
        keys = ('kind', 'table', 'row', 'column') if target.get('kind') == 'cell' else ('kind', 'table') if target.get('kind') == 'table' else ('kind', 'start', 'end')
        params.append(encoded({key: target[key] for key in keys if key in target}))
    selected_source = geometry_source(provider, active_policy['algorithm'])
    for evidence in db.execute("SELECT * FROM geometry_evidence WHERE " + ' AND '.join(clauses) + " ORDER BY source='manual' DESC,source=? DESC,created DESC", params+[selected_source]):
        item = dict(evidence)
        if item['structure_sha256'] != structure and json.loads(item['target']).get('kind') != 'text':
            continue
        item['target'], item['details'] = json.loads(item['target']), json.loads(item['details'])
        if item['source'] not in ('manual', selected_source):
            continue
        if item['source'] != 'manual' and item['details'].get('algorithm_version') not in (None, 'legacy-v1') and (item['details'].get('policy_sha256') != active_policy_hash or item['details'].get('algorithm_sha256')!=active_algorithm_hash):
            continue
        item['details']['anchor_snapshot_current'] = item['details'].get('anchor_snapshot',{}).get('text_sha256') == text_snapshot_hash
        item['details']['display_reason'] = geometry_description(item['details'], item['source'])
        item['polygon'] = json.loads(item['polygon']) if item['polygon'] else None
        if target is None or item['target'] == target:
            result.append(item)
    return {'result_id': result_id, 'revision': row['revision'], 'version_id': version_id,
            'experimental': True, 'evidence': result, 'cache_read_ms': (time.perf_counter()-started)*1000}


def geometry_description(details, source):
    if source == 'manual':
        return '人工定位'
    if details.get('range_semantics') == 'text_extent':
        return '文字范围（实验性）· 尚无可靠完整格边界'
    if details.get('level') == 'cell':
        return '单元格定位（实验性）' + (' · 位置沿用此前文字快照' if details.get('anchor_snapshot_current') is False else '')
    reasons = {'table_identity_ambiguous':'无法区分相似表格', 'topology_mismatch':'局部结构不一致',
        'boxes_slots_out_of_sync':'模型框与逻辑格不同步', 'malformed_structure':'模型结构不完整',
        'no_local_anchors':'缺少可靠的局部文字锚点', 'repeated_value_ambiguous':'重复内容无法消歧',
        'empty_cell_without_anchors':'空格缺少行列锚点', 'coordinate_version_mismatch':'图像版本已变化',
        'timeout':'本次定位超时', 'candidate_budget_exceeded':'表格候选过多',
        'order_conflict':'文字顺序与表格结构冲突', 'no_text_tokens':'缺少真实文字坐标'}
    return reasons.get(details.get('reason'),'暂无可靠单元格对应') + ('，显示整表' if details.get('level')=='region' else '，显示全图')

"""Compose native content and region OCR without changing the displayed version."""

from copy import deepcopy
import json
import math

from ocr_workbench.atomic_files import read_json
from ocr_workbench.coordinates import bounds, polygon as map_polygon, box_polygon
from ocr_workbench.document_store import fingerprint
from ocr_workbench.store import uid, now, encoded, history_encoded, Conflict


def map_region_result(data, crop_box, version_id):
    result = deepcopy(data)
    x, y = crop_box[:2]
    transform = [1, 0, x, 0, 1, y, 0, 0, 1]
    for block in result.get('blocks', []):
        if block.get('polygon'):
            block['polygon'] = map_polygon(transform, block['polygon'])
    for table in result.get('tables', []):
        if table.get('polygon'):
            table['polygon'] = map_polygon(transform, table['polygon'])
        for cell in table.get('cells', []):
            if cell.get('polygon'):
                cell['polygon'] = map_polygon(transform, cell['polygon'])
    result['project_image_version'] = version_id
    result['region_transform'] = transform
    result['region_crop_box'] = crop_box
    return result


def merge_page(native, ocr_results, version, doc, page, mode):
    """Dedupe only agreeing text in the same space; retain explicit conflicts."""
    from ocr_workbench.pdf_worker import _intersect, _area
    native_units = native.get('units', []) if mode != 'ocr' else []
    retained = [dict(unit, kind='text') for unit in native_units]
    conflicts, tables, provenance = [], [], []
    normalize = lambda value: ''.join(value.split())
    for result in ocr_results:
        if result.get('content_status') == 'no_text_detected':
            crop = result['region_crop_box']
            conflicts.append({'id':uid(), 'reason':'region_no_text', 'native':[],
                'ocr':{'text':'','polygon':box_polygon(crop),'source':result.get('engine')},
                'task_id':result.get('region_task_id'),'state':'pending'})
        blocks = result.get('blocks', [])
        if not blocks and result.get('text', '').strip():
            blocks = [{'text': result['text'], 'polygon': None, 'kind': 'text', 'confidence': None}]
        for block in blocks:
            if not block.get('text', '').strip():
                continue
            bbox = bounds(block['polygon']) if block.get('polygon') else None
            overlapping = [u for u in native_units if bbox and u.get('polygon') and
                           _intersect(bbox, bounds(u['polygon'])) > _area(bounds(u['polygon'])) * .5]
            joined = ''.join(u['text'] for u in sorted(overlapping, key=lambda u: u['native_index']))
            if overlapping and normalize(joined) == normalize(block['text']):
                provenance.append({'action': 'agreeing_native_kept', 'ocr_text': block['text'],
                                   'ocr_polygon': block.get('polygon'), 'source': result.get('engine')})
                continue
            candidate = dict(block, source=result.get('engine'), source_kind='ocr')
            if overlapping:
                conflict_id = uid()
                candidate['conflict_id'] = conflict_id
                conflicts.append({'id': conflict_id, 'reason': 'native_ocr_overlap',
                                  'native': overlapping, 'ocr': candidate, 'state': 'pending'})
            # Two overlapping crop proposals must not duplicate identical OCR.
            if any(bbox and u.get('polygon') and normalize(u['text']) == normalize(candidate['text']) and
                   _intersect(bbox, bounds(u['polygon'])) > min(_area(bbox), _area(bounds(u['polygon'])))*.8
                   for u in retained):
                continue
            retained.append(candidate)
        tables.extend(deepcopy(result.get('tables', [])))
    retained.sort(key=lambda u: (round(bounds(u['polygon'])[1]/8)*8, bounds(u['polygon'])[0]) if u.get('polygon') else (math.inf, math.inf))
    parts, offset = [], 0
    for index, unit in enumerate(retained):
        separator = '' if index == 0 else '\n'
        if index and unit.get('polygon') and retained[index-1].get('polygon'):
            current, previous = bounds(unit['polygon']), bounds(retained[index-1]['polygon'])
            if abs(current[1]-previous[1]) < min(current[3]-current[1], previous[3]-previous[1])*.5:
                separator = ' '
        parts.append(separator + unit['text'])
        offset += len(separator)
        unit['text_range'] = [offset, offset+len(unit['text'])]
        offset += len(unit['text'])
    return {'status': 'success', 'engine': 'pdf-native' if not ocr_results else 'document',
            'engine_info': {'name': '原生 PDF 提取' if not ocr_results else '页面区域识别'},
            'origin': 'document', 'text': ''.join(parts), 'tables': tables, 'blocks': retained,
            'confidence': None, 'project_image_version': version['id'],
            'image': {'version': version['sha256'], 'width': version['width'], 'height': version['height']},
            'document': {'id': doc['id'], 'page_id': page['id'], 'page_number': page['page_number'],
                         'source_sha256': doc['sha256'], 'mode': mode, 'page_class': native.get('page_class'),
                         'native_flagged': native.get('flagged', []), 'conflicts': conflicts,
                         'table_structure': native.get('table_structure') if mode != 'ocr' else None,
                         'table_structure_error': native.get('table_structure_error'),
                         'table_tool': native.get('table_tool'),
                         'merge_provenance': provenance,
                         'sources': ['pdf-native'] if native_units else [],
                         'ocr_sources': [r.get('engine') for r in ocr_results]},
            'model_revisions': {k: v for result in ocr_results for k, v in result.get('model_revisions', {}).items()}}


def publish_page(manager, stage, data):
    """One transaction publishes a complete page and completes its durable stage."""
    store = manager.store
    from ocr_workbench.documents import DocumentCancelled
    if manager.stopping.is_set():
        raise DocumentCancelled()
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM document_stages WHERE id=?', (stage['id'],)).fetchone()
        if not current or current['status'] not in ('running', 'waiting_gpu'):
            return False
        page = db.execute('SELECT * FROM pages WHERE id=?', (stage['page_id'],)).fetchone()
        image = db.execute('SELECT * FROM images WHERE id=?', (page['image_id'],)).fetchone()
        task_id, result_id = uid(), uid()
        db.execute("""INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,started,finished,result_id,kind)
            VALUES(?,?,?,?,?,?,'succeeded','页面处理完成',?,?,?,?,'document')""",
            (task_id, image['project_id'], image['id'], data['project_image_version'], data['engine'],
             'document-'+stage['id'], now(), now(), now(), result_id))
        edit = {'text': data['text'], 'tables': data['tables']}
        db.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?)', (result_id, task_id, encoded(data), encoded(edit), 0, 0, now()))
        db.execute('INSERT INTO edits VALUES(?,?,?,?)', (result_id, 0, history_encoded(edit), now()))
        db.execute('INSERT OR IGNORE INTO selections VALUES(?,?)', (image['id'], result_id))
        db.execute("UPDATE pages SET status=?,updated=? WHERE id=?",
                   ('blank' if not data['text'] and not data['document']['conflicts'] else 'processed', now(), page['id']))
        db.execute("UPDATE document_stages SET status='succeeded',phase='完成',output=?,finished=? WHERE id=?",
                   (encoded({'result_id': result_id, 'image_id': image['id']}), now(), stage['id']))
        db.execute('UPDATE projects SET updated=? WHERE id=?', (now(), image['project_id']))
        prediction = data['document'].get('table_structure')
        if prediction and prediction.get('pdfplumber_tables'):
            from ocr_workbench.table_tool import record
            version = dict(db.execute('SELECT * FROM versions WHERE id=?', (data['project_image_version'],)).fetchone())
            # Tool detections may overlap (nested grids, competing regions).
            # Offer each upstream table independently, never select a winner by
            # area, filename or observed quality on development materials.
            record(db, result_id, version, prediction, data['blocks'])
        if manager.stopping.is_set():
            raise DocumentCancelled()
    if manager.gpu is not None and not manager.review_only and not data['tables'] and sum(b.get('source') == 'pdf-native' for b in data.get('blocks', [])) >= 4:
        from ocr_workbench.geometry import enqueue_geometry
        enqueue_geometry(store,result_id,0)
        manager.gpu.wake.set()
    return True


def _context(manager, stage):
    store = manager.store
    page = store.one('pages', stage['page_id'])
    doc = store.one('documents', page['document_id'])
    image = store.one('images', page['image_id'])
    version = store.one('versions', stage.get('version_id') or image['active_version'])
    native = (read_json(store.file(page['native_result']))['native'] if page['native_result'] else
              {'units': [], 'flagged': [], 'coverage_regions': [], 'page_class': 'scan'})
    return page, doc, version, native


def process_stage(manager, stage, cancelled):
    store = manager.store
    parameters = json.loads(stage['parameters'])
    image = manager.ensure_rendered(stage['page_id'], cancelled)
    stage['version_id'] = stage.get('version_id') or image['active_version']
    with store.transaction() as db:
        db.execute('UPDATE document_stages SET version_id=? WHERE id=? AND version_id IS NULL', (stage['version_id'], stage['id']))
    page, doc, version, native = _context(manager, stage)
    relation = store.page_for_version(version['id'])
    original_version = store.rows('SELECT id FROM versions WHERE image_id=? AND parent_id IS NULL', (image['id'],))[0]['id']
    mode = parameters['mode']
    if version['id'] != original_version:
        if mode == 'native':
            raise ValueError('原生提取需要原页面版本；当前图像已经处理，请切回原页或选择 OCR')
        mode = 'ocr'
    from ocr_workbench.table_tool import prepare
    from ocr_workbench.documents import DocumentCancelled
    prepare(manager, page, doc, version, native, mode, cancelled)
    if cancelled():
        raise DocumentCancelled()
    auxiliary = {'mode': mode, 'table_structure': native.get('table_structure'),
                 'table_structure_error': native.get('table_structure_error'), 'table_tool': native['table_tool']}
    # Refresh this snapshot even when already completed OCR regions are resumed.
    with store.transaction() as db:
        db.execute("UPDATE document_stages SET output=? WHERE id=? AND status='running'", (encoded(auxiliary), stage['id']))
    prior = store.rows('SELECT t.status FROM tasks t JOIN page_ocr_inputs i ON i.task_id=t.id WHERE i.stage_id=?', (stage['id'],))
    if prior:
        with store.transaction() as db:
            db.execute("UPDATE tasks SET status='queued',phase='等待区域识别',error=NULL WHERE id IN (SELECT task_id FROM page_ocr_inputs WHERE stage_id=?) AND status IN ('paused','interrupted','failed','cancelled')", (stage['id'],))
            db.execute("UPDATE document_stages SET status='waiting_gpu',phase='等待区域识别' WHERE id=? AND status='running'", (stage['id'],))
        if manager.gpu:
            manager.gpu.wake.set()
        return
    regions = store.page_regions(page['id'], version['id'])
    needed = [r for r in regions if r['kind'] == 'ocr-needed'] if mode == 'auto' else []
    if mode == 'ocr' or (mode == 'auto' and not native['units'] and native.get('page_class') != 'blank' and not needed):
        needed = [{'id': None, 'polygon': box_polygon([0, 0, version['width'], version['height']])}]
    if mode == 'native' or not needed:
        data = merge_page(native, [], version, doc, page, mode)
        data['document']['unprocessed_regions'] = len(native.get('coverage_regions', [])) if mode == 'native' else 0
        data['document']['native_only_incomplete'] = mode == 'native' and bool(native.get('coverage_regions'))
        publish_page(manager, stage, data)
        return
    if manager.review_only:
        raise ValueError('当前页面需要 OCR，请在完整模式处理')
    engine = parameters['engine']
    package = manager.gpu.registry.engines()[engine]['package_id'] if manager.gpu and manager.gpu.registry else 'builtin'
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT status FROM document_stages WHERE id=?', (stage['id'],)).fetchone()
        if current['status'] != 'running':
            return
        for region in needed:
            box = bounds(region['polygon'])
            box = [max(0, math.floor(box[0])), max(0, math.floor(box[1])),
                   min(version['width'], math.ceil(box[2])), min(version['height'], math.ceil(box[3]))]
            task_id = uid()
            db.execute("""INSERT INTO tasks(id,project_id,image_id,version_id,engine,batch,status,phase,created,kind,engine_package)
                VALUES(?,?,?,?,?,?,'queued','等待区域识别',?,'region_ocr',?)""",
                (task_id, doc['project_id'], image['id'], version['id'], engine, 'page-'+stage['id'], now(), package))
            db.execute('INSERT INTO page_ocr_inputs VALUES(?,?,?,?,NULL)', (task_id, stage['id'], region['id'], encoded(box)))
        db.execute("UPDATE document_stages SET version_id=?,status='waiting_gpu',phase='等待区域识别',output=? WHERE id=?",
                   (version['id'], encoded({'mode': mode, 'regions': len(needed),
                    **auxiliary}), stage['id']))
    if manager.gpu:
        manager.gpu.wake.set()


def finalize_waiting_pages(manager):
    store = manager.store
    stages = store.rows("SELECT * FROM document_stages WHERE status='waiting_gpu' ORDER BY created LIMIT 50")
    for stage in stages:
        if manager.stopping.is_set():
            return
        inputs = store.rows('SELECT t.status,i.output FROM page_ocr_inputs i JOIN tasks t ON t.id=i.task_id WHERE i.stage_id=? ORDER BY t.created,t.id', (stage['id'],))
        states = {row['status'] for row in inputs}
        if not inputs or states & {'queued', 'running', 'paused', 'interrupted'}:
            continue
        if states & {'failed', 'cancelled'}:
            with store.transaction() as db:
                db.execute("UPDATE document_stages SET status='failed',phase='区域识别未完成',error='部分区域失败或取消；重试只继续未完成区域' WHERE id=? AND status='waiting_gpu'", (stage['id'],))
            continue
        page, doc, version, native = _context(manager, stage)
        output = json.loads(stage['output'])
        mode = output['mode']
        from ocr_workbench.table_tool import tool_identity
        if mode != 'ocr' and doc['kind'] == 'pdf' and (output.get('table_tool') or {}).get('tool_key') != tool_identity()['key']:
            with store.transaction() as db:
                db.execute("UPDATE document_stages SET status='queued',phase='更新表格候选' WHERE id=? AND status='waiting_gpu'", (stage['id'],))
            continue
        for key in ('table_structure', 'table_structure_error', 'table_tool'):
            native[key] = output.get(key)
        data = merge_page(native, [json.loads(row['output']) for row in inputs], version, doc, page, mode)
        publish_page(manager, stage, data)


def complete_region_task(store, task, data):
    row = store.rows('SELECT * FROM page_ocr_inputs WHERE task_id=?', (task['id'],))[0]
    mapped = map_region_result(data, json.loads(row['crop_box']), task['version_id'])
    mapped['region_task_id'] = task['id']
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("UPDATE tasks SET status='succeeded',phase='区域识别完成',finished=? WHERE id=? AND status='running'", (now(), task['id'])).rowcount != 1:
            return False
        db.execute('UPDATE page_ocr_inputs SET output=? WHERE task_id=?', (encoded(mapped), task['id']))
    return True

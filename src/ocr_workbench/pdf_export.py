"""Capture adopted revisions and coordinate evidence before CPU PDF export."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench.coordinates import validate_polygon
from ocr_workbench.document_store import structure_fingerprint, fingerprint
from ocr_workbench.editing import table_bindings, validate_edit
from ocr_workbench.geometry import geometry_view
from ocr_workbench.review_issues import relocate_text
from ocr_workbench.store import Conflict


def positioned_content(result, evidence, version):
    """No coordinate inference from table dimensions or unbound new text."""
    raw, edit = result['original'], result['edited']
    validate_edit(edit)
    text = edit['text']
    covered = bytearray(len(text))
    units, missing = [], []
    native_changed = False
    parsed, replacements, _ = table_bindings(edit)
    for n, table in enumerate(parsed):
        if n in replacements:
            a, b = table['source']['start'], table['source']['end']
            covered[a:b] = b'\1'*(b-a)

    def add(value, poly, target, source, *, native=False):
        if not value.strip():
            return True
        try:
            valid = validate_polygon(poly, version['width'], version['height'])
        except (ValueError, TypeError):
            missing.append({'target': target, 'text': value[:120], 'reason': '缺少可靠定位'})
            return False
        units.append({'text': value, 'polygon': valid, 'target': target, 'source': source, 'native_preserved': native})
        return True

    by_target = {}
    for item in evidence:
        by_target.setdefault(json.dumps(item['target'], sort_keys=True), item)
    # Preserve unchanged native runs before applying user text bindings. A broad
    # manual selection over existing visible text must not add a duplicate layer.
    native_cursor = 0
    for block in raw.get('blocks', []):
        value = block.get('text', '')
        if not value.strip() or block.get('kind') == 'table' or not (block.get('source_kind') == 'native' or block.get('source') == 'pdf-native'):
            continue
        span = block.get('text_range')
        if not span:
            start = raw['text'].find(value, native_cursor)
            if start < 0: continue
            span = [start, start+len(value)]
            native_cursor = span[1]
        target, stable = relocate_text(raw['text'], text, {'kind': 'text', 'start': span[0], 'end': span[1]})
        a, b = target['start'], target['end']
        if stable and not target.get('unlocatable') and text[a:b] == value and not any(covered[a:b]):
            if add(value, block.get('polygon'), target, 'pdf-native', native=True):
                covered[a:b] = b'\1'*(b-a)
    for item in evidence:
        target = item['target']
        if target.get('kind') != 'text':
            continue
        a, b = target['start'], target['end']
        if 0 <= a < b <= len(text) and not any(covered[a:b]) and add(text[a:b], item['polygon'], target, item['source']):
            covered[a:b] = b'\1'*(b-a)
    same_structure = structure_fingerprint(edit) == structure_fingerprint(raw)
    # Deleting/restructuring a native table must remove its old searchable
    # visible text too, including when another native line remains on the page.
    if raw.get('document', {}).get('table_native_cells') and not same_structure:
        native_changed = True
    for ti, table in enumerate(edit['tables']):
        if table.get('caption'):
            # Captions require their own text binding; a table box is insufficient.
            if table['caption'] not in text:
                missing.append({'target': {'kind': 'caption', 'table': ti}, 'text': table['caption'], 'reason': '表格标题需要文字定位'})
        for cell in table['cells']:
            target = {'kind': 'cell', 'table': ti, 'row': cell['row'], 'column': cell['column']}
            native_cell = raw.get('document',{}).get('table_native_cells',{}).get(f"{ti}:{cell['row']}:{cell['column']}") if same_structure else None
            if native_cell and native_cell['text'] == cell['text']:
                for original_unit in native_cell['units']:
                    add(original_unit['text'],original_unit['polygon'],target,'pdf-native',native=True)
                continue
            if native_cell:
                native_changed = True
            item = by_target.get(json.dumps(target, sort_keys=True))
            poly, source = None, 'unlocated'
            if item and item['details']['level'] == 'cell':
                poly, source = item['polygon'], item['source']
            elif same_structure and ti < len(raw.get('tables', [])):
                original = next((c for c in raw['tables'][ti]['cells'] if c['row'] == cell['row'] and c['column'] == cell['column']), {})
                poly, source = original.get('polygon'), raw.get('engine')
            add(cell['text'], poly, target, source)
    cursor = 0
    for block in raw.get('blocks', []):
        value = block.get('text', '')
        if not value.strip() or block.get('kind') == 'table':
            continue
        native = block.get('source_kind') == 'native' or block.get('source') == 'pdf-native'
        span = block.get('text_range')
        if not span:
            start = raw['text'].find(value, cursor)
            if start < 0:
                native_changed |= native
                continue
            span = [start, start+len(value)]
            cursor = span[1]
        target, stable = relocate_text(raw['text'], text, {'kind': 'text', 'start': span[0], 'end': span[1]})
        a, b = target['start'], target['end']
        if target.get('unlocatable') or a == b:
            native_changed |= native
            continue
        unchanged = text[a:b] == value
        native_changed |= native and not unchanged
        if any(covered[a:b]):
            if native and unchanged:
                for existing in units:
                    if existing['target'] == target and existing['text'] == value:
                        existing['native_preserved'] = True
            continue
        # An exact replacement of a known line retains its line region.
        if add(text[a:b], block.get('polygon'), target, block.get('source') or raw.get('engine'), native=native and unchanged):
            covered[a:b] = b'\1'*(b-a)
    index = 0
    while index < len(text):
        if covered[index] or text[index].isspace():
            index += 1
            continue
        start = index
        while index < len(text) and not covered[index]:
            index += 1
        if text[start:index].strip():
            missing.append({'target': {'kind': 'text', 'start': start, 'end': index}, 'text': text[start:index][:120], 'reason': '新增或改动文字尚无可靠定位'})
    return units, missing, native_changed


def capture_pdf(store, body, folder):
    selectors = [key for key in ('document_ids', 'page_ids', 'result_ids') if body.get(key)]
    if len(selectors) != 1:
        raise ValueError('PDF 导出请选择文档、页面或结果中的一种范围')
    values = body[selectors[0]]
    if not isinstance(values, list) or not 1 <= len(values) <= 1000 or not all(isinstance(v, str) for v in values):
        raise ValueError('PDF 导出范围无效（最多 1000 项）')
    documents, failures = {}, []
    with store.transaction() as db:
        db.execute('BEGIN')
        placeholders = ','.join('?' for _ in values)
        if selectors[0] == 'document_ids':
            rows = db.execute(f'SELECT * FROM pages WHERE document_id IN ({placeholders}) ORDER BY document_id,page_number', values).fetchall()
            found = {r['document_id'] for r in rows}
        elif selectors[0] == 'page_ids':
            rows = db.execute(f'SELECT * FROM pages WHERE id IN ({placeholders}) ORDER BY document_id,page_number', values).fetchall()
            found = {r['id'] for r in rows}
        else:
            rows = db.execute(f'''SELECT p.*,r.id selected_result FROM results r JOIN tasks t ON t.id=r.task_id
                JOIN pages p ON p.image_id=t.image_id WHERE r.id IN ({placeholders}) ORDER BY p.document_id,p.page_number''', values).fetchall()
            found = {r['selected_result'] for r in rows}
        if found != set(values) or len(rows) > 10000:
            raise ValueError('范围包含不存在、尚未解锁或过多的页面')
        if body.get('page_numbers') is not None:
            numbers = body['page_numbers']
            if selectors[0] != 'document_ids' or len(values) != 1 or not isinstance(numbers, list) or not numbers or any(type(n) is not int for n in numbers) or not set(numbers) <= {r['page_number'] for r in rows}:
                raise ValueError('所选页码范围无效')
            rows = [r for r in rows if r['page_number'] in set(numbers)]
        if len({r['id'] for r in rows}) != len(rows):
            raise ValueError('同一页只能选择一个已采用结果')
        for ordinal, row in enumerate(rows):
            page = dict(row)
            info = {'page_id': page['id'], 'page_number': page['page_number']}
            if not page['image_id']:
                failures.append({**info, 'reason': '页面尚未处理'})
                continue
            adopted = store._review_target(db, page['image_id'])
            if not adopted['result_id'] or adopted['version_id'] != adopted['active_version']:
                failures.append({**info, 'reason': '需要采用当前图像版本的完整结果'})
                continue
            if page.get('selected_result') and page['selected_result'] != adopted['result_id']:
                raise Conflict('PDF 必须使用已采用结果，请先采用当前预览')
            result = dict(db.execute('SELECT * FROM results WHERE id=?', (adopted['result_id'],)).fetchone())
            for field in ('original', 'edited'):
                result[field] = json.loads(result[field])
            if body.get('confirmed_only'):
                review = db.execute('SELECT * FROM reviews WHERE image_id=?', (page['image_id'],)).fetchone()
                if not review or review['status'] != 'confirmed' or review['result_id'] != result['id'] or review['revision'] != result['revision'] or review['version_id'] != adopted['version_id']:
                    failures.append({**info, 'reason': '页面未确认或确认已失效'})
                    continue
            version = dict(db.execute('SELECT * FROM versions WHERE id=?', (adopted['version_id'],)).fetchone())
            view = geometry_view(store, result['id'], db=db)
            units, missing, native_changed = positioned_content(result, view['evidence'], version)
            rawdoc = result['original'].get('document', {})
            if rawdoc.get('native_only_incomplete'):
                missing.append({'reason': '仅原生提取尚有未识别区域'})
            for conflict in rawdoc.get('conflicts', []):
                # Resolving overlaps requires an explicit recorded choice.
                decision = db.execute('SELECT * FROM document_conflict_decisions WHERE result_id=? AND conflict_id=?', (result['id'], conflict['id'])).fetchone()
                if not decision or decision['edited_sha256'] != fingerprint(result['edited']):
                    missing.append({'reason': '原生与 OCR 内容重叠冲突尚未核对', 'conflict_id': conflict['id']})
            if missing:
                failures.append({**info, 'reason': '定位或内容预检未通过', 'items': missing})
                continue
            doc_id = page['document_id']
            if doc_id not in documents:
                doc = dict(db.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone())
                documents[doc_id] = {'id': doc_id, 'name': doc['name'], 'kind': doc['kind'],
                    'source_path': str(store.file(doc['original_path'])), 'source_sha256': doc['sha256'], 'pages': []}
            doc = documents[doc_id]
            reasons = []
            if doc['kind'] != 'pdf': reasons.append('image_or_tiff_source')
            if version['parent_id']: reasons.append('processed_image_version')
            if native_changed: reasons.append('visible_native_text_changed')
            native = read_json(store.file(page['native_result']))['native'] if page['native_result'] else {}
            preserved = [u for u in units if u['native_preserved']]
            if native.get('units') and not preserved: reasons.append('native_mapping_unavailable')
            if any(set(u.get('rendering_modes', [])) - {3, 7} or not u.get('rendering_modes') for u in native.get('flagged', [])):
                reasons.append('damaged_native_mapping')
            specification = {**info, 'result_id': result['id'], 'revision': result['revision'], 'version_id': version['id'],
                'image_path': str(store.file(version['path'])), 'image_sha256': version['sha256'],
                'width': version['width'], 'height': version['height'], 'dpi': page['render_dpi'],
                'rebuild_reasons': list(dict.fromkeys(reasons)), 'units': units,
                'geometry_coverage': 1.0 if units else None, 'origin': result['original'].get('origin', 'single-engine')}
            specification['conflict_reviews'] = [dict(d) for d in db.execute('SELECT * FROM document_conflict_decisions WHERE result_id=?', (result['id'],))]
            path = folder / f'page-{ordinal:05d}.json'
            write_json(path, specification)
            doc['pages'].append(str(path))
    return {'documents': list(documents.values()), 'failures': failures}


def build_pdf_export(store, manager, body, *, preflight=False):
    parent = store.root / 'exports'
    parent.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='pdf-', dir=parent))
    try:
        captured = capture_pdf(store, body, folder)
        if preflight:
            return {'ready': not captured['failures'], 'failures': captured['failures'],
                    'document_count': len(captured['documents']), 'page_count': sum(len(d['pages']) for d in captured['documents'])}
        if captured['failures']:
            descriptions = [f"第 {f['page_number']} 页：{f['reason']}" for f in captured['failures'][:12]]
            raise ValueError('PDF 导出预检未通过。' + '；'.join(descriptions) + '。请补定位、核对冲突，或明确排除这些页面。')
        outputs = []
        for index, doc in enumerate(captured['documents']):
            output = folder / (f'{index+1:03d}-document.pdf' if len(captured['documents']) > 1 else 'OCR-document.pdf')
            manager.cpu.call({'operation': 'export', 'document': doc, 'password': manager._password(doc['id']),
                              'fonts': str(manager.cpu.runtime.parents[2] / 'fonts'), 'output': str(output)}, timeout=1800)
            outputs.append(output)
        if len(outputs) == 1:
            return outputs[0]
        archive = folder / 'OCR-documents.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as target:
            for output in outputs:
                target.write(output, output.name)
                target.write(output.with_suffix('.sources.json'), output.with_suffix('.sources.json').name)
        return archive
    except BaseException:
        shutil.rmtree(folder)
        raise
    finally:
        if preflight and folder.exists():
            shutil.rmtree(folder)

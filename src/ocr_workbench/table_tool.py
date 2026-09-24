"""Independent, recoverable PDF table extraction; never edits adopted content."""
import hashlib
import json
from pathlib import Path

from ocr_workbench.atomic_files import read_json, write_json
from ocr_workbench.document_store import fingerprint

ROOT = Path(__file__).resolve().parents[2]


def tool_identity():
    data = (ROOT / 'config/pdf-table-tool.json').read_bytes()
    config = json.loads(data)
    contract = {'config': hashlib.sha256(data).hexdigest(), 'adapter_contract': 1,
                'code': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                         for name in ('pdf_tables.py', 'coordinates.py')}}
    return {'key': fingerprint(contract), 'config_sha256': contract['config'],
            'version': config['version'], 'component': config['provider']}


def prepare(manager, page, doc, version, native, mode, cancelled, *, force=False):
    from ocr_workbench.documents import DocumentCancelled, WaitingUnlock
    native.pop('table_structure', None)
    native.pop('table_structure_error', None)
    if mode == 'ocr' or doc['kind'] != 'pdf':
        native['table_tool'] = {'state': 'not_applicable', 'retry_allowed': False,
            'message': '当前为图像 OCR，PDF 原生表格工具不适用'}
        return
    if cancelled():
        raise DocumentCancelled()
    tool = tool_identity()
    status = {'tool_key': tool['key'], 'retry_allowed': True}
    try:
        metadata = read_json(manager.store.file(page['native_result']))['metadata']
        identity = {'tool_key': tool['key'], 'source_sha256': doc['sha256'],
                    'page_number': page['page_number'], 'image_sha256': version['sha256'], 'metadata': metadata}
        folder = manager.store.file(doc['original_path']).parent / 'table-cache'
        path = folder / (fingerprint(identity) + '.json')
        prediction = None
        if path.is_file() and not force:
            try:
                cached = read_json(path)
                if cached['identity'] == identity and fingerprint(cached['prediction']) == cached['sha256']:
                    prediction = cached['prediction']
            except (ValueError, KeyError, OSError, TypeError):
                pass
        if prediction is None:
            prediction = manager.cpu.call({'operation': 'table-structure',
                'path': str(manager.store.file(doc['original_path'])), 'page_number': page['page_number'],
                'metadata': metadata, 'password': manager._password(doc['id'])}, cancelled)
        if cancelled():
            raise DocumentCancelled()
        if (prediction.get('settings_sha256') != tool['config_sha256']
                or prediction.get('tool_version') != tool['version'] or tool_identity() != tool):
            raise ValueError('表格工具配置已变化，请重试表格提取')
        folder.mkdir(exist_ok=True)
        write_json(path, {'identity': identity, 'prediction': prediction, 'sha256': fingerprint(prediction)})
        native['table_structure'] = prediction
        status.update(state='ready' if prediction['pdfplumber_tables'] else 'empty',
                      run_id=fingerprint(prediction), message='表格候选已提取' if prediction['pdfplumber_tables']
                      else '表格提取已完成，未检测到有线表格；仍需核对原图')
    except (DocumentCancelled, WaitingUnlock):
        raise
    except Exception as error:
        native['table_structure_error'] = str(error)
        status.update(state='failed', message='PDF 表格提取失败，可单独重试；页面内容已保留', error=str(error))
    native['table_tool'] = status


def record(db, result_id, version, prediction, blocks):
    from ocr_workbench.structure_store import record_candidates
    from ocr_workbench.native_tables import native_source_blocks
    blocks,source_result=native_source_blocks(blocks,version['id'])
    run_id = fingerprint(prediction)
    for table in prediction.get('pdfplumber_tables', []):
        record_candidates(db, result_id, version, dict(prediction, pdfplumber_tables=[table],
            table_tool_run_id=run_id, candidate_provider_key='pdfplumber/'+table['id'],
            image_version=version['id'], image_sha256=version['sha256']),
            [b for b in blocks if b.get('source') == 'pdf-native'], source_result=source_result)
    for variant in prediction.get('experimental_alternatives',[]):
        for table in variant['pdfplumber_tables']:
            alternative={k:v for k,v in prediction.items() if k!='experimental_alternatives'}
            alternative.update(pdfplumber_tables=[table],table_tool_run_id=run_id,
                candidate_variant=variant['variant'],candidate_provider_key='pdfplumber/'+variant['variant']+'/'+table['id'],
                image_version=version['id'],image_sha256=version['sha256'])
            record_candidates(db,result_id,version,alternative,
                [b for b in blocks if b.get('source')=='pdf-native'],source_result=source_result)


def view(db, result, current_tool=None):
    raw = result['original']
    document = raw.get('document', {})
    if raw.get('origin') != 'document' or document.get('mode') == 'ocr':
        return {'state': 'not_applicable', 'retry_allowed': False,
                'message': '当前结果不使用 PDF 原生表格工具'}
    page = db.execute('SELECT d.kind FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?',
                      (document.get('page_id'),)).fetchone()
    if not page or page['kind'] != 'pdf':
        return {'state': 'not_applicable', 'retry_allowed': False, 'message': '此材料不适用 PDF 原生表格工具'}
    status = dict(document.get('table_tool') or {'state': 'outdated', 'retry_allowed': True,
                  'message': '表格候选需要按当前工具重新提取'})
    current_tool = current_tool or tool_identity()
    if status.get('tool_key') != current_tool['key']:
        status['run_id'] = None
    stage = db.execute("SELECT * FROM document_stages WHERE kind='table_structure' AND page_id=? "
        "AND json_extract(parameters,'$.result_id')=? ORDER BY created DESC,rowid DESC LIMIT 1",
        (document['page_id'], result['id'])).fetchone()
    if stage:
        output = json.loads(stage['output'] or '{}')
        if stage['status'] == 'succeeded':
            status = output['table_tool']
        else:
            labels = {'queued': '等待表格提取', 'running': '正在提取表格', 'paused': '表格提取已暂停，请继续文档任务',
                'interrupted': '表格提取已中断，可重试', 'cancelled': '表格提取已取消，可重试',
                'waiting_unlock': 'PDF 需要解锁，请输入密码后继续', 'failed': 'PDF 表格提取失败，可单独重试'}
            status.update(state=stage['status'], message=labels.get(stage['status'], stage['status']),
                error=stage['error'], retry_allowed=stage['status'] not in ('queued', 'running'), stage_id=stage['id'])
            return status
    if status.get('tool_key') != current_tool['key']:
        status.update(state='outdated', message='工具配置已更新，需要重新提取表格候选', run_id=None, retry_allowed=True)
    return status


def current_candidate(row, status):
    return not row['provider'].startswith('pdfplumber/') or bool(status.get('run_id') and
        json.loads(row['payload'])['prediction'].get('table_tool_run_id') == status['run_id'])


def enqueue(manager, result_id, revision):
    from ocr_workbench.structure_store import context
    from ocr_workbench.store import Conflict
    with manager.store.transaction() as db:
        result = context(db, result_id)
        if result['revision'] != revision:
            raise Conflict('修订已变化，请保存后重试')
        status = view(db, result)
        if status['state'] == 'not_applicable':
            raise ValueError(status['message'])
        page_id = result['original']['document']['page_id']
    stage = manager.store.enqueue_document_stage(page_id, 'table_structure',
        {'result_id': result_id, 'tool_key': tool_identity()['key']}, force=True)
    scheduled = manager.store.one('document_stages', stage)
    if scheduled['kind'] != 'table_structure' or json.loads(scheduled['parameters']).get('result_id') != result_id:
        raise Conflict('页面正在处理，请完成或取消当前任务后重试表格提取')
    manager.wake.set()
    return {'stage_id': stage}


def retry_stage(manager, stage, cancelled):
    from ocr_workbench.page_processing import _context
    from ocr_workbench.structure_store import context
    from ocr_workbench.documents import DocumentCancelled
    from ocr_workbench.store import encoded, now
    result_id = json.loads(stage['parameters'])['result_id']
    with manager.store.transaction() as db:
        result = context(db, result_id)
    page, doc, version, native = _context(manager, stage)
    if version['id'] != result['version_id']:
        raise ValueError('图像版本已变化，请切回对应版本后重试')
    prepare(manager, page, doc, version, native, result['original']['document']['mode'], cancelled, force=True)
    if cancelled():
        raise DocumentCancelled()
    status = native['table_tool']
    if status['state'] == 'failed':
        manager.store.finish_document_stage(stage['id'], {'table_tool': status}, error=status['error'])
        return
    with manager.store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        context(db, result_id)
        if cancelled():
            raise DocumentCancelled()
        # Accepted revisions and their provenance remain immutable. Only pending
        # tool suggestions are retired; historical candidates remain available.
        db.execute("UPDATE structure_proposals SET state='stale',updated=? WHERE result_id=? "
            "AND state IN ('pending','deferred') AND json_extract(payload,'$.provider') LIKE 'pdfplumber/%'", (now(), result_id))
        db.execute('DELETE FROM structure_checks WHERE result_id=?', (result_id,))
        record(db, result_id, version, native['table_structure'], result['original'].get('blocks', []))
        if cancelled():
            raise DocumentCancelled()
        db.execute("UPDATE document_stages SET status='succeeded',phase='表格提取完成',output=?,finished=? WHERE id=? AND status='running'",
                   (encoded({'table_tool': status}), now(), stage['id']))

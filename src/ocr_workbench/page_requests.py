"""Identified platform PDF page attempts, committed with their CPU stages."""
import json
import re
from ocr_workbench.document_store import fingerprint
from ocr_workbench.store import encoded, now, Conflict

REQUEST = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,119}\Z')


def read(store, document_id, request_id):
    if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
        raise ValueError('请求身份无效')
    doc = store.one('documents', document_id)
    rows = store.rows('SELECT * FROM platform_page_requests WHERE document_id=? AND request_id=?', (document_id, request_id))
    if not rows:
        return {'submitted': False, 'document_id': document_id, 'project_id': doc['project_id'], 'request_id': request_id}
    row = rows[0]
    body = json.loads(row['body'])
    owned = set(json.loads(row['owned_stage_ids']))
    stages = []
    for stage_id in json.loads(row['stage_ids']):
        stage = store.one('document_stages', stage_id)
        page = store.one('pages', stage['page_id'])
        output = json.loads(stage['output']) if stage['output'] else None
        stages.append({'stage_id': stage_id, 'page_id': page['id'], 'page_number': page['page_number'],
                       'status': stage['status'], 'phase': stage['phase'], 'error': stage['error'],
                       'output': output, 'image_id': page['image_id'], 'owned': stage_id in owned})
    return {'submitted': True, 'document_id': document_id, 'project_id': doc['project_id'],
            'request_id': request_id, 'payload_hash': row['payload_hash'], 'parameters': body,
            'stages': stages}


def submit(documents, document_id, request_id, body):
    store = documents.store
    if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
        raise ValueError('请求身份无效')
    if not isinstance(body, dict) or set(body) != {'page_numbers', 'mode', 'engine', 'force'}:
        raise ValueError('页面请求字段无效')
    doc = store.one('documents', document_id)
    numbers = body['page_numbers']
    if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 1000 or len(set(numbers)) != len(numbers)
            or any(type(number) is not int or not 1 <= number <= doc['page_count'] for number in numbers)
            or type(body['force']) is not bool):
        raise ValueError('显式页集无效')
    body = {**body, 'page_numbers': sorted(numbers)}
    parameters = documents.processing_parameters(body['mode'], body['engine'])
    digest = fingerprint({'document_sha256': doc['sha256'], 'body': body, 'parameters': parameters})
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT payload_hash FROM platform_page_requests WHERE document_id=? AND request_id=?',
                         (document_id, request_id)).fetchone()
        if old:
            if old['payload_hash'] != digest:
                raise Conflict('同请求身份已绑定其他页集或参数')
        else:
            pages = db.execute('SELECT id,page_number FROM pages WHERE document_id=? ORDER BY page_number', (document_id,)).fetchall()
            selected = [page['id'] for page in pages if page['page_number'] in numbers]
            if len(selected) != len(numbers):
                raise ValueError('文档页面映射不完整')
            prior = {row[0] for page in selected for row in db.execute('SELECT id FROM document_stages WHERE page_id=?', (page,))}
            keys = store._enqueue_document_stages(db, selected, 'process', parameters, force=body['force'])
            db.execute('INSERT INTO platform_page_requests VALUES(?,?,?,?,?,?,?)',
                       (document_id, request_id, digest, encoded(body), encoded(keys), encoded([key for key in keys if key not in prior]), now()))
    documents.wake.set()
    return read(store, document_id, request_id)


def action(documents, document_id, request_id, action):
    value = read(documents.store, document_id, request_id)
    if not value['submitted'] or action not in {'cancel', 'resume'}:
        raise ValueError('页面请求不存在或动作无效')
    sources = {'queued', 'running', 'waiting_gpu', 'paused', 'interrupted', 'waiting_unlock'} if action == 'cancel' else {'paused', 'interrupted'}
    target = 'cancelled' if action == 'cancel' else 'queued'
    affected = []
    store = documents.store
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        for stage in value['stages']:
            if not stage['owned']:
                continue
            cursor = db.execute('UPDATE document_stages SET status=?,phase=? WHERE id=? AND status IN (' +
                ','.join('?' for _ in sources) + ')', (target, '已取消' if action == 'cancel' else '等待处理', stage['stage_id'], *sources))
            if cursor.rowcount:
                affected.append(stage['stage_id'])
        # Only GPU tasks descended from our owned CPU stages qualify.
        tasks = [(row['project_id'], row['id']) for stage in value['stages'] if stage['owned'] for row in db.execute(
            'SELECT t.id,t.project_id FROM tasks t JOIN page_ocr_inputs p ON p.task_id=t.id WHERE p.stage_id=?', (stage['stage_id'],))]
    if documents.gpu and tasks:
        documents.gpu.action(tasks[0][0], action, [task[1] for task in tasks])
    documents.wake.set()
    return {'affected_stage_ids': affected, **read(store, document_id, request_id)}

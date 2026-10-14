from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from workbench_platform.data_api.service import DataError


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def todo_identity(kind, parent_id, entity_id):
    return ('i' if kind == 'issue' else 'f') + fingerprint([kind, parent_id, entity_id])[:48]


def dispatch_bindings(intent):
    segments = intent.get('segments', [])
    if not isinstance(segments, list) or any(not isinstance(segment, dict) for segment in segments):
        raise DataError('持久派发分段无效', 409)
    progress = {'state', 'native_started', 'native_receipt', 'receipt', 'updated_at', 'reason'}
    return [{key: value for key, value in segment.items() if key not in progress} for segment in segments]


def present_batch(batch, counts):
    batch['summary'] = {'total': len(batch['inputs']), 'processed': 0, 'succeeded': 0, 'failed': 0,
        'execution_complete': batch['status'] == 'succeeded', **batch['summary'], **counts,
        'review_complete': batch['status'] == 'succeeded' and batch['coverage']['index_complete']
        and not counts['pending'] and not counts['deferred']
        and not batch['summary'].get('pending_operations', 0)}
    active = {'queued', 'running', 'waiting_resource', 'cancelling'}
    retryable = {'failed', 'partial', 'waiting_input', 'needs_verification'}
    batch['capabilities'] = ['cancel'] if batch['status'] in active else ['retry'] if batch['status'] in retryable else []
    if batch['template']['scenario'] == 'ocr':
        batch['summary']['review_scope'] = 'platform_fixed_result'
        batch['summary']['native_review_qualification'] = 'independent'
    return batch


class BatchStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'batches.sqlite3'
        self.lock = threading.RLock()
        with closing(self.db()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS templates(template_id TEXT,version INTEGER,
                    workspace_id TEXT,body TEXT,PRIMARY KEY(template_id,version));
                CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY,request_key TEXT UNIQUE,
                    workspace_id TEXT,fingerprint TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS disposition_events(event_id TEXT PRIMARY KEY,
                    batch_id TEXT,item_id TEXT,action TEXT,note TEXT,created_at TEXT);
                CREATE TABLE IF NOT EXISTS batch_items(
                    batch_id TEXT NOT NULL,item_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
                    result_identity TEXT NOT NULL,is_result INTEGER NOT NULL,
                    category TEXT NOT NULL,original_body TEXT NOT NULL,
                    disposition TEXT NOT NULL,note TEXT NOT NULL,updated_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id,item_id));
                CREATE INDEX IF NOT EXISTS batch_items_page ON batch_items(batch_id,ordinal,item_id);
                CREATE INDEX IF NOT EXISTS batch_items_state ON batch_items(batch_id,disposition,ordinal,item_id);
                CREATE INDEX IF NOT EXISTS batch_items_counts ON batch_items(batch_id,is_result,disposition);
                CREATE TABLE IF NOT EXISTS batch_indexes(
                    batch_id TEXT PRIMARY KEY,result_identity TEXT NOT NULL,
                    result_rows INTEGER NOT NULL,required_items INTEGER NOT NULL,
                    left_rows INTEGER NOT NULL,right_rows INTEGER NOT NULL,
                    scanned_results INTEGER NOT NULL,next_ordinal INTEGER NOT NULL,
                    index_complete INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS batch_disposition_operations(
                    operation_id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,
                    batch_id TEXT NOT NULL,payload_hash TEXT NOT NULL,expected_revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,intent TEXT NOT NULL,state TEXT NOT NULL,
                    native_receipt TEXT NOT NULL,receipt TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS batch_operations_state ON batch_disposition_operations(batch_id,state);
                CREATE UNIQUE INDEX IF NOT EXISTS batch_operation_reservation
                    ON batch_disposition_operations(batch_id) WHERE state<>'committed';
                CREATE TABLE IF NOT EXISTS batch_disposition_history(
                    event_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,item_id TEXT NOT NULL,
                    operation_id TEXT,revision INTEGER,action TEXT NOT NULL,note TEXT NOT NULL,
                    actor TEXT,previous_state TEXT NOT NULL,current_state TEXT NOT NULL,
                    adoption TEXT,legacy INTEGER NOT NULL,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS batch_history_page ON batch_disposition_history(batch_id,created_at,event_id);
                CREATE TABLE IF NOT EXISTS batch_item_adoptions(
                    batch_id TEXT NOT NULL,item_id TEXT NOT NULL,operation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id,item_id));
                CREATE TABLE IF NOT EXISTS batch_review_requests(
                    request_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    bundle_id TEXT NOT NULL UNIQUE,snapshot_hash TEXT NOT NULL,
                    snapshot TEXT NOT NULL,receipt TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batch_todo_identity(
                    todo_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,item_id TEXT NOT NULL,
                    UNIQUE(batch_id,item_id));
                CREATE TABLE IF NOT EXISTS workspace_todo_revisions(
                    workspace_id TEXT PRIMARY KEY,revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO workspace_todo_revisions SELECT DISTINCT workspace_id,1 FROM batches;
                CREATE TRIGGER IF NOT EXISTS batch_todo_generation_insert AFTER INSERT ON batches BEGIN
                    INSERT INTO workspace_todo_revisions VALUES(NEW.workspace_id,1)
                    ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1;
                END;
                CREATE TRIGGER IF NOT EXISTS batch_todo_generation_update AFTER UPDATE ON batches BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1 WHERE workspace_id=NEW.workspace_id;
                END;
                CREATE TRIGGER IF NOT EXISTS batch_todo_generation_delete AFTER DELETE ON batches BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1 WHERE workspace_id=OLD.workspace_id;
                    DELETE FROM batch_todo_identity WHERE batch_id=OLD.batch_id;
                END;
                CREATE TRIGGER IF NOT EXISTS item_todo_generation_insert AFTER INSERT ON batch_items BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1
                    WHERE workspace_id=(SELECT workspace_id FROM batches WHERE batch_id=NEW.batch_id);
                END;
                CREATE TRIGGER IF NOT EXISTS item_todo_generation_update AFTER UPDATE ON batch_items BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1
                    WHERE workspace_id=(SELECT workspace_id FROM batches WHERE batch_id=NEW.batch_id);
                END;
                CREATE TRIGGER IF NOT EXISTS item_todo_generation_delete AFTER DELETE ON batch_items BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1
                    WHERE workspace_id=(SELECT workspace_id FROM batches WHERE batch_id=OLD.batch_id);
                    DELETE FROM batch_todo_identity WHERE batch_id=OLD.batch_id AND item_id=OLD.item_id;
                END;
                CREATE TRIGGER IF NOT EXISTS operation_todo_generation_insert AFTER INSERT ON batch_disposition_operations BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1
                    WHERE workspace_id=(SELECT workspace_id FROM batches WHERE batch_id=NEW.batch_id);
                END;
                CREATE TRIGGER IF NOT EXISTS operation_todo_generation_update AFTER UPDATE ON batch_disposition_operations BEGIN
                    UPDATE workspace_todo_revisions SET revision=revision+1
                    WHERE workspace_id=(SELECT workspace_id FROM batches WHERE batch_id=NEW.batch_id);
                END;
            ''')
            db.execute('BEGIN IMMEDIATE')
            # Derived identity migration streams only IDs inside SQLite. No
            # result bodies or complete issue collections enter Python.
            db.create_function('todo_identity', 3, todo_identity, deterministic=True)
            db.execute("INSERT OR IGNORE INTO batch_todo_identity SELECT todo_identity('issue',batch_id,item_id),"
                       'batch_id,item_id FROM batch_items')
            # One-time read compatibility: move old JSON issues to independent
            # rows without changing their fixed original output or decisions.
            for row in db.execute('SELECT batch_id,body FROM batches'):
                body = json.loads(row['body'])
                defaults_missing = 'revision' not in body or 'automation_version' not in body or 'delivery' not in body
                body.setdefault('revision', 1)
                body.setdefault('automation_version', '2.0')
                body.setdefault('delivery', {'status': 'none'})
                if body.get('issues'):
                    self._write_items(db, row['batch_id'], body['issues'])
                if defaults_missing or body.get('issues') or any(isinstance(value, dict) and 'issues' in value
                                            for value in body.get('receipts', {}).values()):
                    body['issues'] = []
                    for value in body.get('receipts', {}).values():
                        if isinstance(value, dict):
                            value.pop('issues', None)
                    db.execute('UPDATE batches SET body=? WHERE batch_id=?', (encoded(body), row['batch_id']))
            # Legacy rows never recorded an actor or an exact revision. Preserve
            # that uncertainty explicitly instead of fabricating either value.
            db.execute('INSERT OR IGNORE INTO batch_disposition_history '
                'SELECT event_id,batch_id,item_id,NULL,NULL,action,note,NULL,\'{}\',\'{}\',NULL,1,created_at '
                'FROM disposition_events')

    def _write_items(self, db, batch_id, issues):
        for index, item in enumerate(issues):
            original = {key: value for key, value in item.items()
                        if key not in {'disposition', 'note', 'updated_at'}}
            source = item.get('source', {})
            identity = source.get('result_id') or 'native:' + batch_id
            category = item.get('category', 'review')
            is_result = bool(source.get('result_id') or category in {'review', 'execution'})
            ordinal = item.get('raw_output', {}).get('ordinal', index if is_result else 1_000_000_000 + index)
            db.execute('INSERT INTO batch_items VALUES(?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(batch_id,item_id) DO NOTHING',
                       (batch_id, item['item_id'], ordinal, identity, int(is_result), category,
                        encoded(original), item.get('disposition', 'pending'), item.get('note', ''),
                        item.get('updated_at', now())))
            db.execute('INSERT OR IGNORE INTO batch_todo_identity VALUES(?,?,?)',
                       (todo_identity('issue', batch_id, item['item_id']), batch_id, item['item_id']))

    def _item(self, row):
        item = {**json.loads(row['original_body']), 'disposition': row['disposition'],
                'note': row['note'], 'updated_at': row['updated_at']}
        if 'adopted_body' in row.keys() and row['adopted_body'] is not None:
            item['adoption'] = json.loads(row['adopted_body'])
        return item

    def issue_page(self, batch_id, offset=0, limit=30, disposition=None):
        with closing(self.db()) as db:
            db.execute('BEGIN')
            return self._issue_page(db, batch_id, offset, limit, disposition)

    def _issue_page(self, db, batch_id, offset=0, limit=30, disposition=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise DataError('事项分页参数无效')
        if disposition is not None and disposition not in {'pending', 'confirmed', 'deferred'}:
            raise DataError('事项处置筛选无效')
        where, args = 'i.batch_id=?', [batch_id]
        if disposition:
            where += ' AND i.disposition=?'
            args.append(disposition)
        total = db.execute('SELECT count(*) FROM batch_items i WHERE ' + where, args).fetchone()[0]
        rows = db.execute('SELECT i.*,a.body AS adopted_body FROM batch_items i LEFT JOIN batch_item_adoptions a '
                          'ON a.batch_id=i.batch_id AND a.item_id=i.item_id WHERE ' + where +
                          ' ORDER BY i.ordinal,i.item_id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return {'items': [self._item(row) for row in rows], 'total': total, 'offset': offset}

    def selected_items(self, batch_id, item_ids):
        with closing(self.db()) as db:
            return self._selected_items(db, batch_id, item_ids)

    def _selected_items(self, db, batch_id, item_ids):
        if not item_ids or len(item_ids) > 1000 or len(set(item_ids)) != len(item_ids):
            raise DataError('请选择本批次明确存在的结果范围', 409)
        rows = db.execute('SELECT i.*,a.body AS adopted_body FROM batch_items i LEFT JOIN batch_item_adoptions a '
                          'ON a.batch_id=i.batch_id AND a.item_id=i.item_id WHERE i.batch_id=? AND i.item_id IN (' +
                          ','.join('?' for _ in item_ids) + ')', (batch_id, *item_ids)).fetchall()
        if len(rows) != len(item_ids):
            raise DataError('请选择本批次明确存在的结果范围', 409)
        by_id = {row['item_id']: self._item(row) for row in rows}
        return [by_id[item_id] for item_id in item_ids]

    def counts(self, batch_id, *, result_only=False):
        with closing(self.db()) as db:
            return self._counts(db, batch_id, result_only=result_only)

    def _counts(self, db, batch_id, *, result_only=False):
        rows = db.execute('SELECT disposition,count(*) AS count FROM batch_items WHERE batch_id=?' +
                          (' AND is_result=1' if result_only else '') + ' GROUP BY disposition', (batch_id,))
        observed = {row['disposition']: row['count'] for row in rows}
        return {state: observed.get(state, 0) for state in ('pending', 'confirmed', 'deferred')}

    def coverage(self, batch_id, input_files):
        with closing(self.db()) as db:
            db.execute('BEGIN')
            return self._coverage(db, batch_id, input_files)

    def _coverage(self, db, batch_id, input_files):
        row = db.execute('SELECT * FROM batch_indexes WHERE batch_id=?', (batch_id,)).fetchone()
        counts = self._counts(db, batch_id, result_only=True)
        indexed = sum(counts.values())
        return {'input_files': input_files, 'left_rows': row['left_rows'] if row else 0,
                'right_rows': row['right_rows'] if row else 0, 'result_rows': row['result_rows'] if row else indexed,
                'required_items': row['required_items'] if row else indexed, 'indexed_items': indexed,
                'scanned_results': row['scanned_results'] if row else 0,
                'next_ordinal': row['next_ordinal'] if row else 0,
                'index_complete': bool(row and row['index_complete']), **counts}

    def begin_index(self, batch_id, result_identity, total, required, left_rows, right_rows):
        if any(type(value) is not int or value < 0 for value in (total, required, left_rows, right_rows)) or required > total:
            raise DataError('固定结果覆盖计数无效', 409)
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM batch_indexes WHERE batch_id=?', (batch_id,)).fetchone()
            if old:
                if (old['result_identity'], old['result_rows'], old['required_items'], old['left_rows'], old['right_rows']) != (
                        result_identity, total, required, left_rows, right_rows):
                    raise DataError('固定结果身份或覆盖已变化，不能替代原事项索引', 409)
            else:
                db.execute('INSERT INTO batch_indexes VALUES(?,?,?,?,?,?,0,0,0)',
                           (batch_id, result_identity, total, required, left_rows, right_rows))
                db.execute('DELETE FROM batch_items WHERE batch_id=? AND is_result=0', (batch_id,))
        return self.coverage(batch_id, 0)

    def index_page(self, batch_id, result_identity, start, rows, issues):
        """Items and their resume cursor commit together, including empty same pages."""
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            index = db.execute('SELECT * FROM batch_indexes WHERE batch_id=?', (batch_id,)).fetchone()
            if not index or index['result_identity'] != result_identity:
                raise DataError('事项索引固定身份不匹配', 409)
            if start != index['next_ordinal']:
                # A replayed page must refer to exactly the already committed rows.
                if start < index['next_ordinal'] and rows and rows[-1]['ordinal'] < index['next_ordinal']:
                    for item in issues:
                        existing = db.execute('SELECT original_body FROM batch_items WHERE batch_id=? AND item_id=?',
                                              (batch_id, item['item_id'])).fetchone()
                        original = {key: value for key, value in item.items()
                                    if key not in {'disposition', 'note', 'updated_at'}}
                        if not existing or existing['original_body'] != encoded(original):
                            raise DataError('已提交索引页内容不匹配', 409)
                    return
                raise DataError('事项索引恢复位置不匹配', 409)
            if (len(rows) > 1000 or not rows or any(row['ordinal'] != start + offset for offset, row in enumerate(rows))
                    or start + len(rows) > index['result_rows']):
                raise DataError('固定结果分页不连续或超出覆盖范围', 409)
            self._write_items(db, batch_id, issues)
            scanned = index['scanned_results'] + len(rows)
            indexed = db.execute('SELECT count(*) FROM batch_items WHERE batch_id=? AND is_result=1', (batch_id,)).fetchone()[0]
            if indexed > index['required_items']:
                raise DataError('事项索引数量超过固定结果覆盖', 409)
            complete = scanned == index['result_rows'] and indexed == index['required_items']
            db.execute('UPDATE batch_indexes SET scanned_results=?,next_ordinal=?,index_complete=? WHERE batch_id=?',
                       (scanned, start + len(rows), int(complete), batch_id))

    def complete_empty_index(self, batch_id):
        with closing(self.db()) as db, db:
            db.execute('UPDATE batch_indexes SET index_complete=1 WHERE batch_id=? AND result_rows=0 AND required_items=0',
                       (batch_id,))

    def clear_technical_issues(self, batch_id):
        with closing(self.db()) as db, db:
            db.execute('DELETE FROM batch_items WHERE batch_id=? AND is_result=0', (batch_id,))

    def db(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def save_template(self, value):
        template_id = value.get('template_id') or 't' + uuid.uuid4().hex
        with closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT workspace_id,max(version) AS version FROM templates '
                                  'WHERE template_id=?', (template_id,)).fetchone()
            if previous['version'] and previous['workspace_id'] != value['workspace_id']:
                raise DataError('模板属于其他工作空间', 409)
            body = {**value, 'template_id': template_id, 'version': (previous['version'] or 0) + 1,
                    'config_hash': fingerprint({'scenario': value['scenario'], 'config': value['config']}),
                    'created_at': now()}
            db.execute('INSERT INTO templates VALUES(?,?,?,?)',
                       (template_id, body['version'], value['workspace_id'], encoded(body)))
        return body

    def template(self, workspace_id, template_id, version):
        with closing(self.db()) as db:
            row = db.execute('SELECT body FROM templates WHERE template_id=? AND version=? AND workspace_id=?',
                             (template_id, version, workspace_id)).fetchone()
        if not row:
            raise DataError('模板版本不存在', 404)
        return json.loads(row['body'])

    def templates(self, workspace_id):
        with closing(self.db()) as db:
            return [json.loads(row['body']) for row in db.execute(
                'SELECT t.body FROM templates t WHERE workspace_id=? AND version='
                '(SELECT max(version) FROM templates p WHERE p.template_id=t.template_id) '
                'ORDER BY rowid DESC', (workspace_id,))]

    def accept(self, request, template):
        digest = fingerprint(request)
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM batches WHERE request_key=?', (request['request_key'],)).fetchone()
            if existing:
                if existing['fingerprint'] != digest:
                    raise DataError('相同运行请求标识不能用于不同资料或模板', 409)
                return json.loads(existing['body']), False
            body = {**request, 'batch_id': 'b' + uuid.uuid4().hex, 'template': template,
                    'automation_version': '2.0', 'revision': 1, 'delivery': {'status': 'none'},
                    'status': 'queued', 'stage': 'preflight', 'summary': {}, 'issues': [],
                    'receipts': {}, 'artifact_state': 'not_generated', 'artifacts': [],
                    'capabilities': ['cancel'], 'cancel_requested': False,
                    'created_at': now(), 'updated_at': now()}
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?)', (body['batch_id'], request['request_key'],
                       request['workspace_id'], digest, encoded(body)))
        return body, True

    def get(self, batch_id):
        return self.snapshot(batch_id)[0]

    def snapshot(self, batch_id, offset=0, limit=30, disposition=None):
        with closing(self.db()) as db:
            db.execute('BEGIN')
            return self._snapshot(db, batch_id, offset, limit, disposition)

    def _snapshot(self, db, batch_id, offset=0, limit=30, disposition=None):
        row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if not row:
            raise DataError('批次不存在', 404)
        body = json.loads(row['body'])
        body.setdefault('automation_version', '2.0')
        body.setdefault('revision', 1)
        body.setdefault('delivery', {'status': 'none'})
        page = self._issue_page(db, batch_id, offset, limit, disposition)
        body['issues'] = page['items']
        body['coverage'] = self._coverage(db, batch_id, len(body['inputs']))
        pending = db.execute("SELECT operation_id,request_id,state FROM batch_disposition_operations "
                             "WHERE batch_id=? AND state<>'committed'", (batch_id,)).fetchone()
        body['summary']['pending_operations'] = int(pending is not None)
        body['summary']['pending_operation'] = dict(pending) if pending else None
        return body, self._counts(db, batch_id), page

    def list(self, workspace_id=None, request_key=None):
        with closing(self.db()) as db:
            rows = db.execute('SELECT body FROM batches ORDER BY rowid DESC').fetchall()
        return [body for row in rows if (body := json.loads(row['body']))
                and (not workspace_id or body['workspace_id'] == workspace_id)
                and (not request_key or body['request_key'] == request_key)]

    def update(self, batch_id, **changes):
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if not row:
                raise DataError('批次不存在', 404)
            body = json.loads(row['body'])
            if 'issues' in changes:
                self._write_items(db, batch_id, changes.pop('issues'))
                changes['issues'] = []
            if 'receipts' in changes:
                changes['receipts'] = {key: {field: value for field, value in receipt.items() if field != 'issues'}
                                       if isinstance(receipt, dict) else receipt
                                       for key, receipt in changes['receipts'].items()}
            body.update(changes)
            body['updated_at'] = now()
            db.execute('UPDATE batches SET body=? WHERE batch_id=?', (encoded(body), batch_id))
        return body

    def begin_operation(self, batch_id, payload, *, origin=None, segments=None):
        from packages.contracts.automation import BatchDisposition
        from pydantic import ValidationError
        try:
            value = BatchDisposition.model_validate(payload).model_dump(mode='json')
        except ValidationError:
            raise DataError('处置必须带所见版本、请求身份和本机操作人') from None
        digest = fingerprint({'batch_id': batch_id, 'payload': value})
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM batch_disposition_operations WHERE request_id=?',
                                  (value['request_id'],)).fetchone()
            if previous:
                if previous['payload_hash'] != digest or previous['batch_id'] != batch_id:
                    raise DataError('相同处置请求标识不能用于不同批次或内容', 409)
                return self._operation(previous, private=True)
            row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if not row:
                raise DataError('批次不存在', 404)
            body = json.loads(row['body'])
            if value['action'] == 'adopt' and body['template']['scenario'] == 'data_compare':
                raise DataError('采用原生修订只适用于分类或 OCR 批次', 409)
            if body.get('revision', 1) != value['expected_revision']:
                raise DataError('处置版本冲突，请重新检查已显示内容；不能刷新版本后覆盖判断', 409)
            pending = db.execute("SELECT operation_id FROM batch_disposition_operations WHERE batch_id=? AND state<>'committed'",
                                 (batch_id,)).fetchone()
            if pending:
                raise DataError('已有处置待核实，请先恢复原操作 ' + pending['operation_id'], 409)
            targets = self._selected_items(db, batch_id, value['item_ids'])
            operation_id = 'op' + uuid.uuid4().hex
            created = now()
            intent = {'workspace_id': body['workspace_id'], 'scenario': body['template']['scenario'],
                      'template_id': body['template_id'], 'template_version': body['template_version'],
                      'inputs': body['inputs'], 'targets': targets, 'origin': origin or {},
                      'native_started': False}
            if segments:
                intent['segments'] = [{**segment, 'operation_id': operation_id + '_s' + str(index),
                    'native_started': False, 'state': 'intent'} for index, segment in enumerate(segments)]
            db.execute('INSERT INTO batch_disposition_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (operation_id, value['request_id'], batch_id, digest, value['expected_revision'], encoded(value),
                        encoded(intent), 'intent', '{}', '{}', created, created))
            operation = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            return self._operation(operation, private=True)

    def request_operation(self, batch_id, payload):
        digest = fingerprint({'batch_id': batch_id, 'payload': payload})
        with closing(self.db()) as db:
            row = db.execute('SELECT * FROM batch_disposition_operations WHERE request_id=?',
                             (payload['request_id'],)).fetchone()
        if row and (row['batch_id'] != batch_id or row['payload_hash'] != digest):
            raise DataError('相同处置请求标识不能用于不同批次或内容', 409)
        return self._operation(row, private=True) if row else None

    def native_revisions(self, batch_id):
        """Use prior committed receipts, never silently substitute live revisions."""
        revisions = {}
        with closing(self.db()) as db:
            rows = db.execute("SELECT native_receipt FROM batch_disposition_operations "
                "WHERE batch_id=? AND state='committed' AND native_receipt<>'{}' ORDER BY expected_revision DESC", (batch_id,))
            for row in rows:
                receipt = json.loads(row['native_receipt'])
                for segment in receipt.get('segments', []):
                    for item_id, revision in segment.get('revisions', {}).items():
                        revisions.setdefault(item_id, revision)
        return revisions

    def start_segment(self, operation_id, segment_id):
        """Claim first dispatch durably; a second worker can only reconcile it."""
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                raise DataError('处置操作不存在', 404)
            intent = json.loads(row['intent'])
            segment = next((value for value in intent.get('segments', []) if value['operation_id'] == segment_id), None)
            if segment is None:
                raise DataError('派发分段不属于原操作', 409)
            if row['state'] == 'committed' or segment.get('native_started'):
                return self._operation(row, private=True), False
            if json.loads(row['native_receipt']):
                raise DataError('已核对操作不能重新派发', 409)
            segment['native_started'] = intent['native_started'] = True
            db.execute('UPDATE batch_disposition_operations SET intent=?,updated_at=? WHERE operation_id=?',
                       (encoded(intent), now(), operation_id))
            updated = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            return self._operation(updated, private=True), True

    def reject_native_conflict(self, operation_id, evidence):
        """Only a definite first POST 409 with no effects releases reservation."""
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                raise DataError('处置操作不存在', 404)
            if row['state'] == 'committed':
                return json.loads(row['receipt'])
            intent = json.loads(row['intent'])
            started = [segment for segment in intent.get('segments', []) if segment.get('native_started')]
            if (json.loads(row['native_receipt']) or len(started) != 1
                    or any(segment.get('native_receipt') for segment in intent.get('segments', []))
                    or evidence.get('source') != 'conditional_post_409' or evidence.get('status_code') != 409
                    or evidence.get('operation_id') != started[0]['operation_id']
                    or evidence.get('payload_hash') != started[0]['payload_hash']):
                raise DataError('尚不能证明原生未执行，保持待核实', 409)
            receipt = {'operation_id': operation_id, 'request_id': row['request_id'], 'outcome': 'conflict',
                'revision': row['expected_revision'], 'reason': '所见原生版本冲突；原生未写入，平台判断未改变',
                'next_action': 'inspect_native_revision', 'evidence': evidence}
            db.execute("UPDATE batch_disposition_operations SET state='committed',receipt=?,updated_at=? WHERE operation_id=?",
                       (encoded(receipt), now(), operation_id))
            return receipt

    def _operation(self, row, *, private=False):
        payload = json.loads(row['payload'])
        result = {'schema_version': '1.0', 'operation_id': row['operation_id'], 'batch_id': row['batch_id'],
                  'request_id': row['request_id'], 'payload_hash': row['payload_hash'],
                  'expected_revision': row['expected_revision'], 'actor': payload['actor'],
                  'state': row['state'], 'receipt': json.loads(row['receipt']),
                  'created_at': row['created_at'], 'updated_at': row['updated_at']}
        if private:
            result.update(payload=payload, intent=json.loads(row['intent']), native_receipt=json.loads(row['native_receipt']))
        return result

    def operation(self, operation_id, *, private=False):
        with closing(self.db()) as db:
            row = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if not row:
            raise DataError('处置操作不存在', 404)
        return self._operation(row, private=private)

    def checkpoint_operation(self, operation_id, state, *, native_receipt=None, reason=None, intent=None):
        if state not in {'intent', 'native_committed', 'needs_verification'}:
            raise DataError('处置恢复状态无效')
        if native_receipt is not None and not isinstance(native_receipt, dict):
            raise DataError('原生操作回执无效', 409)
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not row:
                raise DataError('处置操作不存在', 404)
            if row['state'] == 'committed':
                return self._operation(row, private=True)
            saved_native = json.loads(row['native_receipt'])
            if row['state'] == 'native_committed' or saved_native:
                if state == 'intent':
                    raise DataError('已核对原生回执不能退回首次派发状态', 409)
                if native_receipt is not None and native_receipt != saved_native:
                    raise DataError('已核对原生回执不能替换', 409)
            effective_native = native_receipt if native_receipt is not None else saved_native
            if state == 'native_committed' and (not isinstance(effective_native, dict) or not effective_native):
                raise DataError('原生完成必须有可核对的操作回执', 409)
            saved_intent = json.loads(row['intent'])
            if intent is not None:
                # Origin/targets were fixed before the native write. Only
                # dispatch bookkeeping may advance within that fixed intent.
                if any(intent.get(key) != saved_intent.get(key) for key in
                       ('workspace_id', 'scenario', 'template_id', 'template_version', 'inputs', 'targets', 'origin')):
                    raise DataError('不能改写原操作的来源、实例或用户所见结果', 409)
                if saved_intent.get('native_started') and not intent.get('native_started'):
                    raise DataError('不能清除原生已派发操作身份', 409)
                if saved_intent.get('native_started'):
                    if dispatch_bindings(intent) != dispatch_bindings(saved_intent):
                        raise DataError('已派发分段的操作身份、目标或请求绑定不能替换', 409)
                    for previous, current in zip(saved_intent.get('segments', []), intent.get('segments', [])):
                        if previous.get('native_started') and not current.get('native_started'):
                            raise DataError('不能清除分段已派发操作身份', 409)
                        for field in ('native_receipt', 'receipt'):
                            if previous.get(field) and current.get(field) != previous[field]:
                                raise DataError('已核对分段回执不能替换', 409)
                saved_intent = intent
            receipt = json.loads(row['receipt'])
            if reason:
                receipt.update(reason=reason, next_action='recover')
            db.execute('UPDATE batch_disposition_operations SET state=?,native_receipt=?,intent=?,receipt=?,updated_at=? '
                       'WHERE operation_id=?', (state, encoded(native_receipt) if native_receipt is not None else row['native_receipt'],
                       encoded(saved_intent), encoded(receipt), now(), operation_id))
            updated = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            return self._operation(updated, private=True)

    def commit_operation(self, operation_id, *, adoption=None):
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            operation = db.execute('SELECT * FROM batch_disposition_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if not operation:
                raise DataError('处置操作不存在', 404)
            if operation['state'] == 'committed':
                return json.loads(operation['receipt'])
            payload, intent = json.loads(operation['payload']), json.loads(operation['intent'])
            platform_only = (intent['scenario'] == 'ocr' and intent['origin'].get('effect') == 'platform_only'
                and payload['action'] in {'defer', 'reopen'} and not intent.get('native_started')
                and not json.loads(operation['native_receipt']) and not intent.get('segments'))
            if intent['scenario'] != 'data_compare' and not platform_only and operation['state'] != 'native_committed':
                raise DataError('原生处置尚未取得核对回执，保持待恢复', 409)
            if payload['action'] == 'adopt' and not isinstance(adoption, dict):
                raise DataError('采用修订必须携带已核对的采用关系', 409)
            batch_id = operation['batch_id']
            row = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if not row:
                raise DataError('批次不存在', 404)
            body = json.loads(row['body'])
            if body.get('revision', 1) != operation['expected_revision']:
                raise DataError('平台处置版本已变化，原操作保持待核实，不能覆盖判断', 409)
            selected = self._selected_items(db, batch_id, payload['item_ids'])
            if fingerprint(selected) != fingerprint(intent['targets']):
                raise DataError('平台事项或采用关系已变化，原操作保持待核实', 409)
            state = {'confirm': 'confirmed', 'adopt': 'confirmed', 'defer': 'deferred', 'reopen': 'pending'}[payload['action']]
            revision, created = operation['expected_revision'] + 1, now()
            for previous in selected:
                item_id = previous['item_id']
                current = {**previous, 'disposition': state, 'note': payload['note'], 'updated_at': created}
                if payload['action'] == 'adopt':
                    current['adoption'] = adoption
                    db.execute('INSERT INTO batch_item_adoptions VALUES(?,?,?,?,?,?) '
                               'ON CONFLICT(batch_id,item_id) DO UPDATE SET operation_id=excluded.operation_id,'
                               'revision=excluded.revision,body=excluded.body,created_at=excluded.created_at',
                               (batch_id, item_id, operation_id, revision, encoded(adoption), created))
                db.execute('UPDATE batch_items SET disposition=?,note=?,updated_at=? WHERE batch_id=? AND item_id=?',
                           (state, payload['note'], created, batch_id, item_id))
                event_id = 'e' + uuid.uuid4().hex
                db.execute('INSERT INTO disposition_events VALUES(?,?,?,?,?,?)',
                           (event_id, batch_id, item_id, payload['action'], payload['note'], created))
                db.execute('INSERT INTO batch_disposition_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (event_id, batch_id, item_id, operation_id, revision, payload['action'], payload['note'],
                            encoded(payload['actor']), encoded(previous), encoded(current),
                            encoded(adoption) if payload['action'] == 'adopt' else None, 0, created))
            body.update(revision=revision, automation_version='2.0', updated_at=created)
            if body.get('delivery', {}).get('status') in {'current', 'outdated'}:
                body['delivery']['status'] = 'outdated'
            db.execute('UPDATE batches SET body=? WHERE batch_id=?', (encoded(body), batch_id))
            db.execute("UPDATE batch_disposition_operations SET state='committed',updated_at=? WHERE operation_id=?",
                       (created, operation_id))
            snapshot, counts, _ = self._snapshot(db, batch_id)
            receipt = {'operation_id': operation_id, 'request_id': operation['request_id'], 'revision': revision,
                       'item_ids': payload['item_ids'], 'action': payload['action'],
                       'native_receipt': json.loads(operation['native_receipt']), 'batch': present_batch(snapshot, counts)}
            db.execute('UPDATE batch_disposition_operations SET receipt=? WHERE operation_id=?', (encoded(receipt), operation_id))
            return receipt

    def disposition(self, batch_id, payload):
        operation = self.begin_operation(batch_id, payload)
        return self.commit_operation(operation['operation_id'])

    def _review_snapshot(self, db, batch_id):
        body, counts, _ = self._snapshot(db, batch_id)
        rows = db.execute('SELECT i.*,a.body AS adopted_body FROM batch_items i LEFT JOIN batch_item_adoptions a '
            'ON a.batch_id=i.batch_id AND a.item_id=i.item_id WHERE i.batch_id=? ORDER BY i.ordinal,i.item_id',
            (batch_id,)).fetchall()
        items = [self._item(row) for row in rows]
        body['issues'] = items
        index = db.execute('SELECT * FROM batch_indexes WHERE batch_id=?', (batch_id,)).fetchone()
        history = []
        for row in db.execute('SELECT * FROM batch_disposition_history WHERE batch_id=? '
                'ORDER BY coalesce(revision,0),created_at,event_id', (batch_id,)):
            event = dict(row)
            for field in ('actor', 'previous_state', 'current_state', 'adoption'):
                event[field] = json.loads(event[field]) if event[field] else None
            event['legacy'] = bool(event['legacy'])
            history.append(event)
        operations = [self._operation(row, private=True) for row in db.execute(
            'SELECT * FROM batch_disposition_operations WHERE batch_id=? ORDER BY created_at,operation_id', (batch_id,))]
        return {'batch': present_batch(body, counts), 'items': items,
                'index': dict(index) if index else None, 'history': history, 'operations': operations}

    @staticmethod
    def _review_state_hash(snapshot):
        # Delivery attachment changes no review judgment. Exclude that token
        # from the state digest, then compare it separately on attachment.
        body = {key: value for key, value in snapshot['batch'].items() if key not in {'delivery', 'updated_at'}}
        return fingerprint({**snapshot, 'batch': body})

    def begin_review(self, batch_id, payload):
        from packages.contracts.automation import ReviewArtifactRequest
        from pydantic import ValidationError
        try:
            value = ReviewArtifactRequest.model_validate(payload).model_dump(mode='json')
        except ValidationError:
            raise DataError('复核成果必须带所见版本、请求身份和本机操作人') from None
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM batch_review_requests WHERE request_id=?', (value['request_id'],)).fetchone()
            digest = fingerprint({'batch_id': batch_id, 'payload': value})
            if previous:
                if previous['batch_id'] != batch_id or previous['payload_hash'] != digest:
                    raise DataError('同一成果请求身份不能替换内容', 409)
                return dict(previous)
            snapshot = self._review_snapshot(db, batch_id)
            batch = snapshot['batch']
            if batch['revision'] != value['expected_revision']:
                raise DataError('复核成果版本冲突，请检查所见内容', 409)
            if value['kind'] == 'final' and not batch['summary']['review_complete']:
                raise DataError('执行、全量覆盖或复核尚未完成，只能明确生成中间材料', 409)
            snapshot['request'] = value
            snapshot['predecessor_bundle_id'] = batch['delivery'].get('bundle_id')
            state_hash = self._review_state_hash({key: snapshot[key] for key in ('batch', 'items', 'index', 'history', 'operations')})
            db.execute('INSERT INTO batch_review_requests VALUES(?,?,?,?,?,?,?,?)',
                (value['request_id'], batch_id, digest, 'b' + uuid.uuid4().hex, state_hash, encoded(snapshot), '{}', now()))
            return dict(db.execute('SELECT * FROM batch_review_requests WHERE request_id=?', (value['request_id'],)).fetchone())

    def finish_review(self, request_id, bundle):
        with self.lock, closing(self.db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM batch_review_requests WHERE request_id=?', (request_id,)).fetchone()
            if not row or bundle['bundle_id'] != row['bundle_id']:
                raise DataError('复核成果不属于原请求', 409)
            receipt = json.loads(row['receipt'])
            if receipt:
                return receipt
            fixed = json.loads(row['snapshot'])
            current = self._review_snapshot(db, row['batch_id'])
            body = current['batch']
            same_state = self._review_state_hash(current) == row['snapshot_hash']
            same_delivery = body['delivery'].get('bundle_id') == fixed['batch']['delivery'].get('bundle_id')
            if same_state and same_delivery:
                body['delivery'] = {'revision': fixed['batch']['revision'], 'bundle_id': row['bundle_id'],
                                    'status': 'current', 'kind': fixed['request']['kind']}
                body['updated_at'] = now()
                # Persist the ordinary body, keeping SQL ledger items separate.
                saved = json.loads(db.execute('SELECT body FROM batches WHERE batch_id=?', (row['batch_id'],)).fetchone()[0])
                saved.update(delivery=body['delivery'], updated_at=body['updated_at'])
                db.execute('UPDATE batches SET body=? WHERE batch_id=?', (encoded(saved), row['batch_id']))
            response = json.loads(encoded(fixed['batch']))
            response['delivery'] = {'revision': fixed['batch']['revision'], 'bundle_id': row['bundle_id'],
                                   'status': 'current' if same_state and same_delivery else 'outdated',
                                   'kind': fixed['request']['kind']}
            response['artifacts'] = [*response['artifacts'], {**bundle, 'status': response['delivery']['status']}]
            # Keep ProcessingBatch's bounded preview while the package retains
            # the complete transaction snapshot.
            response['issues'] = response['issues'][:30]
            db.execute('UPDATE batch_review_requests SET receipt=? WHERE request_id=?', (encoded(response), request_id))
            return response

    def review_delivery_current(self, batch_id, bundle_id, snapshot_hash):
        with closing(self.db()) as db:
            db.execute('BEGIN')
            current = self._review_snapshot(db, batch_id)
            return (current['batch']['delivery'].get('bundle_id') == bundle_id
                    and self._review_state_hash(current) == snapshot_hash)

    def history(self, batch_id, offset=0, limit=30, *, item_id=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise DataError('处置历史分页参数无效')
        where, args = 'batch_id=?', [batch_id]
        if item_id:
            where += ' AND item_id=?'
            args.append(item_id)
        with closing(self.db()) as db:
            db.execute('BEGIN')
            batch = db.execute('SELECT body FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
            if not batch:
                raise DataError('批次不存在', 404)
            revision = json.loads(batch['body']).get('revision', 1)
            total = db.execute('SELECT count(*) FROM batch_disposition_history WHERE ' + where, args).fetchone()[0]
            rows = db.execute('SELECT * FROM batch_disposition_history WHERE ' + where +
                              ' ORDER BY coalesce(revision,0),created_at,event_id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        return {'schema_version': '1.0', 'batch_id': batch_id, 'revision': revision,
            'items': [{**dict(row), 'actor': json.loads(row['actor']) if row['actor'] else None,
            'previous_state': json.loads(row['previous_state']), 'current_state': json.loads(row['current_state']),
            'adoption': json.loads(row['adoption']) if row['adoption'] else None, 'legacy': bool(row['legacy'])} for row in rows],
            'total': total, 'offset': offset}

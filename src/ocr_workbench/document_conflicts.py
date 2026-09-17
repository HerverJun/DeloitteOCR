"""Explicit acknowledgement of user-reviewed overlap content; raw evidence stays immutable."""
import json
from ocr_workbench.document_store import fingerprint
from ocr_workbench.store import Conflict, now


def conflict_view(store, result_id):
    with store.transaction() as db:
        db.execute('BEGIN')
        row = db.execute('SELECT original,edited,revision FROM results WHERE id=?', (result_id,)).fetchone()
        if row is None: raise KeyError('结果不存在')
        conflicts = json.loads(row['original']).get('document', {}).get('conflicts', [])
        current = fingerprint(json.loads(row['edited']))
        decisions = {d['conflict_id']: d for d in db.execute('SELECT * FROM document_conflict_decisions WHERE result_id=?', (result_id,))}
        return {'revision': row['revision'], 'conflicts': [dict(c, reviewed=bool(c['id'] in decisions and decisions[c['id']]['edited_sha256'] == current)) for c in conflicts]}


def acknowledge_conflict(store, result_id, conflict_id, revision):
    with store.transaction() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT original,edited,revision FROM results WHERE id=?', (result_id,)).fetchone()
        if row is None: raise KeyError('结果不存在')
        if row['revision'] != revision: raise Conflict('结果已变化，请保存并重新核对页面内容')
        if not any(c['id'] == conflict_id for c in json.loads(row['original']).get('document', {}).get('conflicts', [])):
            raise ValueError('页面复核项不存在')
        db.execute('INSERT OR REPLACE INTO document_conflict_decisions VALUES(?,?,?,?,?)',
                   (result_id, conflict_id, revision, fingerprint(json.loads(row['edited'])), now()))
        return {'saved': True, 'revision': revision}

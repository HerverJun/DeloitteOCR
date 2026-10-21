"""SQLite transaction outbox installed by the trusted execution service.

Triggers capture every native write in the same transaction, including writes
from background processes which do not inherit request context. Source IDs and
generations are bound to central attempts before their events are acknowledged.
"""
import json
from pathlib import Path
import sqlite3
from contextlib import closing

SOURCES = {
 'documents/documents.sqlite3': [('documents.documents','documents','document_id','body')],
 'data-workbench/sessions.sqlite3': [('data.jobs','jobs','id',None)],
 'jobs.sqlite3': [('manual.manual_analysis','manual_analysis','job_id','body')],
 'forensics/forensics.sqlite3': [('forensics.inspections','inspections','inspection_id','body')],
 'native/ocr/workbench.sqlite3': [('ocr.tasks','tasks','id',None),('ocr.document_stages','document_stages','id',None)],
 'native/classification/platform.sqlite3': [('classification.jobs','jobs','id',None),('classification.items','job_items','job_id',None)],
 'batches/batches.sqlite3': [('batches.batches','batches','batch_id','body')],
 'agent/operations.sqlite3': [('agent.runs','runs','id',None),('agent.operations','operations','run_id',None),('agent.model_requests','model_requests','run_id',None)],
}

def install(path, definitions):
    if not path.is_file(): return False
    with closing(sqlite3.connect(path,timeout=15)) as db, db:
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('''CREATE TABLE IF NOT EXISTS central_task_outbox(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,
            source_id TEXT NOT NULL, generation INTEGER NOT NULL, payload TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
        db.execute('CREATE TABLE IF NOT EXISTS central_task_generations(namespace TEXT,source_id TEXT,generation INTEGER,PRIMARY KEY(namespace,source_id))')
        db.execute('CREATE INDEX IF NOT EXISTS central_outbox_source ON central_task_outbox(namespace,source_id,generation,seq)')
        db.execute('CREATE INDEX IF NOT EXISTS central_outbox_pending ON central_task_outbox(seq) WHERE acknowledged=0')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for namespace,table,identity,body in definitions:
            if table not in tables: continue
            columns=[r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            if identity not in columns: continue
            if body and body in columns:
                payload=f'NEW."{body}"'
                generation=f"COALESCE(json_extract(NEW.\"{body}\",'$.generation'),(SELECT generation FROM central_task_generations WHERE namespace='{namespace}' AND source_id=NEW.\"{identity}\"),1)"
            else:
                allowed=[c for c in columns if not any(x in c for x in ('password','token','secret','credential'))]
                pairs=','.join(f"'{column}',NEW.\"{column}\"" for column in allowed)
                payload=f'json_object({pairs})'
                generation='NEW."generation"' if 'generation' in columns else f"COALESCE((SELECT generation FROM central_task_generations WHERE namespace='{namespace}' AND source_id=NEW.\"{identity}\"),1)"
            for action in ('INSERT','UPDATE'):
                trigger=f"central_outbox_{table}_{action.lower()}"
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS "{trigger}" AFTER {action} ON "{table}"
                    BEGIN INSERT INTO central_task_outbox(namespace,source_id,generation,payload)
                    VALUES('{namespace}',NEW."{identity}",{generation},{payload}); END''')
    return True

def install_all(root):
    result=[]
    for relative,definitions in SOURCES.items():
        path=Path(root)/relative
        if install(path,definitions): result.append(path)
    return result

def pending(path,limit=200,after=0):
    with closing(sqlite3.connect(path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        return [dict(r) for r in db.execute('SELECT * FROM central_task_outbox WHERE acknowledged=0 AND seq>? ORDER BY seq LIMIT ?', (after,limit))]

def acknowledge(path,seq):
    with closing(sqlite3.connect(path,timeout=15)) as db,db:
        db.execute('UPDATE central_task_outbox SET acknowledged=1 WHERE seq=?',(seq,))

def backlog(root):
    sources={}
    for relative in SOURCES:
        path=Path(root)/relative
        if not path.is_file():continue
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='central_task_outbox' AND type='table'").fetchone():continue
            sources[relative]=db.execute('SELECT COUNT(*) FROM central_task_outbox WHERE acknowledged=0').fetchone()[0]
    return {'pending_events':sum(sources.values()),'sources':sources}

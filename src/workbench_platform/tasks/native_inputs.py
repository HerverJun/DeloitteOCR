"""Resolve original input sizes from server-owned native project records."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3


def resolve(root,application,payload,path):
    database=root/'native'/application/('workbench.sqlite3' if application=='ocr' else 'platform.sqlite3')
    if not database.is_file():return []
    items={}
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        if application=='ocr':
            versions=set(payload.get('version_ids',[]) or payload.get('versions',[]))
            if payload.get('version_id'):versions.add(payload['version_id'])
            match=re.search(r'/versions/([A-Za-z0-9_-]+)',path)
            if match:versions.add(match[1])
            match=re.search(r'/results/([A-Za-z0-9_-]+)',path)
            if match:
                row=db.execute('SELECT COALESCE(t.input_version_id,t.version_id) FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=?',(match[1],)).fetchone()
                if row:versions.add(row[0])
            documents=set()
            match=re.search(r'/documents/([A-Za-z0-9_-]+)',path)
            if match:documents.add(match[1])
            match=re.search(r'/pages/([A-Za-z0-9_-]+)',path)
            if match:
                row=db.execute('SELECT document_id FROM pages WHERE id=?',(match[1],)).fetchone()
                if row:documents.add(row[0])
            for version in versions:
                if not isinstance(version,str):continue
                row=db.execute('SELECT i.*,v.id AS version_id FROM versions v JOIN images i ON i.id=v.image_id WHERE v.id=?',(version,)).fetchone()
                if not row:continue
                document=db.execute('SELECT document_id FROM pages WHERE image_id=?',(row['id'],)).fetchone()
                if document:documents.add(document[0]);continue
                asset='ocr:'+row['id'];items[asset]={'asset_id':asset,'asset_revision':None,'blob_hash':row['sha256'],'name':row['name'],
                    'source_locator':{'application':'ocr','image_id':row['id'],'version_id':version},'_path':row['original_path']}
            for identifier in documents:
                row=db.execute('SELECT * FROM documents WHERE id=?',(identifier,)).fetchone()
                if row:
                    asset='ocr-document:'+identifier;items[asset]={'asset_id':asset,'asset_revision':None,'blob_hash':row['sha256'],'name':row['name'],'_path':row['original_path'],'source_locator':{'application':'ocr','document_id':identifier}}
        else:
            identifiers=set(payload.get('asset_ids',[]))
            if payload.get('asset_id'):identifiers.add(payload['asset_id'])
            for identifier in identifiers:
                row=db.execute('SELECT * FROM assets WHERE id=?',(identifier,)).fetchone()
                if row:
                    asset='classification:'+identifier;items[asset]={'asset_id':asset,'asset_revision':None,'blob_hash':row['sha256'],'name':row['name'],'_path':row['path'],'source_locator':{'application':'classification','asset_id':identifier}}
    for item in items.values():
        value=item.pop('_path');candidate=Path(value) if value else None
        if candidate and not candidate.is_absolute():candidate=root/'native'/application/candidate
        item['size_bytes']=candidate.stat().st_size if candidate and candidate.resolve().drive.upper()=='F:' and candidate.is_file() else None
    return list(items.values())


def known_sizes(engine,workspace,inputs):
    """Keep recorded original sizes when the current native file is unavailable."""
    from sqlalchemy import select
    from . import schema_v1 as s
    missing=[item for item in inputs if item.get('size_bytes') is None and item.get('blob_hash')]
    if not missing:return inputs
    with engine.connect() as db:
        for item in missing:
            item['size_bytes']=db.execute(select(s.task_inputs.c.size_bytes).join(s.tasks,s.task_inputs.c.task_id==s.tasks.c.id).where(
                s.tasks.c.workspace_id==workspace,s.task_inputs.c.blob_hash==item['blob_hash'],s.task_inputs.c.size_bytes.is_not(None)).limit(1)).scalar_one_or_none()
    return inputs

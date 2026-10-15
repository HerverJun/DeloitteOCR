"""Recover only recorded legacy input identities and verifiable file manifests."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
from sqlalchemy import select,update,func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from . import schema_v1 as s
from .database import f_path
from .files import hash_file
from .store import digest


def native_one(root,relative,table,column,identity):
    path=root/relative
    if not path.is_file():return None
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return None
        columns={row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if column not in columns:return None
        row=db.execute(f'SELECT * FROM "{table}" WHERE "{column}"=?',(identity,)).fetchone()
        return dict(row) if row else None


def enrich(db,root,task_id,run_id,namespace,body,source):
    inputs=body.get('inputs',[])
    if not isinstance(inputs,list):inputs=[]
    if body.get('asset_id'):inputs=[*inputs,body]
    if namespace=='data.jobs':
        request=native_one(root,'data-workbench/sessions.sqlite3','operation_requests','job_id',source['id'])
        if request:
            try:options=json.loads(request['request']).get('payload',{})
            except (ValueError,TypeError):options={}
            upload=native_one(root,'data-workbench/sessions.sqlite3','uploads','id',options.get('upload_id'))
            if upload:inputs=[*inputs,{'asset_id':upload.get('source_asset_id') or upload['id'],'asset_revision':upload.get('source_asset_revision'),
                'blob_hash':upload.get('sha256'),'size_bytes':upload.get('size'),'name':upload.get('name'),'source_locator':{'data_session_id':source.get('session_id'),'upload_id':upload['id']}}]
    if namespace=='ocr.tasks':
        version=native_one(root,'native/ocr/workbench.sqlite3','versions','id',source.get('input_version_id') or source.get('version_id'))
        image=native_one(root,'native/ocr/workbench.sqlite3','images','id',source.get('image_id'))
        if version:
            candidate=(root/'native/ocr'/version.get('path','')).resolve()
            size=candidate.stat().st_size if candidate.is_relative_to(root) and candidate.is_file() and hash_file(candidate)==version.get('sha256') else None
            inputs=[*inputs,{'asset_id':'native-ocr:'+str(source.get('image_id'))+':'+version['id'],'asset_revision':None,'blob_hash':version.get('sha256'),
                'size_bytes':size,'name':image.get('name') if image else None,'source_locator':{'application':'ocr','image_id':source.get('image_id'),'version_id':version['id']}}]
    for item in inputs:
        if not isinstance(item,dict) or not item.get('asset_id'):continue
        asset=native_one(root,'assets.sqlite3','registered_asset','asset_id',item['asset_id'])
        revision=item.get('asset_revision',item.get('revision'))
        matches=asset and (asset['revision'],asset['blob_hash'])==(revision,item.get('blob_hash'))
        identifier='legacy-input-'+digest([task_id,item['asset_id'],revision,item.get('blob_hash')])[:32]
        db.execute(pg_insert(s.task_inputs).values(id=identifier,task_id=task_id,asset_id=str(item['asset_id'])[:100],asset_revision=revision,
            blob_hash=item.get('blob_hash'),name=item.get('name') or (asset['original_name'] if matches else None),size_bytes=item.get('size_bytes',asset['size_bytes'] if matches else None),
            source_locator=item.get('source_locator',{})).on_conflict_do_nothing())
    rows=db.execute(select(s.task_inputs).where(s.task_inputs.c.task_id==task_id)).mappings().all()
    if rows:
        unique={(row['asset_id'],row['asset_revision'],row['blob_hash']):row for row in rows}
        total=sum(row['size_bytes'] for row in unique.values()) if all(row['size_bytes'] is not None for row in unique.values()) else None
        db.execute(update(s.tasks).where(s.tasks.c.id==task_id).values(file_count=len(unique),input_total_bytes=total))
    files=body.get('files',[])
    if not isinstance(files,list):files=[]
    location=native_one(root,'forensics/forensics.sqlite3','output_locations','inspection_id',body.get('inspection_id')) if namespace=='forensics.inspections' else None
    for item in files:
        if not isinstance(item,dict) or not item.get('name'):continue
        name=Path(item['name']).name;candidate=(root/'forensics'/location['path']/name).resolve() if location else None
        blob=item.get('sha256') or item.get('blob_hash');source_path=None
        if candidate and candidate.is_relative_to(root) and candidate.is_file() and hash_file(candidate)==blob:
            configured=os.environ.get('WORKBENCH_DATA_DIR')
            if configured:
                target=f_path(configured)/'task-files'/task_id/run_id/(blob+'-'+name);target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists():shutil.copy2(candidate,target)
                if hash_file(target)!=blob:raise ValueError('Legacy artifact copy failed integrity verification')
                source_path=str(target)
        identifier='legacy-file-'+digest([run_id,name,blob])[:32]
        db.execute(pg_insert(s.task_artifact_links).values(id=identifier,task_id=task_id,run_id=run_id,name=name,kind='output',blob_hash=blob,
            size_bytes=item.get('size_bytes',item.get('bytes')),source_path=source_path,missing=not bool(source_path)).on_conflict_do_nothing(index_elements=['id']))
    task=db.execute(select(s.tasks.c.summary).where(s.tasks.c.id==task_id)).scalar_one()
    missing=set(task.get('legacy_missing_fields',[]))
    if not rows:missing.add('input_identity')
    else:missing.discard('input_identity')
    db.execute(update(s.tasks).where(s.tasks.c.id==task_id).values(summary={**task,'legacy_missing_fields':sorted(missing)}))

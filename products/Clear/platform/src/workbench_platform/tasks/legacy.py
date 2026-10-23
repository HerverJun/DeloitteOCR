"""Repeatable read-only migration of native records and projection aliases."""
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from sqlalchemy import select,insert,update,func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .store import key,now,Ledger,wire,digest
from . import schema_v1 as s
from .legacy_evidence import enrich
from .worker import normalized,as_date

def rows(root,relative,table):
    path=root/relative
    if not path.is_file():return
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return
        for row in db.execute(f'SELECT * FROM "{table}"'):yield dict(row)

def import_legacy(engine,root):
    # Cold snapshots have an explicit consistency receipt. Live business roots
    # must be snapshotted by the backup command before import.
    if not (root/'manifest.json').is_file() and not (root/'legacy-snapshot.json').is_file():
        raise ValueError('Import requires a verified cold backup or an explicitly prepared legacy snapshot')
    if (root/'manifest.json').is_file():
        from .maintenance import verify_backup
        verify_backup(engine,root)
    elif json.loads((root/'legacy-snapshot.json').read_text('utf-8')).get('cold') is not True:
        raise ValueError('Legacy snapshot must explicitly confirm it is cold')
    if (root/'files').is_dir():root=root/'files'
    ledger=Ledger(engine);counts={'projects':0,'tasks':0,'aliases':0,'unknown_ownership':0}
    with engine.begin() as db:
        for item in rows(root,'workspaces.sqlite3','workspace') or ():
            identifier=item.get('workspace_id',item.get('id'))
            created=as_date(item.get('created_at')) or now()
            result=db.execute(pg_insert(s.workspaces).values(workspace_id=identifier,name=item['name'],revision=item.get('revision',1),created_at=created,updated_at=as_date(item.get('updated_at')) or created).on_conflict_do_nothing(index_elements=['workspace_id']).returning(s.workspaces.c.workspace_id)).first()
            counts['projects']+=int(result is not None)
        for item in rows(root,'workspaces.sqlite3','module_project_link') or ():
            link=dict(link_id=item.get('link_id',item.get('id')),workspace_id=item['workspace_id'],app_id=item.get('app_id',item.get('module_id')),
                module_project_id=item.get('module_project_id',item.get('native_project_id')),role=item.get('role','editor'),revision=item.get('revision',1))
            db.execute(pg_insert(s.module_project_links).values(**link).on_conflict_do_nothing(index_elements=['link_id']))
    sources=[('jobs.sqlite3','projected_jobs','projection','platform_job_id','payload'),
             ('jobs.sqlite3','manual_analysis','manual.manual_analysis','job_id','body'),
             ('forensics/forensics.sqlite3','inspections','forensics.inspections','inspection_id','body'),
             ('data-workbench/sessions.sqlite3','jobs','data.jobs','id',None),
             ('native/ocr/workbench.sqlite3','tasks','ocr.tasks','id',None),
             ('native/classification/platform.sqlite3','jobs','classification.jobs','id',None),
             ('batches/batches.sqlite3','batches','batches.batches','batch_id','body'),
             ('agent/operations.sqlite3','runs','agent.runs','id',None)]
    session_projects={row['id']:row.get('workspace_id') for row in rows(root,'data-workbench/sessions.sqlite3','sessions') or ()}
    for relative,table,namespace,id_field,body_field in sources:
        for source in rows(root,relative,table) or ():
            body=json.loads(source[body_field]) if body_field else dict(source)
            identifier=source[id_field];generation=int(body.get('generation',source.get('generation',1)))
            with engine.begin() as db:
                existing=db.execute(select(s.task_source_links).where(s.task_source_links.c.namespace==namespace,s.task_source_links.c.source_id==identifier,s.task_source_links.c.source_generation==generation)).mappings().first()
                if existing:
                    # Projection rows can precede the richer native record.
                    created=as_date(body.get('created_at') or body.get('created'))
                    if created:db.execute(update(s.tasks).where(s.tasks.c.id==existing['task_id'],s.tasks.c.created_at.is_(None)).values(created_at=created))
                    step_id='legacy-step-'+digest([namespace,identifier,generation])[:32]
                    db.execute(pg_insert(s.task_step_runs).values(id=step_id,task_id=existing['task_id'],run_id=existing['run_id'],step_key=(namespace+':'+identifier)[:200],attempt=1,
                        tool_id=namespace.split('.')[0],action=body.get('action') or body.get('kind') or 'legacy',status=normalized(body),native_id=identifier,native_generation=generation,
                        started_at=as_date(body.get('started_at') or body.get('started')),finished_at=as_date(body.get('finished_at') or body.get('finished')),error=body.get('error'),details={'legacy_source':namespace}).on_conflict_do_nothing(index_elements=['id']))
                    enrich(db,root,existing['task_id'],existing['run_id'],namespace,body,source)
                    continue
                workspace=body.get('workspace_id') or source.get('workspace_id')
                if namespace=='data.jobs':workspace=session_projects.get(source.get('session_id'))
                if namespace in {'ocr.tasks','classification.jobs'}:
                    app=namespace.split('.')[0]
                    matches=db.execute(select(s.module_project_links.c.workspace_id).where(s.module_project_links.c.app_id==app,s.module_project_links.c.module_project_id==source.get('project_id'))).scalars().all()
                    workspace=matches[0] if len(matches)==1 else None
                project=db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace)).mappings().first() if workspace else None
                workspace=project['workspace_id'] if project else None
                created=as_date(body.get('created_at') or body.get('created'))
                started=as_date(body.get('started_at') or body.get('started'))
                finished=as_date(body.get('finished_at') or body.get('finished'))
                status=normalized(body)
                if status not in {'succeeded','partial','failed','cancelled'}:status='needs_verification'
                tool=source.get('app_id') or body.get('kind') or namespace.split('.')[0]
                if tool=='batches':tool='batch'
                task_id=key('t');run_id=key('r');number=db.execute(select(s.task_numbers.next_value())).scalar_one()
                db.execute(insert(s.tasks).values(id=task_id,task_code=f'TK-LEGACY-{number:08d}',workspace_id=workspace,project_name_snapshot=project['name'] if project else None,
                    name=body.get('name') or f'{tool} · 历史任务 {identifier}',tool_id=tool,action=body.get('action') or body.get('kind') or 'legacy',source='legacy',
                    status=status,native_status=str(body.get('status') or body.get('state') or '')[:256],current_run_id=run_id,idempotency_key='legacy:'+digest([namespace,identifier,generation]),request_hash=digest([namespace,identifier,generation]),
                    created_at=created,summary={'legacy_source':namespace,'replayable':False,'verification_available':False,'legacy_missing_fields':['created_by','retry_history']+([] if created else ['created_at'])}))
                elapsed=max(0,int((finished-created).total_seconds()*1000)) if finished and created else None
                db.execute(insert(s.task_runs).values(id=run_id,task_id=task_id,attempt=1,generation=generation,status=status,queued_at=created,started_at=started,finished_at=finished,elapsed_ms=elapsed,
                    error=body.get('error'),request_snapshot={'legacy_source':namespace,'source_id':identifier},scope={'history_complete':False}))
                db.execute(insert(s.task_source_links).values(id=key('src'),task_id=task_id,run_id=run_id,namespace=namespace,source_id=identifier,source_generation=generation,metadata={'source_file':relative}))
                enrich(db,root,task_id,run_id,namespace,body,source)
                if namespace=='projection':
                    app=source['app_id'];native_namespace={'ocr':'ocr.tasks','classification':'classification.jobs','face':'manual.manual_analysis','audio':'manual.manual_analysis','seal':'manual.manual_analysis','catnet':'manual.manual_analysis','document_forensics':'manual.manual_analysis','aigc_image':'manual.manual_analysis'}.get(app)
                    if native_namespace:
                        for native_id in json.loads(source.get('native_ids') or '[]'):
                            result=db.execute(pg_insert(s.task_source_links).values(id=key('src'),task_id=task_id,run_id=run_id,namespace=native_namespace,source_id=native_id,source_generation=generation,metadata={}).on_conflict_do_nothing(index_elements=['namespace','source_id','source_generation']).returning(s.task_source_links.c.id)).first()
                            counts['aliases']+=int(result is not None)
                ledger._event(db,task_id,run_id,'legacy_imported',{'namespace':namespace,'source_id':identifier,'historical_creation_known':created is not None},identity='legacy:'+digest([namespace,identifier,generation]))
                counts['tasks']+=1
                if not workspace:counts['unknown_ownership']+=1
    return counts

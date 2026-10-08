"""Bound repeated native cold-load timeouts using durable task metadata.

Only actual adapter load outcomes count. Parent, precision and sibling batches
share the same root lineage. Another root, engine or frozen model identity and
recognition timeouts cannot change that lineage's consecutive streak.
"""
import json
import time
from types import SimpleNamespace

from ocr_workbench.store import encoded,now


class ScreenEngineUnavailable(RuntimeError):
    phase='engine_load_blocked'


def _scope(store,task_id):
    if not store.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screen_tasks'"):return None
    rows=store.rows('''SELECT s.batch_id,s.metadata,t.engine,t.project_id FROM screen_tasks s
        JOIN tasks t ON t.id=s.task_id WHERE s.task_id=?''',(task_id,))
    storage_table='screen_tasks'
    if not rows:
        from ocr_workbench.screen_resolution import _local_rows
        rows=_local_rows(store,'''SELECT s.batch_id,s.metadata,t.engine,t.project_id FROM screen_local_requests s
            JOIN tasks t ON t.id=s.task_id WHERE s.task_id=?''',(task_id,))
        storage_table='screen_local_requests'
    if not rows:return None
    row=rows[0];metadata=json.loads(row['metadata'])
    identity=metadata.get('model_identity')
    # Older unknown task identities do not justify disabling other models.
    if not isinstance(identity,str) or not identity:return None
    from ocr_workbench.screen_resolution import _lineage
    root,lineage=_lineage(SimpleNamespace(store=store),row['project_id'],row['batch_id'])
    return {'batch_id':row['batch_id'],'root_batch_id':root,'project_id':row['project_id'],
        'lineage_batch_ids':[batch['id'] for batch in lineage],
        'engine':row['engine'],'model_identity':identity,'storage_table':storage_table}


def cold_load_state(store,task_id,*,limit=3):
    scope=_scope(store,task_id)
    if not scope:return {'blocked':False,'consecutive_timeouts':0,'basis':'no_frozen_native_model_identity'}
    identifiers=scope['lineage_batch_ids'];parameters=','.join('?' for _ in identifiers)
    rows=store.rows('''SELECT s.task_id,s.metadata FROM screen_tasks s JOIN tasks t ON t.id=s.task_id
        WHERE s.batch_id IN ('''+parameters+''') AND t.engine=? AND t.project_id=?''',
        (*identifiers,scope['engine'],scope['project_id']))
    from ocr_workbench.screen_resolution import _local_rows
    rows+=_local_rows(store,'''SELECT s.task_id,s.metadata FROM screen_local_requests s JOIN tasks t ON t.id=s.task_id
        WHERE s.batch_id IN ('''+parameters+''') AND t.engine=? AND t.project_id=?''',
        (*identifiers,scope['engine'],scope['project_id']))
    events=[]
    for row in rows:
        metadata=json.loads(row['metadata'])
        if metadata.get('model_identity')!=scope['model_identity']:continue
        for event in metadata.get('engine_load_events',[]):
            if (isinstance(event,dict) and event.get('phase')=='engine_load' and
                    event.get('outcome') in {'ready','timeout'} and type(event.get('ordinal')) is int):
                events.append((event['ordinal'],row['task_id'],event))
    streak=0;last=None
    for _,_,event in sorted(events,key=lambda entry:(entry[0],entry[1])):
        streak=0 if event['outcome']=='ready' else streak+1
        last=event
    return {**scope,'blocked':streak>=limit,'consecutive_timeouts':streak,'limit':limit,
        'basis':'persisted_adapter_load_outcomes','last_load':last}


def record_cold_load(store,task_id,outcome,*,error=None):
    if outcome not in {'ready','timeout'}:raise ValueError('加载回执类型无效')
    scope=_scope(store,task_id)
    if not scope:return None
    if outcome=='timeout':
        from ocr_workbench.adapter import EngineLoadTimeout
        if not isinstance(error,EngineLoadTimeout) or getattr(error,'phase',None)!='engine_load':return None
    event={'phase':'engine_load','outcome':outcome,'ordinal':time.time_ns(),'at':now(),
        'basis':'adapter_ready_returned' if outcome=='ready' else 'EngineLoadTimeout',
        'timeout_seconds':getattr(error,'timeout_seconds',None),'last_stage':getattr(error,'last_stage',None)}
    with store.transaction() as db:
        table=scope['storage_table']
        row=db.execute('SELECT metadata FROM '+table+' WHERE task_id=?',(task_id,)).fetchone()
        metadata=json.loads(row['metadata'])
        events=metadata.get('engine_load_events',[])
        if not isinstance(events,list):raise ValueError('任务加载回执无效')
        metadata['engine_load_events']=[*events,event]
        db.execute('UPDATE '+table+' SET metadata=? WHERE task_id=?',(encoded(metadata),task_id))
    return event

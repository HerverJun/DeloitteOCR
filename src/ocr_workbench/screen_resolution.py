"""Bounded native issue handling through the registered precision batch route.

Existing screen_actions receipts are the only action ledger. A durable intent
precedes submission, so a crash cannot silently cause an unbound second batch.
No candidate value, selected result or human review is changed by this module.
"""

from copy import deepcopy
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import time
from types import SimpleNamespace

from ocr_workbench.store import Conflict,encoded

VERSION='screen-resolution-v1'
ACTION='precision_subbatch'
IDENTIFIER=re.compile(r'^[A-Za-z0-9_-]{1,120}$')


def _hash(value):return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def _validate(body):
    if not isinstance(body,dict) or set(body)-{'request_id','action','budget_seconds','targets'}:
        raise ValueError('异常处理参数无效')
    if body.get('action')!=ACTION:raise ValueError('只支持已登记的 precision_subbatch 精度动作')
    request=body.get('request_id')
    if not isinstance(request,str) or not 8<=len(request)<=120 or not IDENTIFIER.fullmatch(request):
        raise ValueError('异常处理缺少有效请求编号')
    budget=body.get('budget_seconds')
    if type(budget) is not int or not 10<=budget<=864000:raise ValueError('总预算必须为 10–864000 秒')
    targets=body.get('targets')
    if not isinstance(targets,list) or not 1<=len(targets)<=50:raise ValueError('每次处理须明确选择 1–50 张图片')
    versions=set()
    for target in targets:
        if not isinstance(target,dict) or set(target)!={'input_version_id','result_id','result_revision','issue_ids'}:
            raise ValueError('须绑定图片版本、当前结果及修订号和明确问题')
        for key in ('input_version_id','result_id'):
            if not isinstance(target[key],str) or not IDENTIFIER.fullmatch(target[key]):raise ValueError('结果身份无效')
        if type(target['result_revision']) is not int or target['result_revision']<0:raise ValueError('结果修订号无效')
        issues=target['issue_ids']
        if not isinstance(issues,list) or not 1<=len(issues)<=50 or any(not isinstance(item,str) or not IDENTIFIER.fullmatch(item) for item in issues) or len(set(issues))!=len(issues):
            raise ValueError('须选择不同的真实问题编号')
        if target['input_version_id'] in versions:raise ValueError('图片范围不能重复')
        versions.add(target['input_version_id'])
    return deepcopy(body)


def precision_capability(manager):
    """Read registered capability and required local assets; no load or probe."""
    registry=getattr(manager.queue,'registry',None)
    try:
        if registry:
            specification=registry.engines().get('glm')
            if not specification:return {'available':False,'reason':'glm_not_registered'}
            root=Path(registry.resolve('glm',specification.get('package_id','builtin')))
        else:
            root=Path(manager.bundle)
            path=root/'config/engines.json'
            if not path.is_file():return {'available':False,'reason':'engine_inventory_unavailable'}
            specification=json.loads(path.read_text('utf-8')).get('glm')
        if not specification or specification.get('capabilities',{}).get('tables') is not True:
            return {'available':False,'reason':'glm_table_capability_unavailable'}
        if not (root/'runtimes/glm/python.exe').is_file():return {'available':False,'reason':'glm_runtime_missing'}
        models=specification.get('models')
        if not isinstance(models,list) or not models:return {'available':False,'reason':'glm_model_assets_unverified'}
        for name in models:
            folder=root/'models'/str(name)
            if not folder.is_dir() or not any(file.is_file() and file.suffix in {'.safetensors','.gguf','.pdparams','.pdiparams'} for file in folder.rglob('*')):
                return {'available':False,'reason':'glm_model_assets_missing','model':str(name)}
        return {'available':True,'engine':'glm','package_id':specification.get('package_id','builtin'),
                'models':models,'basis':'registered_table_capability_and_local_asset_presence',
                'inference_verified_by_this_check':False}
    except (OSError,ValueError,KeyError,TypeError):
        return {'available':False,'reason':'engine_inventory_unavailable'}


def _lineage(manager,project_id,batch_id):
    rows={row['id']:row for row in manager.store.rows('SELECT * FROM screen_batches WHERE project_id=?',(project_id,))}
    current=batch_id;ancestors=set()
    while current:
        if current in ancestors or current not in rows:raise Conflict('批次来源链无效，不能重置预算')
        ancestors.add(current);parent=rows[current].get('parent_batch_id')
        if not parent:root=current;break
        current=parent
    children={root}
    while True:
        expanded=children|{identifier for identifier,row in rows.items() if row.get('parent_batch_id') in children}
        if expanded==children:break
        children=expanded
    return root,[rows[identifier] for identifier in sorted(children)]


def _costs(manager,batch_id,items=None):
    from ocr_workbench.store import now
    timestamp=now()
    rows=manager.store.rows('''SELECT input_version_id,SUM(seconds) seconds FROM (
        SELECT s.input_version_id,s.spent_seconds + CASE WHEN t.started IS NULL THEN 0 ELSE
            MAX(0,(julianday(COALESCE(t.finished,?))-julianday(t.started))*86400) END seconds
        FROM screen_tasks s JOIN tasks t ON t.id=s.task_id WHERE s.batch_id=?
        UNION ALL
        SELECT c.input_version_id,c.spent_seconds + CASE WHEN t.started IS NULL THEN 0 ELSE
            MAX(0,(julianday(COALESCE(t.finished,?))-julianday(t.started))*86400) END seconds
        FROM screen_contexts c JOIN tasks t ON t.id=c.task_id WHERE c.batch_id=?) GROUP BY input_version_id''',
        (timestamp,batch_id,timestamp,batch_id))
    costs={row['input_version_id']:max(0.,float(row['seconds'] or 0)) for row in rows}
    for row in _local_rows(manager.store, '''SELECT l.input_version_id,l.spent_seconds +
        CASE WHEN t.started IS NULL THEN 0 ELSE MAX(0,(julianday(COALESCE(t.finished,?))-
        julianday(t.started))*86400) END seconds FROM screen_local_requests l
        JOIN tasks t ON t.id=l.task_id WHERE l.batch_id=?''', (timestamp,batch_id)):
        costs[row['input_version_id']]=costs.get(row['input_version_id'],0.)+max(0.,float(row['seconds'] or 0))
    for item in items if items is not None else manager._items(batch_id):
        preparation=json.loads(item['preparation']) if item.get('preparation') else {}
        value=preparation.get('preparation_seconds',0)
        if type(value) in (int,float) and math.isfinite(value) and value>0:
            costs[item['input_version_id']]=costs.get(item['input_version_id'],0.)+value
    return costs


def _spent(manager,batch_id,version_id):return _costs(manager,batch_id).get(version_id,0.)


def _receipts(manager,lineage):
    receipts=[]
    for batch in lineage:
        for row in manager.store.rows('SELECT * FROM screen_actions WHERE batch_id=?',(batch['id'],)):
            receipt=json.loads(row['receipt'])
            if receipt.get('resolution_version')==VERSION:receipts.append(receipt)
    return receipts


def task_seconds_spent(store,task_id):
    from ocr_workbench.store import now
    rows=store.rows('''SELECT t.started,t.finished,COALESCE(s.spent_seconds,c.spent_seconds,0) carried,
        CASE WHEN t.started IS NULL THEN 0 ELSE MAX(0,(julianday(COALESCE(t.finished,?))-julianday(t.started))*86400) END active
        FROM tasks t LEFT JOIN screen_tasks s ON s.task_id=t.id
        LEFT JOIN screen_contexts c ON c.task_id=t.id WHERE t.id=?''',(now(),task_id))
    local=_local_rows(store,'SELECT spent_seconds FROM screen_local_requests WHERE task_id=?',(task_id,))
    carried=float(local[0]['spent_seconds'] or 0) if local else float(rows[0]['carried'] or 0) if rows else 0.
    return max(0.,carried+float(rows[0]['active'] or 0)) if rows else 0.


def _local_rows(store,query,parameters=()):
    if not store.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='screen_local_requests'"):
        return []
    return store.rows(query,parameters)


def _local_reservations(manager,batch,exclude_task_id=None):
    from ocr_workbench.store import now
    rows=_local_rows(manager.store,'''SELECT l.input_version_id,l.metadata,t.id,l.spent_seconds +
        CASE WHEN t.started IS NULL THEN 0 ELSE MAX(0,(julianday(COALESCE(t.finished,?))-
        julianday(t.started))*86400) END spent FROM screen_local_requests l JOIN tasks t ON t.id=l.task_id
        WHERE l.batch_id=? AND t.status IN ('queued','running','paused','interrupted')''',(now(),batch['id']))
    reserved={}
    for row in rows:
        if row['id']==exclude_task_id:continue
        allowance=json.loads(row['metadata']).get('budget_reservation_seconds')
        if type(allowance) not in (int,float) or not math.isfinite(allowance) or allowance<0:
            raise Conflict('局部候选任务预算预留无效')
        key=row['input_version_id'];reserved[key]=reserved.get(key,0.)+max(0.,allowance-float(row['spent'] or 0))
    return reserved


def _live_reservations(manager,batch,exclude_task_id=None):
    from ocr_workbench.store import now
    rows=manager.store.rows('''SELECT t.id,t.kind,s.metadata,c.raw,COALESCE(s.input_version_id,c.input_version_id) version_id,
        COALESCE(s.spent_seconds,c.spent_seconds,0) + CASE WHEN t.started IS NULL THEN 0 ELSE
            MAX(0,(julianday(COALESCE(t.finished,?))-julianday(t.started))*86400) END spent
        FROM tasks t
        LEFT JOIN screen_tasks s ON s.task_id=t.id LEFT JOIN screen_contexts c ON c.task_id=t.id
        WHERE (s.batch_id=? OR c.batch_id=?)
        AND t.status IN ('queued','running','paused','interrupted')''',
        (now(),batch['id'],batch['id']))
    policy=json.loads(batch['policy']) if isinstance(batch['policy'],str) else batch['policy']
    reserved={}
    for row in rows:
        if row['id']==exclude_task_id or row['kind'] in {'screen_cached','screen_context_cached'}:continue
        frozen=json.loads(row['metadata'] or row['raw'] or '{}')
        allowance=frozen.get('budget_reservation_seconds',frozen.get('timeout_seconds',policy['budget_seconds']))
        if type(allowance) not in (int,float) or not math.isfinite(allowance) or allowance<0:
            raise Conflict('任务预算预留无效，不能重置额度')
        reserved[row['version_id']]=reserved.get(row['version_id'],0.)+max(0.,allowance-float(row['spent'] or 0))
    for key,value in _local_reservations(manager,batch,exclude_task_id).items():
        reserved[key]=reserved.get(key,0.)+value
    return reserved


def _budget(manager,root,lineage,receipts,*,executing=None,exclude_task_id=None):
    root_policy=json.loads(next(batch['policy'] for batch in lineage if batch['id']==root))
    used=0.;actual=0.;reserved=0.;per_source={};per_source_actual={};batch_ids={batch['id'] for batch in lineage}
    submitted_requests={batch['request_id'] for batch in lineage}
    for batch in lineage:
        policy=json.loads(batch['policy']);items=manager._items(batch['id'])
        item_costs=_costs(manager,batch['id'],items)
        live=_live_reservations(manager,batch,exclude_task_id if executing and executing[0]==batch['id'] else None)
        local_live=_local_reservations(manager,batch,exclude_task_id if executing and executing[0]==batch['id'] else None)
        costs=[(item,item_costs.get(item['input_version_id'],0.)) for item in items]
        for item,cost in costs:
            pending=item['status'] not in {'completed','needs_review','failed','cancelled'}
            current=executing==(batch['id'],item['input_version_id'])
            reserve=(live.get(item['input_version_id'],0.)
                     if batch['id']==root or current else max(0.,policy['budget_seconds']-cost)) if pending else local_live.get(item['input_version_id'],0.)
            amount=cost+reserve;used+=amount;actual+=cost;reserved+=reserve
            per_source[item['source_hash']]=per_source.get(item['source_hash'],0.)+amount
            per_source_actual[item['source_hash']]=per_source_actual.get(item['source_hash'],0.)+cost
    # Intents reserve funds across a submit crash and before a child is visible.
    for receipt in receipts:
        if (receipt.get('status')=='intent' and receipt.get('batch_id') not in batch_ids
                and receipt.get('submit_body',{}).get('request_id') not in submitted_requests):
            used+=receipt['budget']['reserved_seconds'];reserved+=receipt['budget']['reserved_seconds']
            for target in receipt.get('bindings',[]):
                key=target['source_sha256'];per_source[key]=per_source.get(key,0.)+receipt['policy']['budget_seconds']
    return {'root_batch_id':root,'limit_seconds':root_policy['batch_budget_seconds'],'consumed_or_reserved_seconds':used,
            'actual_seconds':actual,'inflight_reserved_seconds':reserved,
            'remaining_seconds':max(0.,root_policy['batch_budget_seconds']-used),
            'per_image_limit_seconds':root_policy['budget_seconds'],'per_source_consumed_or_reserved':per_source,
            'per_source_actual_seconds':per_source_actual}


def execution_budget(manager,project_id,batch_id,version_id,*,task_id=None):
    """Release only this input's future envelope; retain siblings' reservations.

    Costs are summed model/queue elapsed seconds, separately from each batch's
    wall-clock deadline. Pauses never forgive accumulated execution costs.
    """
    root,lineage=_lineage(manager,project_id,batch_id)
    batch=next(row for row in lineage if row['id']==batch_id)
    policy=json.loads(batch['policy'])
    items=manager._items(batch_id)
    item=next((row for row in items if row['input_version_id']==version_id),None)
    if not item:raise ValueError('预算图片不属于当前批次')
    budget=_budget(manager,root,lineage,_receipts(manager,lineage),executing=(batch_id,version_id),exclude_task_id=task_id)
    costs=_costs(manager,batch_id,items)
    live=_live_reservations(manager,batch,task_id)
    local_live=_local_reservations(manager,batch,task_id)
    local=costs.get(version_id,0.)
    source=item['source_hash']
    sibling_reserved=sum((live.get(other['input_version_id'],0.) if batch_id==root else
        max(0.,policy['budget_seconds']-costs.get(other['input_version_id'],0.)))
        if other['status'] not in {'completed','needs_review','failed','cancelled'} else local_live.get(other['input_version_id'],0.)
        for other in items if other['input_version_id']!=version_id)
    same_input_reserved=live.get(version_id,0.)
    local_batch_spent=sum(costs.get(other['input_version_id'],0.) for other in items)
    wall=max(0.,batch['deadline']-time.time()) if batch.get('deadline') else float('inf')
    remaining=min(policy['budget_seconds']-local-same_input_reserved,
        policy['batch_budget_seconds']-local_batch_spent-sibling_reserved-same_input_reserved,
        budget['remaining_seconds'],budget['per_image_limit_seconds']-budget['per_source_consumed_or_reserved'].get(source,0),wall)
    potential=min(policy['budget_seconds']-local,policy['batch_budget_seconds']-local_batch_spent,
        budget['limit_seconds']-budget['actual_seconds'],
        budget['per_image_limit_seconds']-budget['per_source_actual_seconds'].get(source,0),wall)
    if task_id:
        task=manager.store.rows('''SELECT s.metadata,c.raw FROM tasks t LEFT JOIN screen_tasks s ON s.task_id=t.id
            LEFT JOIN screen_contexts c ON c.task_id=t.id WHERE t.id=?''',(task_id,))[0]
        frozen=json.loads(task['metadata'] or task['raw'] or '{}')
        local_task=_local_rows(manager.store,'SELECT metadata FROM screen_local_requests WHERE task_id=?',(task_id,))
        if local_task:frozen=json.loads(local_task[0]['metadata'])
        allowance=frozen.get('budget_reservation_seconds',frozen.get('timeout_seconds',policy['budget_seconds']))
        remaining=min(remaining,allowance-task_seconds_spent(manager.store,task_id))
    return {**budget,'remaining_seconds':max(0.,remaining),'potential_remaining_seconds':max(0.,potential),
            'wall_deadline_remaining_seconds':None if not math.isfinite(wall) else wall,
            'local_input_actual_seconds':local,'basis':'source_lineage_actual_cost_and_sibling_reservations'}


def remaining_screen_budget(store,batch_id,version_id,*,task_id=None):
    """Queue entry without constructing a manager, starting a service or model."""
    batch=store.rows('SELECT project_id FROM screen_batches WHERE id=?',(batch_id,))[0]
    manager=SimpleNamespace(store=store,_items=lambda identifier:store.rows(
        'SELECT * FROM screen_items WHERE batch_id=? ORDER BY position',(identifier,)))
    return execution_budget(manager,batch['project_id'],batch_id,version_id,task_id=task_id)


def _existing_child(manager,project_id,receipt):
    child=receipt.get('batch_id')
    if not child and receipt.get('submit_body'):
        found=manager.lookup(project_id,receipt['submit_body']['request_id'])
        child=found['batch_id'] if found.get('submitted') else None
    if child and receipt.get('submit_body'):
        actual=manager._batch(project_id,child)
        versions=[item['input_version_id'] for item in manager._items(child)]
        if actual.get('parent_batch_id')!=receipt['parent_batch_id'] or actual['policy']!=receipt['policy'] or versions!=receipt['submit_body']['version_ids']:
            raise Conflict('已存在精度子批次与冻结的范围或策略不一致')
    return child


def _persist(manager,batch_id,body,receipt,*,update=False):
    with manager.store.transaction() as db:
        if update:
            db.execute('UPDATE screen_actions SET receipt=? WHERE batch_id=? AND request_id=?',(encoded(receipt),batch_id,body['request_id']))
        else:
            db.execute('INSERT INTO screen_actions VALUES(?,?,?,?)',(batch_id,body['request_id'],_hash(body),encoded(receipt)))


def resolve_screen_issues(manager,project_id,batch_id,body,*,allow_new=True):
    """Submit one bound GLM candidate attempt, or return an existing/review reason."""
    body=_validate(body)
    with manager.guard,manager.store.file_lock:
        batch=manager._batch(project_id,batch_id)
        previous=manager.store.rows('SELECT * FROM screen_actions WHERE batch_id=? AND request_id=?',(batch_id,body['request_id']))
        old=json.loads(previous[0]['receipt']) if previous else None
        if previous and previous[0]['payload_hash']!=_hash(body):raise Conflict('操作编号已绑定不同参数')
        if old:
            child=_existing_child(manager,project_id,old)
            if child:
                return {**old,'status':'reused' if old.get('status')=='intent' else old['status'],'batch_id':child,
                        'attempt':manager.get(project_id,child),'replayed':True}
            if old.get('status')!='intent' or not allow_new:return {**old,'replayed':True}
        elif not allow_new:
            return None
        root,lineage=_lineage(manager,project_id,batch_id)
        ledger=_receipts(manager,lineage)
        bindings=[];existing_candidates=[]
        for target in body['targets']:
            rows=manager.store.rows('SELECT * FROM screen_items WHERE batch_id=? AND input_version_id=?',(batch_id,target['input_version_id']))
            if not rows:raise ValueError('问题图片不属于当前批次')
            item=rows[0]
            if item['status'] not in {'completed','needs_review','failed','cancelled'}:raise Conflict('请等待当前图片尝试结束')
            if item['result_id']!=target['result_id']:raise Conflict('当前候选结果已变化')
            result=manager.store.result(target['result_id'])
            if result['revision']!=target['result_revision']:raise Conflict('结果修订号已变化')
            version=manager.store.one('versions',target['input_version_id'])
            image=manager.store.one('images',item['image_id'])
            if image['active_version']!=target['input_version_id']:raise Conflict('当前图片版本已变化')
            selected=manager.store.rows('SELECT result_id FROM selections WHERE image_id=?',(item['image_id'],))
            if selected and selected[0]['result_id']!=target['result_id']:raise Conflict('当前选定结果已变化，请重新读取问题')
            source_hash=hashlib.sha256(manager.store.file(version['path']).read_bytes()).hexdigest()
            if source_hash!=item['source_hash']:raise Conflict('原图像素已变化')
            detail=manager.detail(project_id,batch_id,target['input_version_id'])
            diagnostics=detail.get('diagnostics',{})
            issues={issue['id']:issue for issue in diagnostics.get('items',[])}
            if not set(target['issue_ids'])<=set(issues):raise Conflict('所选问题已变化，请重新读取诊断')
            picked=[issues[identifier] for identifier in target['issue_ids']]
            if any(issue['code'] in {'unreadable_source','image_edge_cutoff','image_low_detail'} for issue in issues.values()):
                existing_candidates.append({'input_version_id':target['input_version_id'],'reason':'source_incomplete_or_unreadable','candidate_ids':[]})
            glm=[candidate['id'] for candidate in detail.get('candidate_results',[]) if candidate.get('engine')=='glm']
            if glm:existing_candidates.append({'input_version_id':target['input_version_id'],'reason':'existing_glm_candidate_requires_comparison','candidate_ids':glm})
            bindings.append({**target,'source_sha256':source_hash,'table_snapshot_sha256':diagnostics['binding']['table_snapshot_sha256'],
                'problem_signature':_hash(sorted([{'code':issue['code'],'location':issue['location']} for issue in picked],key=lambda value:encoded(value)))})
        policy=deepcopy(batch['policy'])
        policy.update(primary_engine='glm',fallback_engines=[],max_upgrades=0,mode='accuracy',upgrade_trigger='failure_only',
                      allow_text_fixes=False,visual_review=False,require_human_review=True,strategy_version=VERSION+'-glm-candidate')
        # Keep the registered fixed GLM meaning and historical intent hashes.
        if 'engine_route' in policy or 'technical_fallback_reserve_seconds' in policy:
            policy.update(engine_route='fixed',technical_fallback_reserve_seconds=0)
        strategy=_hash({'model_identity':manager.model_identity,'policy':{key:policy[key] for key in policy if key not in {'budget_seconds','batch_budget_seconds','request_timeout_seconds','group_size'}}})
        attempt_key=_hash({'strategy':strategy,'bindings':sorted([{key:binding[key] for key in
            ('source_sha256','table_snapshot_sha256','problem_signature')} for binding in bindings],key=lambda value:encoded(value))})
        # This registered precision action reads the complete same image. A
        # different issue order or a partly overlapping image group cannot
        # justify another identical model attempt without new source evidence.
        pixel_keys=[_hash([strategy,binding['source_sha256']]) for binding in bindings]
        base={'resolution_version':VERSION,'request_id':body['request_id'],'action':ACTION,'parent_batch_id':batch_id,
              'requested_budget_seconds':body['budget_seconds'],
              'root_batch_id':root,'batch_id':None,'bindings':bindings,'attempt_key':attempt_key,'pixel_attempt_keys':pixel_keys,
              'human_review':'not_performed','needs_review':True,'values_modified':False,'selection_applied':False}
        repeat=next((receipt for receipt in ledger if (receipt.get('attempt_key')==attempt_key or set(receipt.get('pixel_attempt_keys',[]))&set(pixel_keys)) and receipt.get('status') in {'intent','submitted','reused'}
                     and receipt.get('request_id')!=body['request_id']),None)
        if repeat:
            child=_existing_child(manager,project_id,repeat)
            covered=set(repeat.get('pixel_attempt_keys',[]))&set(pixel_keys)
            complete=set(pixel_keys)<=covered
            receipt={**base,'status':'reused' if child and complete else 'blocked','reason':'same_source_strategy_problem_content_already_attempted',
                     'batch_id':child,'existing_attempt':{'request_id':repeat['request_id'],'batch_id':child},'budget':repeat['budget']}
            receipt['pixel_attempt_keys']=sorted(covered)
            receipt['unprocessed_version_ids']=[binding['input_version_id'] for binding,key in zip(bindings,pixel_keys) if key not in covered]
            _persist(manager,batch_id,body,receipt);return receipt
        if existing_candidates:
            receipt={**base,'status':'blocked','reason':'existing_candidate_or_source_review_required','existing_candidates':existing_candidates}
            if old:_persist(manager,batch_id,body,receipt,update=True)
            else:_persist(manager,batch_id,body,receipt)
            return receipt
        primary_pending=[item['input_version_id'] for item in manager._items(root)
                         if item['status'] not in {'completed','needs_review','failed','cancelled'}]
        if primary_pending:
            if old:
                # A frozen pre-submit intent keeps its reservation and remains
                # resumable after the temporarily active main batch settles.
                return {**old,'reason':'primary_batch_unsettled','replayed':True,
                    'message':'主批次仍有未完成图片，已冻结的精度意图继续保留；等待主任务结束后恢复。',
                    'pending_version_ids':primary_pending}
            receipt={**base,'status':'blocked','reason':'primary_batch_unsettled',
                'message':'主批次仍有未完成图片，请先等待主任务结束；精度处理不会抢占主任务预算。',
                'pending_version_ids':primary_pending}
            _persist(manager,batch_id,body,receipt,update=bool(old));return receipt
        capability=precision_capability(manager)
        if capability.get('available') is not True:
            receipt={**base,'status':'blocked','reason':capability['reason'],'capability':capability}
            _persist(manager,batch_id,body,receipt,update=bool(old));return receipt
        # Exclude this same intent's reservation while resuming its submission.
        budget=_budget(manager,root,lineage,[receipt for receipt in ledger if receipt.get('request_id')!=body['request_id'] or receipt.get('parent_batch_id')!=batch_id])
        multiplicity=Counter(binding['source_sha256'] for binding in bindings)
        per_image=min(180,body['budget_seconds']//len(bindings),int(budget['remaining_seconds']//len(bindings)),
            *(int(max(0,budget['per_image_limit_seconds']-budget['per_source_consumed_or_reserved'].get(binding['source_sha256'],0))/multiplicity[binding['source_sha256']]) for binding in bindings))
        if per_image<10:
            receipt={**base,'status':'blocked','reason':'lineage_budget_exhausted','budget':budget}
            _persist(manager,batch_id,body,receipt,update=bool(old));return receipt
        reserved=per_image*len(bindings)
        policy.update(budget_seconds=per_image,batch_budget_seconds=reserved,
                      request_timeout_seconds=min(per_image,policy['request_timeout_seconds']))
        submit={'version_ids':[target['input_version_id'] for target in body['targets']],
                'request_id':'resolve-'+_hash([batch_id,body['request_id']])[:64],'policy':policy}
        receipt={**base,'status':'intent','reason':'frozen_submission_not_yet_observed','capability':capability,
                 'budget':{**budget,'reserved_seconds':reserved,'per_image_reserved_seconds':per_image},
                 'policy':policy,'submit_body':submit}
        if old:
            # A restart may have less remaining budget; never expand the frozen
            # intent. Revalidate current bindings before attempting its submit.
            if old['bindings']!=bindings:raise Conflict('待恢复操作的内容绑定已变化')
            if old['budget']['reserved_seconds']>reserved:raise Conflict('待恢复操作已无足够来源链预算')
            receipt=old;submit=old['submit_body']
        else:_persist(manager,batch_id,body,receipt)
        attempt=manager.submit(project_id,submit,parent_batch_id=batch_id)
        receipt={**receipt,'status':'submitted','reason':'precision_candidate_requires_human_review',
                 'batch_id':attempt['batch_id'],'attempt':attempt}
        _persist(manager,batch_id,body,receipt,update=True)
        return receipt

"""Transactional authority for task identities, attempts, events and dispatch."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import base64
import hashlib
import json
import secrets
import uuid
from sqlalchemy import select, insert, update, delete, and_, or_, func, tuple_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from . import schema_v1 as s

TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled'}
STATES = TERMINAL | {'queued','waiting_resource','running','waiting_user','pausing','paused','cancelling','needs_verification'}
hasher = PasswordHasher()
_dummy_password = hasher.hash(secrets.token_urlsafe(24))

def now(): return datetime.now(timezone.utc)
def key(prefix): return prefix + uuid.uuid4().hex
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
def token_hash(value): return hashlib.sha256(value.encode()).hexdigest()
def wire(value):
    if isinstance(value, datetime): return value.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {k: wire(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)): return [wire(v) for v in value]
    return value
def safe(value):
    """Never persist credentials, authentication headers or raw provider bodies."""
    if isinstance(value, dict):
        return {k: '[redacted]' if k.casefold() in {'token','raw_response','response_body','request_body'} or any(word in k.lower() for word in ('password','secret','api_key','apikey','authorization','credential','access_token','bearer_token')) else safe(v)
                for k,v in value.items()}
    if isinstance(value, list): return [safe(v) for v in value]
    return value

class LedgerError(ValueError):
    def __init__(self, message, status=400): super().__init__(message); self.status = status

class Ledger:
    def __init__(self, engine): self.engine = engine

    def audit(self, db, actor, action, workspace=None, resource=None, details=None):
        db.execute(insert(s.audit_events).values(actor_id=actor['id'] if actor else None,
            action=action, workspace_id=workspace, resource_id=resource, details=safe(details or {})))

    def create_user(self, username, display_name, password, *, admin=False, actor=None):
        username = username.strip().casefold()
        if not username or len(username)>100 or not display_name.strip() or len(password)<12:
            raise LedgerError('账号、显示名称有效，且密码须至少 12 位')
        if actor is not None and not actor['is_admin']: raise LedgerError('需要平台管理员权限',403)
        record = dict(id=key('u'), username=username, display_name=display_name.strip(), password_hash=hasher.hash(password), is_admin=admin)
        with self.engine.begin() as db:
            db.execute(insert(s.users).values(**record))
            self.audit(db, actor, 'user.create', resource=record['id'])
        return {k:v for k,v in record.items() if k!='password_hash'}

    def login(self, username, password):
        with self.engine.begin() as db:
            user = db.execute(select(s.users).where(s.users.c.username==username.strip().casefold())).mappings().first()
            try: valid = hasher.verify(user['password_hash'] if user else _dummy_password, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError): valid=False
            if not valid or not user or user['disabled']: raise LedgerError('账号或密码错误',401)
            token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            db.execute(insert(s.user_sessions).values(id=key('ss'),token_hash=token_hash(token),csrf_hash=token_hash(csrf),user_id=user['id'],expires_at=now()+timedelta(hours=12)))
            self.audit(db,user,'session.login')
        return self.public_user(user), token, csrf

    @staticmethod
    def public_user(user): return {name:user[name] for name in ('id','username','display_name','is_admin','disabled') if name in user}

    def authenticate(self, token):
        if not token: raise LedgerError('请先登录',401)
        with self.engine.connect() as db:
            row = db.execute(select(s.users, s.user_sessions.c.csrf_hash).join(s.user_sessions,s.users.c.id==s.user_sessions.c.user_id).where(
                s.user_sessions.c.token_hash==token_hash(token),s.user_sessions.c.expires_at>now(),s.users.c.disabled.is_(False))).mappings().first()
        if not row: raise LedgerError('登录已过期，请重新登录',401)
        return dict(row)

    def logout(self, token):
        with self.engine.begin() as db: db.execute(delete(s.user_sessions).where(s.user_sessions.c.token_hash==token_hash(token)))

    def role(self, db, actor, workspace, *, write=False, owner=False):
        if actor['is_admin']: return 'owner'
        role=db.execute(select(s.workspace_members.c.role).join(s.workspaces,s.workspace_members.c.workspace_id==s.workspaces.c.workspace_id).where(
            s.workspace_members.c.workspace_id==workspace,s.workspace_members.c.user_id==actor['id'],s.workspaces.c.archived.is_(False))).scalar_one_or_none()
        if not role or owner and role!='owner' or write and role=='viewer': raise LedgerError('没有此项目的访问权限',403)
        return role

    def workspace(self, actor, workspace, *, write=False):
        with self.engine.connect() as db:
            self.role(db,actor,workspace,write=write)
            row=db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace)).mappings().first()
            if not row: raise LedgerError('项目不存在',404)
            return wire(dict(row))

    def authorize_native_control(self,actor,workspace,identifiers):
        with self.engine.connect() as db:
            role=self.role(db,actor,workspace,write=True)
            if role=='owner':return
            if not identifiers:raise LedgerError('批量队列控制需要项目管理员权限',403)
            for identifier in identifiers:
                row=db.execute(select(s.tasks.c.created_by).join(s.task_source_links,s.tasks.c.id==s.task_source_links.c.task_id).where(
                    s.task_source_links.c.source_id==identifier,s.tasks.c.workspace_id==workspace).order_by(s.task_source_links.c.source_generation.desc()).limit(1)).first()
                if not row or row[0]!=actor['id']:raise LedgerError('仅提交人或项目管理员可控制该原生任务',403)

    def create_workspace(self, actor, name, workspace_id=None,request_key=None):
        if not name.strip() or len(name)>200: raise LedgerError('请输入有效项目名称')
        workspace_id=workspace_id or ('w'+uuid.uuid5(uuid.NAMESPACE_URL,actor['id']+':workspace:'+request_key).hex if request_key else key('w'))
        with self.engine.begin() as db:
            db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:identity,0))'),{'identity':workspace_id})
            previous=db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace_id)).mappings().first()
            if previous:
                self.role(db,actor,workspace_id)
                if previous['name']!=name.strip():raise LedgerError('项目请求键已绑定其他名称',409)
                return wire(dict(previous))
            db.execute(insert(s.workspaces).values(workspace_id=workspace_id,name=name.strip()))
            db.execute(insert(s.workspace_members).values(workspace_id=workspace_id,user_id=actor['id'],role='owner'))
            self.audit(db,actor,'workspace.create',workspace_id,workspace_id)
        return self.workspace(actor,workspace_id)

    def list_workspaces(self, actor):
        query=select(s.workspaces).where(s.workspaces.c.archived.is_(False)).order_by(s.workspaces.c.created_at.desc())
        if not actor['is_admin']: query=query.where(s.workspaces.c.workspace_id.in_(select(s.workspace_members.c.workspace_id).where(s.workspace_members.c.user_id==actor['id'])))
        with self.engine.connect() as db: return wire([dict(r) for r in db.execute(query).mappings()])

    def set_member(self, actor, workspace, user, role):
        if role not in {'owner','editor','viewer'}: raise LedgerError('项目角色无效')
        with self.engine.begin() as db:
            self.role(db,actor,workspace,owner=True)
            db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace).with_for_update())
            if role!='owner':
                owners=db.execute(select(s.workspace_members.c.user_id).where(s.workspace_members.c.workspace_id==workspace,s.workspace_members.c.role=='owner')).scalars().all()
                if user in owners and len(owners)==1:raise LedgerError('请先指定另一位项目管理员',409)
            db.execute(pg_insert(s.workspace_members).values(workspace_id=workspace,user_id=user,role=role).on_conflict_do_update(
                index_elements=['workspace_id','user_id'],set_={'role':role}))
            self.audit(db,actor,'workspace.member',workspace,user,{'role':role})

    def remove_member(self,actor,workspace,user):
        from sqlalchemy import delete
        with self.engine.begin() as db:
            self.role(db,actor,workspace,owner=True)
            db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==workspace).with_for_update())
            owners=db.execute(select(s.workspace_members.c.user_id).where(s.workspace_members.c.workspace_id==workspace,s.workspace_members.c.role=='owner')).scalars().all()
            if user in owners and len(owners)==1:raise LedgerError('请先指定另一位项目管理员',409)
            db.execute(delete(s.workspace_members).where(s.workspace_members.c.workspace_id==workspace,s.workspace_members.c.user_id==user))
            self.audit(db,actor,'workspace.member.revoke',workspace,user)

    def existing_submission(self,actor,value):
        from .private_payloads import request_fingerprint
        with self.engine.connect() as db:
            self.role(db,actor,value['workspace_id'],write=True)
            row=db.execute(select(s.tasks).where(s.tasks.c.created_by==actor['id'],s.tasks.c.idempotency_key==value['idempotency_key'])).mappings().first()
            if row:
                if row['request_hash']!=request_fingerprint(value):raise LedgerError('请求键已绑定另一组参数',409)
                return self._summary(db,dict(row),actor)
        return None

    def _task(self, db, actor, task_id, *, control=False, lock=False):
        query=select(s.tasks).where(s.tasks.c.id==task_id)
        if lock: query=query.with_for_update()
        row=db.execute(query).mappings().first()
        if not row: raise LedgerError('任务不存在',404)
        role=self.role(db,actor,row['workspace_id'],write=control)
        if control and role!='owner' and row['created_by']!=actor['id']: raise LedgerError('仅提交人或项目管理员可操作此任务',403)
        return dict(row)

    def _event(self, db, task, run, kind, payload=None, actor=None, identity=None):
        db.execute(pg_insert(s.task_events).values(task_id=task,run_id=run,kind=kind,payload=safe(payload or {}),
            actor_id=actor['id'] if actor else None,identity=identity or key('e')).on_conflict_do_nothing(index_elements=['identity']))

    def steer(self,actor,task_id,request_key,path,payload):
        fingerprint=digest({'task_id':task_id,'path':path,'payload':payload})
        with self.engine.begin() as db:
            task=self._task(db,actor,task_id,control=True,lock=True)
            previous=db.execute(select(s.control_requests).where(s.control_requests.c.actor_id==actor['id'],s.control_requests.c.request_key==request_key)).mappings().first()
            if previous:
                if previous['request_hash']!=fingerprint:raise LedgerError('消息请求键冲突',409)
                return previous['receipt']
            run=db.execute(select(s.task_runs).where(s.task_runs.c.id==task['current_run_id'])).mappings().one()
            identifier=key('d')
            db.execute(insert(s.task_dispatches).values(id=identifier,task_id=task_id,run_id=run['id'],kind='control',state='queued',generation=run['generation'],command={'action':'steer','path':path,'payload':safe(payload),'actor_id':actor['id']}))
            receipt={'task_id':task_id,'run_id':run['id'],'dispatch_id':identifier}
            db.execute(insert(s.control_requests).values(id=key('c'),task_id=task_id,actor_id=actor['id'],request_key=request_key,request_hash=fingerprint,receipt=receipt))
            self._event(db,task_id,run['id'],'steering_requested',payload,actor)
            self.audit(db,actor,'task.steer',task['workspace_id'],task_id)
            return receipt

    def submit(self, actor, value, command, inputs=(), *, parent=None,request_identity=None):
        from .private_payloads import request_fingerprint
        from zoneinfo import ZoneInfo
        from .registry import task_label
        request_hash=request_fingerprint(request_identity or value); task_id=key('t'); run_id=key('r'); stamp=now()
        with self.engine.begin() as db:
            self.role(db,actor,value['workspace_id'],write=True)
            # Serialize duplicate submissions without aborting the transaction on a unique conflict.
            db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:identity,0))'),{'identity':actor['id']+':'+value['idempotency_key']})
            previous=db.execute(select(s.tasks).where(s.tasks.c.created_by==actor['id'],s.tasks.c.idempotency_key==value['idempotency_key'])).mappings().first()
            if previous:
                if previous['request_hash']!=request_hash: raise LedgerError('请求键已绑定另一组参数',409)
                return self._summary(db,dict(previous),actor)
            project=db.execute(select(s.workspaces).where(s.workspaces.c.workspace_id==value['workspace_id'])).mappings().one()
            if project['archived']: raise LedgerError('项目已归档',409)
            number=db.execute(select(s.task_numbers.next_value())).scalar_one()
            unique={ (row.get('asset_id'),row.get('asset_revision')):row for row in inputs }
            size=sum(row['size_bytes'] for row in unique.values()) if unique and all(row.get('size_bytes') is not None for row in unique.values()) else None
            local_stamp=stamp.astimezone(ZoneInfo('Asia/Hong_Kong'))
            row=dict(id=task_id,task_code=f'TK-{local_stamp:%Y%m%d}-{number:08d}',workspace_id=value['workspace_id'],
                project_name_snapshot=project['name'],created_by=actor['id'],creator_name_snapshot=actor['display_name'],
                name=value.get('name') or task_label(value['tool_id'],value['action']),description=value.get('description',''),
                tool_id=value['tool_id'],action=value['action'],source=value.get('source','manual'),
                status='queued',current_run_id=run_id,idempotency_key=value['idempotency_key'],request_hash=request_hash,
                input_total_bytes=size,file_count=len(unique) if unique else None,options=safe(value.get('options',{})))
            if parent: row.update(parent_task_id=parent['task_id'],parent_run_id=parent['run_id'])
            db.execute(insert(s.tasks).values(**row))
            for item in unique.values():
                db.execute(insert(s.task_inputs).values(id=key('i'),task_id=task_id,**{k:item.get(k) for k in ('asset_id','asset_revision','blob_hash','name','size_bytes')},source_locator=item.get('source_locator',{})))
            snapshot=safe(value)
            snapshot['requested_by_snapshot']={name:actor[name] for name in ('id','username','display_name')}
            if command.get('resolved_ai_service'):snapshot={**snapshot,'resolved_ai_service':command['resolved_ai_service']}
            db.execute(insert(s.task_runs).values(id=run_id,task_id=task_id,attempt=1,status='queued',generation=1,requested_by=actor['id'],request_snapshot=snapshot))
            db.execute(insert(s.task_dispatches).values(id=key('d'),task_id=task_id,run_id=run_id,kind='execute',state='queued',generation=1,command=safe(command)))
            self._event(db,task_id,run_id,'submitted',{'status':'queued'},actor)
            self.audit(db,actor,'task.submit',value['workspace_id'],task_id)
            row=db.execute(select(s.tasks).where(s.tasks.c.id==task_id)).mappings().one()
            return self._summary(db,dict(row),actor)

    def _summary(self, db, row, actor):
        run=db.execute(select(s.task_runs).where(s.task_runs.c.id==row['current_run_id'])).mappings().first()
        return self.summarize(row,run,actor)

    def summarize(self, row, run, actor, role=None):
        # Capabilities are hints only; actions recheck identity, role and revision under a lock.
        can=actor['is_admin'] or role=='owner' or actor['id']==row['created_by'] and role!='viewer'
        actions=['view']
        if can:
            if (row['status']=='needs_verification' and row.get('summary',{}).get('verification_available',True)) or row.get('summary',{}).get('control_verification_required'):actions.append('verify')
            if row['status'] in {'failed','partial','cancelled','needs_verification'} and row.get('summary',{}).get('replayable',True) and not row.get('summary',{}).get('retry_blocked',False): actions.append('retry')
            elif row['status']=='queued' or row['status'] not in TERMINAL|{'cancelling','needs_verification'} and 'cancel' in row.get('summary',{}).get('native_capabilities',[]): actions.append('cancel')
            if 'pause' in row.get('summary',{}).get('native_capabilities',[]) and row['status'] in {'running','waiting_user'}: actions.append('pause')
            if row['status']=='paused' or row['status']=='waiting_user' and 'resume' in row.get('summary',{}).get('native_capabilities',[]): actions.append('resume')
            actions.append('edit')
        if actor['is_admin'] or role=='owner': actions.append('unarchive' if row['archived'] else 'archive')
        elapsed=run['elapsed_ms'] if run else None
        if run and elapsed is None and run['status'] not in TERMINAL and run['queued_at']:
            elapsed=max(0,int((now()-run['queued_at']).total_seconds()*1000))
        return wire({k:row.get(k) for k in ('id','task_code','workspace_id','project_name_snapshot','created_by','creator_name_snapshot','name','description',
            'tool_id','tool_version','action','source','parent_task_id','parent_run_id','status','stage','availability','native_status','current_run_id','run_count','revision',
            'input_total_bytes','file_count','page_count','progress_done','progress_total','created_at','updated_at','last_verified_at','archived','summary')}
            |{'elapsed_ms':elapsed,'capabilities':actions})

    def list(self, actor, filters, limit=50, cursor=None):
        if not 1<=limit<=100: raise LedgerError('分页大小无效')
        summary_columns=[column for column in s.tasks.c if column.name not in {'options','request_hash','idempotency_key'}]
        query=select(*summary_columns,s.task_runs.c.queued_at,s.task_runs.c.elapsed_ms,s.task_runs.c.status.label('run_status'),s.workspace_members.c.role.label('member_role')).outerjoin(
            s.task_runs,s.tasks.c.current_run_id==s.task_runs.c.id).outerjoin(s.workspace_members,and_(s.tasks.c.workspace_id==s.workspace_members.c.workspace_id,s.workspace_members.c.user_id==actor['id']))
        if not actor['is_admin']: query=query.where(s.workspace_members.c.user_id==actor['id'])
        query=query.where(s.tasks.c.parent_task_id.is_(None),s.tasks.c.archived==bool(filters.get('archived',False)))
        for name in ('workspace_id','created_by','tool_id','source','status'):
            if filters.get(name): query=query.where(s.tasks.c[name]==filters[name])
        if filters.get('ai_service'):
            service=filters['ai_service']
            matched=select(s.ai_calls.c.task_id.label('id')).where(or_(s.ai_calls.c.profile_id==service,s.ai_calls.c.provider==service)).distinct().cte(recursive=True)
            matched=matched.union(select(s.tasks.c.parent_task_id).join(matched,s.tasks.c.id==matched.c.id).where(s.tasks.c.parent_task_id.is_not(None)))
            query=query.where(s.tasks.c.id.in_(select(matched.c.id)))
        if filters.get('keyword'):
            value='%'+filters['keyword'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            query=query.where(or_(s.tasks.c.name.ilike(value,escape='\\'),s.tasks.c.task_code.ilike(value,escape='\\')))
        for field,op in [('created_from','ge'),('created_to','lt')]:
            if filters.get(field):
                try: date=datetime.fromisoformat(filters[field].replace('Z','+00:00'))
                except ValueError: raise LedgerError('时间筛选无效')
                if date.tzinfo is None: raise LedgerError('时间须包含时区')
                query=query.where(s.tasks.c.created_at>=date if op=='ge' else s.tasks.c.created_at<date)
        fingerprint=digest(filters)
        if cursor:
            try:
                saved=json.loads(base64.urlsafe_b64decode(cursor+'='*((-len(cursor))%4)))
                if saved['filter']!=fingerprint: raise ValueError()
                if saved['created_at'] is None:query=query.where(s.tasks.c.created_at.is_(None),s.tasks.c.id<saved['id'])
                else:
                    date=datetime.fromisoformat(saved['created_at'].replace('Z','+00:00'))
                    query=query.where(or_(tuple_(s.tasks.c.created_at,s.tasks.c.id)<tuple_(date,saved['id']),s.tasks.c.created_at.is_(None)))
            except (ValueError,KeyError,TypeError): raise LedgerError('分页游标无效') from None
        query=query.order_by(s.tasks.c.created_at.desc().nulls_last(),s.tasks.c.id.desc()).limit(limit+1)
        with self.engine.connect() as db:
            rows=list(db.execute(query).mappings())
        items=[self.summarize(dict(row),{'queued_at':row['queued_at'],'elapsed_ms':row['elapsed_ms'],'status':row['run_status']},actor,row['member_role']) for row in rows[:limit]]
        next_cursor=None
        if len(rows)>limit:
            last=rows[limit-1]
            next_cursor=base64.urlsafe_b64encode(json.dumps({'filter':fingerprint,'created_at':wire(last['created_at']),'id':last['id']}).encode()).decode().rstrip('=')
        return {'items':items,'next_cursor':next_cursor}

    def detail(self, actor, task_id):
        with self.engine.connect() as db:
            task=self._task(db,actor,task_id)
            role=self.role(db,actor,task['workspace_id'])
            summary=self._summary(db,task,actor); summary['capabilities']=self.summarize(task,db.execute(select(s.task_runs).where(s.task_runs.c.id==task['current_run_id'])).mappings().first(),actor,role)['capabilities']
            result={'task':summary,'collection_cursors':{}}
            for name in ('inputs','runs','steps','ai_calls','artifacts','children'):
                page=self._collection(db,task_id,name,1000,None)
                result[name]=page['items'];result['collection_cursors'][name]=page['next_cursor']
        return result

    def _collection(self,db,task_id,name,limit,cursor):
        tables={'inputs':s.task_inputs,'runs':s.task_runs,'steps':s.task_step_runs,'ai_calls':s.ai_calls,'artifacts':s.task_artifact_links,'children':s.tasks}
        if name not in tables:raise LedgerError('详情集合不存在',404)
        table=tables[name]
        query=select(table) if name!='children' else select(table.c.id,table.c.name,table.c.status,table.c.parent_run_id)
        if name=='children':query=query.where(table.c.parent_task_id==task_id)
        elif name=='ai_calls':
            descendants=select(s.tasks.c.id).where(s.tasks.c.id==task_id).cte(recursive=True)
            descendants=descendants.union_all(select(s.tasks.c.id).join(descendants,s.tasks.c.parent_task_id==descendants.c.id))
            query=query.where(table.c.task_id.in_(select(descendants.c.id)))
        else:query=query.where(table.c.task_id==task_id)
        if cursor:
            try:point=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
            except (ValueError,TypeError):raise LedgerError('详情游标无效') from None
            if not isinstance(point,dict) or not isinstance(point.get('id'),str) or name=='runs' and type(point.get('attempt')) is not int:raise LedgerError('详情游标无效')
            if point.get('task')!=task_id or point.get('name')!=name:raise LedgerError('详情游标范围不一致')
            query=query.where(tuple_(table.c.attempt,table.c.id)>tuple_(point['attempt'],point['id'])) if name=='runs' else query.where(table.c.id>point['id'])
        query=query.order_by(table.c.attempt,table.c.id) if name=='runs' else query.order_by(table.c.id)
        rows=[dict(row) for row in db.execute(query.limit(limit+1)).mappings()];more=len(rows)>limit;rows=rows[:limit]
        next_cursor=None
        if more:
            point={'task':task_id,'name':name,'id':rows[-1]['id'],'attempt':rows[-1].get('attempt')}
            next_cursor=base64.urlsafe_b64encode(json.dumps(point).encode()).decode().rstrip('=')
        for row in rows:row.pop('source_path',None)
        return {'items':wire(rows),'next_cursor':next_cursor}

    def collection(self,actor,task_id,name,limit=200,cursor=None):
        if not 1<=limit<=1000:raise LedgerError('详情分页大小无效')
        with self.engine.connect() as db:
            self._task(db,actor,task_id)
            return self._collection(db,task_id,name,limit,cursor)

    def events(self, actor, task_id, after=0, limit=100):
        if after<0 or not 1<=limit<=500: raise LedgerError('事件游标无效')
        with self.engine.connect() as db:
            self._task(db,actor,task_id)
            rows=[dict(r) for r in db.execute(select(s.task_events).where(s.task_events.c.task_id==task_id,s.task_events.c.seq>after).order_by(s.task_events.c.seq).limit(limit)).mappings()]
        return {'items':wire(rows),'next_cursor':rows[-1]['seq'] if rows else after}

    def edit(self, actor, task_id, expected_revision, changes):
        if set(changes)-{'name','description'} or not changes or 'name' in changes and not changes['name'].strip(): raise LedgerError('任务修改参数无效')
        with self.engine.begin() as db:
            row=self._task(db,actor,task_id,control=True,lock=True)
            if row['revision']!=expected_revision: raise LedgerError('任务已变化，请刷新',409)
            db.execute(update(s.tasks).where(s.tasks.c.id==task_id).values(**changes,revision=row['revision']+1,updated_at=now()))
            self._event(db,task_id,row['current_run_id'],'metadata_changed',changes,actor)
            self.audit(db,actor,'task.edit',row['workspace_id'],task_id,changes)
        return self.detail(actor,task_id)['task']

    def control(self, actor, task_id, action, expected_revision, request_key, scope=None):
        if not isinstance(request_key,str) or not 1<=len(request_key)<=200:raise LedgerError('控制请求键无效')
        if action not in {'retry','cancel','pause','resume','verify','archive','unarchive'}: raise LedgerError('任务操作无效')
        payload={'task_id':task_id,'action':action,'expected_revision':expected_revision,'scope':scope or {}}
        from .private_payloads import request_fingerprint
        fingerprint=request_fingerprint(payload);scope=safe(scope or {})
        with self.engine.begin() as db:
            row=self._task(db,actor,task_id,control=True,lock=True)
            previous=db.execute(select(s.control_requests).where(s.control_requests.c.actor_id==actor['id'],s.control_requests.c.request_key==request_key)).mappings().first()
            if previous:
                if previous['request_hash']!=fingerprint: raise LedgerError('控制请求键冲突',409)
                return previous['receipt']
            if row['revision']!=expected_revision: raise LedgerError('任务已变化，请刷新',409)
            stamp=now(); run_id=row['current_run_id']; changes={'revision':row['revision']+1,'updated_at':stamp}
            if action in {'archive','unarchive'}:
                self.role(db,actor,row['workspace_id'],owner=True)
                if row['status'] not in TERMINAL|{'needs_verification'}: raise LedgerError('请先结束任务再归档',409)
                changes['archived']=action=='archive'
            elif action=='retry':
                if row['status'] not in {'failed','partial','cancelled','needs_verification'}: raise LedgerError('当前任务不能重试',409)
                original=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.run_id==run_id,s.task_dispatches.c.kind=='execute')).mappings().first()
                if not original: raise LedgerError('历史任务缺少可重放的执行请求',409)
                if original['state'] in {'leased','observing','needs_verification'}: raise LedgerError('原执行尚未核实结束，不能重复启动',409)
                if db.execute(select(s.task_dispatches.c.id).where(s.task_dispatches.c.run_id==run_id,s.task_dispatches.c.kind=='control',s.task_dispatches.c.state.in_(['leased','needs_verification'])).limit(1)).first():raise LedgerError('原控制请求尚未核实，不能启动新一轮执行',409)
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.run_id==run_id,s.task_dispatches.c.kind=='control',s.task_dispatches.c.state=='queued').values(state='obsolete',updated_at=stamp))
                prior=db.execute(select(s.task_runs).where(s.task_runs.c.id==run_id)).mappings().one()
                run_id=key('r'); generation=prior['generation']+1
                db.execute(insert(s.task_runs).values(id=run_id,task_id=task_id,attempt=row['run_count']+1,status='queued',generation=generation,
                    requested_by=actor['id'],request_snapshot={**prior['request_snapshot'],'requested_by_snapshot':{name:actor[name] for name in ('id','username','display_name')}},scope=scope or {}))
                db.execute(insert(s.task_dispatches).values(id=key('d'),task_id=task_id,run_id=run_id,kind='execute',state='queued',generation=generation,
                    command={**original['command'],'retry_of':prior['id'],'scope':scope or {}}))
                changes.update(current_run_id=run_id,run_count=row['run_count']+1,status='queued',availability='not_ready',progress_done=None,progress_total=None)
            else:
                if action=='verify' and 'verify' not in self.summarize(row,None,actor,'owner')['capabilities']:raise LedgerError('此历史记录没有可信执行命令，请从工具重新发起',409)
                if action=='cancel' and row['status']=='queued':
                    count=db.execute(update(s.task_dispatches).where(s.task_dispatches.c.run_id==run_id,s.task_dispatches.c.state=='queued',s.task_dispatches.c.kind=='execute').values(state='cancelled',updated_at=stamp)).rowcount
                    if count:
                        run=db.execute(select(s.task_runs).where(s.task_runs.c.id==run_id)).mappings().one()
                        db.execute(update(s.task_runs).where(s.task_runs.c.id==run_id).values(status='cancelled',finished_at=stamp,elapsed_ms=int((stamp-run['queued_at']).total_seconds()*1000)))
                        changes['status']='cancelled'
                    else: changes['status']='cancelling'
                else:
                    if action not in self.summarize(row,None,actor,'owner')['capabilities']: raise LedgerError('当前操作不可用',409)
                    changes['status']={'cancel':'cancelling','pause':'pausing','resume':'queued','verify':row['status']}[action]
                if changes['status']!='cancelled':
                    generation=db.execute(select(s.task_runs.c.generation).where(s.task_runs.c.id==run_id)).scalar_one()
                    db.execute(insert(s.task_dispatches).values(id=key('d'),task_id=task_id,run_id=run_id,kind='control',state='queued',generation=generation,
                        command={'action':action,'target_run_id':run_id,'actor_id':actor['id']}))
            db.execute(update(s.tasks).where(s.tasks.c.id==task_id).values(**changes))
            self._event(db,task_id,run_id,'control_requested',{'action':action},actor)
            self.audit(db,actor,'task.'+action,row['workspace_id'],task_id)
            receipt={'task_id':task_id,'run_id':run_id,'request_state':'recorded'}
            db.execute(insert(s.control_requests).values(id=key('c'),task_id=task_id,actor_id=actor['id'],request_key=request_key,request_hash=fingerprint,receipt=receipt))
        return receipt

    def claim(self, worker,heavy_limit=None):
        with self.engine.begin() as db:
            query=select(s.task_dispatches).where(s.task_dispatches.c.state=='queued')
            if heavy_limit is not None:
                if not 1<=heavy_limit<=4:raise LedgerError('重任务并发必须为 1 至 4')
                db.execute(text('SELECT pg_advisory_xact_lock(74001420261016)'))
                busy=db.execute(select(func.count()).select_from(s.task_dispatches.join(s.task_runs,s.task_dispatches.c.run_id==s.task_runs.c.id).join(s.tasks,s.task_runs.c.task_id==s.tasks.c.id)).where(s.task_dispatches.c.kind=='execute',
                    s.task_dispatches.c.state.in_(['leased','observing','needs_verification']),or_(s.task_runs.c.status.not_in(['paused','waiting_user']),s.tasks.c.status=='needs_verification'),func.coalesce(s.task_dispatches.c.command['resource_class'].astext,'heavy')!='io')).scalar_one()
                if busy>=heavy_limit:query=query.where(or_(and_(s.task_dispatches.c.kind=='control',func.coalesce(s.task_dispatches.c.command['action'].astext,'')!='resume'),s.task_dispatches.c.command['resource_class'].astext=='io'))
            row=db.execute(query.order_by(s.task_dispatches.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if not row: return None
            row=dict(row); lease=key('lease'); stamp=now()
            if row['kind']=='control' and row['command'].get('action')=='resume':
                db.execute(update(s.task_runs).where(s.task_runs.c.id==row['run_id'],s.task_runs.c.status.in_(['paused','waiting_user'])).values(status='queued'))
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(state='leased',worker_id=worker,lease_token=lease,lease_until=stamp+timedelta(seconds=60),heartbeat_at=stamp,updated_at=stamp))
            return {**row,'worker_id':worker,'lease_token':lease}

    def heartbeat(self, dispatch):
        with self.engine.begin() as db:
            result=db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id'],s.task_dispatches.c.lease_token==dispatch['lease_token'],s.task_dispatches.c.state.in_(['leased','observing'])).values(lease_until=now()+timedelta(seconds=60),heartbeat_at=now()))
        return bool(result.rowcount)

    def observe(self, dispatch, status, *, identity, observation=None, receipt=None, terminal=False):
        if status not in STATES: raise LedgerError('执行端状态无效')
        observation=safe(observation or {}); stamp=now()
        with self.engine.begin() as db:
            saved=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.id==dispatch['id']).with_for_update()).mappings().one()
            task=db.execute(select(s.tasks).where(s.tasks.c.id==dispatch['task_id']).with_for_update()).mappings().one()
            if saved['lease_token']!=dispatch['lease_token'] or task['current_run_id']!=dispatch['run_id'] or saved['generation']!=dispatch['generation']:
                raise LedgerError('旧执行代际不能提交结果',409)
            if db.execute(select(s.task_events.c.seq).where(s.task_events.c.identity==identity)).first(): return False
            run=db.execute(select(s.task_runs).where(s.task_runs.c.id==dispatch['run_id'])).mappings().one()
            if run['status'] in TERMINAL:
                # A late observation cannot replace a durable terminal result.
                return False
            if task['status'] in {'cancelling','pausing'} and status not in TERMINAL|{'paused','needs_verification'}:status=task['status']
            changes={'status':status}
            if status=='running' and run['started_at'] is None: changes.update(started_at=stamp,queue_ms=max(0,int((stamp-run['queued_at']).total_seconds()*1000)))
            if terminal:
                changes.update(finished_at=stamp,elapsed_ms=max(0,int((stamp-run['queued_at']).total_seconds()*1000)),error=observation.get('error'))
            db.execute(update(s.task_runs).where(s.task_runs.c.id==run['id']).values(**changes))
            updates={'status':status,'revision':task['revision']+1,'updated_at':stamp,'last_verified_at':stamp,'availability':observation.get('availability','ready')}
            for name in ('stage','native_status','progress_done','progress_total','summary'):
                if name in observation: updates[name]=observation[name]
            if 'summary' in updates:updates['summary']={**task['summary'],**updates['summary']}
            if status=='needs_verification':updates['summary']={**updates.get('summary',task['summary']),'retry_blocked':True}
            elif terminal:updates['summary']={**updates.get('summary',task['summary']),'retry_blocked':False}
            db.execute(update(s.tasks).where(s.tasks.c.id==task['id']).values(**updates))
            state='done' if terminal else 'needs_verification' if status=='needs_verification' else 'observing'
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==saved['id']).values(state=state,receipt=safe(receipt or saved['receipt']),updated_at=stamp,
                lease_until=stamp+timedelta(seconds=60),heartbeat_at=stamp))
            self._event(db,task['id'],run['id'],'state_changed',{'status':status,**observation},identity=identity)
        return True

    def expire_leases(self):
        with self.engine.begin() as db:
            rows=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.state.in_(['leased','observing']),s.task_dispatches.c.lease_until<now()).with_for_update(skip_locked=True)).mappings().all()
            for row in rows:
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(state='needs_verification',updated_at=now()))
                task=db.execute(select(s.tasks).where(s.tasks.c.id==row['task_id'])).mappings().one()
                db.execute(update(s.tasks).where(s.tasks.c.id==row['task_id'],s.tasks.c.current_run_id==row['run_id'],s.tasks.c.status.not_in(TERMINAL)).values(status='needs_verification',availability='unreachable',revision=s.tasks.c.revision+1,updated_at=now(),summary={**task['summary'],'retry_blocked':True}))
                self._event(db,row['task_id'],row['run_id'],'lease_expired',{'status':'needs_verification'})
        return len(rows)

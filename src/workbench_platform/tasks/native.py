"""Project-scoped same-origin access to the existing OCR and classification UI."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs,urlsplit,quote
import httpx
from sqlalchemy import select
from .store import Ledger,LedgerError,key,safe
from .database import f_path
from . import schema_v1 as s
from . import journal

BOOTSTRAP = r'''(() => {
 const base = new URL('.', document.currentScript.src).pathname;
 const csrf = () => document.cookie.split('; ').find(v => v.startsWith('clear_csrf='))?.slice(11) || '';
 const route = value => { const u = new URL(value, location.href); if (u.origin === location.origin && !u.pathname.startsWith(base) && !u.pathname.startsWith('/api/platform/v2/') && (u.pathname.startsWith('/api/') || u.pathname.startsWith('/assets/'))) return base + u.pathname.slice(1) + u.search + u.hash; return value; };
 const raw = window.fetch.bind(window);
 window.fetch = (input, init={}) => { const url = input instanceof Request ? input.url : String(input); const headers = new Headers(input instanceof Request ? input.headers : undefined); new Headers(init.headers).forEach((v,k)=>headers.set(k,v)); const method = init.method || (input instanceof Request ? input.method : 'GET'); if (!['GET','HEAD','OPTIONS'].includes(method.toUpperCase())) headers.set('X-Clear-CSRF', decodeURIComponent(csrf())); return raw(input instanceof Request ? new Request(route(url),input) : route(url), {...init,headers,credentials:'same-origin'}); };
 const rewrite = root => { root.querySelectorAll('a[href],img[src],link[href],source[src],form[action]').forEach(node=>{ for(const attr of ['href','src','action']) if(node.hasAttribute(attr)) node.setAttribute(attr,route(node.getAttribute(attr))); }); };
 document.addEventListener('DOMContentLoaded',()=>{rewrite(document); new MutationObserver(()=>rewrite(document)).observe(document.body,{childList:true,subtree:true});});
})();'''

def _native_db(root,application):
    return root/'native'/application/('workbench.sqlite3' if application=='ocr' else 'platform.sqlite3')

def native_project(db,application,kind,identifier):
    if application=='ocr':
        queries={
          'projects':'SELECT id FROM projects WHERE id=?',
          'images':'SELECT project_id FROM images WHERE id=?',
          'versions':'SELECT i.project_id FROM versions v JOIN images i ON i.id=v.image_id WHERE v.id=?',
          'tasks':'SELECT project_id FROM tasks WHERE id=?',
          'results':'SELECT t.project_id FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=?',
          'documents':'SELECT project_id FROM documents WHERE id=?',
          'pages':'SELECT d.project_id FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?',
          'regions':'SELECT d.project_id FROM regions r JOIN pages p ON p.id=r.page_id JOIN documents d ON d.id=p.document_id WHERE r.id=?',
        }
    else:
        queries={kind:f'SELECT project_id FROM {kind} WHERE id=?' for kind in ('assets','datasets','models','jobs')}
        queries['projects']='SELECT id FROM projects WHERE id=?'
    if kind not in queries:raise LedgerError('无法核实原生资源身份',403)
    try:row=db.execute(queries[kind],(identifier,)).fetchone()
    except sqlite3.Error:raise LedgerError('原生资源不可用',404) from None
    if not row:raise LedgerError('原生资源不存在',404)
    return row[0]

def check_native_scope(root,application,project,path,payload,query,*,admin_settings=False,trusted_task=False):
    path='/'+path.lstrip('/')
    if any(piece in {'.','..'} for piece in path.split('/')) or '\\' in path or ':' in path:raise LedgerError('原生路径无效',403)
    if not path.startswith('/api/'):return
    if path.startswith('/api/platform/v2/'):
        if trusted_task and (path in {'/api/platform/v2/task-retries','/api/platform/v2/document-stage-retries'} or path.startswith('/api/platform/v2/import-requests/')):return
        raise LedgerError('此原生入口仅供受信执行服务使用',403)
    if admin_settings and path.startswith(('/api/multimodal/external','/api/engine-packages','/api/diagnostics')):return
    global_reads={'/api/state','/api/health','/api/fusion/policies','/api/multimodal/models','/api/engines',
        '/api/v1/projects','/api/v1/runtime','/api/v1/health','/api/v1/project-create-capabilities'}
    path_db=_native_db(root,application)
    if not path_db.is_file():raise LedgerError('原生项目尚未启动',503)
    keys={'project_id':'projects','image_id':'images','image_ids':'images','version_id':'versions','version_ids':'versions',
          'task_id':'tasks','task_ids':'tasks','result_id':'results','result_ids':'results','document_id':'documents','page_id':'pages','region_id':'regions',
          'dataset_id':'datasets','asset_id':'assets','asset_ids':'assets'}
    if application=='classification':keys['model_id']='models'
    with closing(sqlite3.connect(path_db.as_uri()+'?mode=ro',uri=True,timeout=15)) as db:
        checked=False
        for kind,identifier in re.findall(r'/(projects|images|versions|tasks|results|documents|pages|regions|assets|datasets|models|jobs)/([A-Za-z0-9_-]+)',path):
            if native_project(db,application,kind,identifier)!=project:raise LedgerError('原生资源不属于当前项目',403)
            checked=True
        def walk(value):
            nonlocal checked
            if isinstance(value,dict):
                for name,item in value.items():
                    if name in {'path','paths','directory','root','output_dir','output_directory','export_directory'} and item:
                        permitted=(root/'native'/application/'uploads'/project).resolve()
                        for candidate in item if isinstance(item,list) else [item]:
                            if not isinstance(candidate,str) or not Path(candidate).is_absolute() or not Path(candidate).resolve().is_relative_to(permitted):
                                raise LedgerError('多人版请通过浏览器上传或平台资料库选择文件',403)
                    if name in keys and item is not None:
                        for identifier in item if isinstance(item,list) else [item]:
                            if native_project(db,application,keys[name],identifier)!=project:raise LedgerError('原生参数引用了其他项目资料',403)
                            checked=True
                    elif isinstance(item,(dict,list)):walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(payload);walk({name:values[0] if len(values)==1 else values for name,values in parse_qs(query).items()})
        if not checked and path not in global_reads:raise LedgerError('原生接口缺少可核实的项目范围',403)

def execution_action(method,path,payload):
    if method=='GET' and re.search(r'/(export|download|text|report)(?:/|$)',path):return 'export'
    if method not in {'POST','PUT'}:return None
    if re.search(r'/(export|download|report)(?:/|$)',path) and not path.endswith('/preflight'):return 'export'
    if re.fullmatch(r'(?:/)?api/(?:v1/)?projects/[A-Za-z0-9_-]+/(tasks|fusion|experiments|batch-jobs|dataset-versions|imports|uploads|images|documents)',path):
        return 'import' if path.rsplit('/',1)[1] in {'imports','uploads','images','documents'} else 'train' if path.endswith('/experiments') else 'analyze'
    if re.fullmatch(r'(?:/)?api/(results/[A-Za-z0-9_-]+/(multimodal|geometry|structure/(check|tool-retry))|(?:documents|pages)/[A-Za-z0-9_-]+/(process|render)|versions/[A-Za-z0-9_-]+/transform)',path):return 'analyze'
    return None

async def perform_native(legacy,root,value):
    application=value['application']; workspace=value['workspace_id'];link=value['link_id'];path=value['path'].lstrip('/')
    context=legacy.state.job_service._context(workspace,application,link)
    origin=urlsplit(context.base_url)
    if origin.scheme!='http' or origin.hostname!='127.0.0.1' or not origin.port:raise LedgerError('原生服务登记无效',503)
    prefix=f'/native/{workspace}/{application}/{link}/'
    if path=='clear-gateway.js':return {'status':200,'headers':{'content-type':'text/javascript; charset=utf-8'},'content_hex':BOOTSTRAP.encode().hex()}
    if value.get('body_file'):
        source=f_path(value['body_file'])
        if not source.is_relative_to(root/'native-requests') and not source.is_relative_to(root/'native-request-staging'):raise LedgerError('原生请求文件身份变化',409)
        content=source.read_bytes()
        if value.get('body_encoding')=='dpapi':
            from workbench_platform.agent.credentials import CredentialVault
            content=CredentialVault._crypt(content,decrypt=True)
        if hashlib.sha256(content).hexdigest()!=value['body_hash']:raise LedgerError('原生请求文件身份变化',409)
    else:content=bytes.fromhex(value.get('content_hex',''))
    payload={}
    if content and 'application/json' in value.get('content_type',''):
        try:payload=json.loads(content)
        except ValueError:raise LedgerError('原生 JSON 参数无效')
    if application=='ocr' and value['method']=='POST' and re.fullmatch(r'api/projects/[A-Za-z0-9_-]+/(tasks|fusion)',path) and value.get('idempotency_key'):
        payload.setdefault('request_id',value['idempotency_key']);content=json.dumps(payload,ensure_ascii=False).encode()
    check_native_scope(root,application,context.module_project_id,path,payload,value.get('query',''),admin_settings=value.get('admin_settings',False),trusted_task=value.get('trusted_task',False))
    if value['method'] not in {'GET','HEAD'} and path in {'api/state','api/v1/projects','api/engines','api/multimodal/models'}:
        raise LedgerError('此原生设置需由平台管理员管理',403)
    headers={'Origin':f'http://127.0.0.1:{origin.port}','X-Platform-Request':'1','Content-Type':value.get('content_type','application/json')}
    if application=='ocr':headers['Authorization']='Bearer '+legacy.state.job_service._token()
    if value.get('idempotency_key'):headers['Idempotency-Key']=value['idempotency_key']
    async with httpx.AsyncClient(timeout=180,follow_redirects=False) as client:
        response=await client.request(value['method'],context.base_url.rstrip('/')+'/'+path+('?' + value['query'] if value.get('query') else ''),headers=headers,content=content)
    data=response.content;media=response.headers.get('content-type','')
    if response.is_success and media.startswith('application/json'):
        body=response.json()
        if path=='api/state':
            body['projects']=[row for row in body.get('projects',[]) if row['id']==context.module_project_id]
            for name in ('data_directory','queue','fusion_queue','external_queue','document_queue','disk'):body.pop(name,None)
        elif path=='api/v1/projects':body=[row for row in body if row['id']==context.module_project_id]
        if isinstance(body,dict):
            for name in ('queue','fusion_queue','external_queue','document_queue'):
                if isinstance(body.get(name),dict):
                    original=body[name]
                    body[name]={key:item for key,item in original.items() if key in {'healthy','paused','busy','pending','running','waiting','workers'} and isinstance(item,(bool,int,float))}
                    if original.get('last_error'):body[name]['last_error']='执行服务需检查，请联系管理员'
        data=json.dumps(body,ensure_ascii=False).encode()
    elif media.startswith('text/html'):
        html=data.decode('utf-8')
        html=html.replace('src="/','src="'+prefix).replace('href="/','href="'+prefix)
        html=html.replace('<head>','<head><script src="'+prefix+'clear-gateway.js"></script>',1)
        data=html.encode()
    headers={name:value for name,value in response.headers.items() if name.lower() in {'content-type','content-disposition','content-security-policy','x-content-type-options'}}
    from .files import envelope
    return envelope(root,response.status_code,headers,data,force_file='attachment' in headers.get('content-disposition',''))

async def proxy_native(engine,legacy,root,request):
    value=await request.json();actor_id=request.headers.get('x-clear-actor')
    ledger=Ledger(engine)
    with engine.connect() as db:
        user=db.execute(select(s.users).where(s.users.c.id==actor_id,s.users.c.disabled.is_(False))).mappings().first()
        if not user:raise LedgerError('用户身份无效',403)
        role=ledger.role(db,user,value['workspace_id'],write=value['method'] not in {'GET','HEAD'})
        linked=db.execute(select(s.module_project_links).where(s.module_project_links.c.workspace_id==value['workspace_id'],s.module_project_links.c.link_id==value['link_id'],s.module_project_links.c.app_id==value['application'])).mappings().first()
        if not linked or value['method'] not in {'GET','HEAD'} and linked['role']=='viewer':raise LedgerError('原生项目关联权限不足',403)
    value['admin_settings']=bool(user['is_admin'])
    if value.get('staged_body_file'):
        source=f_path(value.pop('staged_body_file'))
        if not source.is_relative_to(root/'native-request-staging'):raise LedgerError('原生暂存请求身份无效',403)
        raw=source.read_bytes();value['body_file']=str(source);value['body_hash']=hashlib.sha256(raw).hexdigest()
    else:raw=bytes.fromhex(value.get('content_hex',''))
    payload={}
    if raw and 'application/json' in value.get('content_type',''):
        try:payload=json.loads(raw)
        except ValueError:raise LedgerError('原生参数无效')
    check_native_scope(root,value['application'],linked['module_project_id'],value['path'],payload,value.get('query',''),admin_settings=value['admin_settings'])
    if value['method'] not in {'GET','HEAD'} and re.search(r'/(queue|actions|cancel|retry|resume)(?:/|$)',value['path']):
        identifiers=re.findall(r'/(?:jobs|tasks)/([A-Za-z0-9_-]+)',value['path'])
        identifiers.extend(payload.get('task_ids',[]))
        ledger.authorize_native_control(dict(user),value['workspace_id'],identifiers)
        operation=payload.get('action') if value['path'].rstrip('/').endswith('/actions') else value['path'].rstrip('/').rsplit('/',1)[-1]
        operation={'retry_failed':'retry','retry_cpu':'retry','continue':'resume'}.get(operation,operation)
        if operation in {'cancel','pause','resume','retry'}:
            if not identifiers:raise LedgerError('请在平台任务中心选择任务后操作',409)
            with engine.connect() as db:
                rows=db.execute(select(s.tasks).join(s.task_source_links,s.tasks.c.id==s.task_source_links.c.task_id).where(s.tasks.c.workspace_id==value['workspace_id'],s.task_source_links.c.source_id.in_(identifiers))).mappings().all()
            if not rows:raise LedgerError('此旧任务没有可重放请求，请重新发起处理',409)
            for row in {row['id']:row for row in rows}.values():
                from .store import digest
                ledger.control(dict(user),row['id'],operation,row['revision'],'native-control:'+digest([value.get('idempotency_key') or key('control'),row['id']]))
            from .files import envelope
            return envelope(root,202,{'content-type':'application/json'},json.dumps({'request_state':'recorded'}).encode())
    action=execution_action(value['method'],value['path'],payload)
    if not action:
        if value['method'] not in {'GET','HEAD'}:
            with engine.begin() as db:ledger.audit(db,user,'native.'+value['method'].lower(),value['workspace_id'],value['path'],{'parameter_keys':list(payload)})
        return await perform_native(legacy,root,value)
    journal.install_all(root)
    request_key=payload.get('request_id') or value.get('idempotency_key') or request.headers.get('idempotency-key') or key('native')
    body_dir=root/'native-requests';body_dir.mkdir(exist_ok=True)
    from workbench_platform.agent.credentials import CredentialVault
    target=body_dir/(key('request')+'.bin');target.write_bytes(CredentialVault._crypt(raw))
    tool='visual_review' if value['application']=='ocr' and '/multimodal' in value['path'] else value['application']
    command={'tool_id':tool,'action':action,'transport':'native','method':value['method'],
        'path':'/internal/native','payload':{},'native':{k:v for k,v in value.items() if k!='content_hex'}|{'body_file':str(target),'body_encoding':'dpapi','body_hash':hashlib.sha256(raw).hexdigest()}}
    if action=='export':command['resource_class']='io'
    from .native_inputs import resolve,known_sizes
    inputs=known_sizes(engine,value['workspace_id'],resolve(root,value['application'],payload,value['path']))
    if action=='import':
        from .multipart import input_records
        inputs=input_records(raw,value.get('content_type','')) or inputs
    task=ledger.submit(dict(user),{'workspace_id':value['workspace_id'],'tool_id':tool,'action':action,'idempotency_key':request_key,'options':payload,'body_hash':hashlib.sha256(raw).hexdigest()},command,inputs)
    del raw
    receipt_run=task['current_run_id']
    if task['run_count']>1:
        with engine.connect() as db:receipt_run=db.execute(select(s.task_runs.c.id).where(s.task_runs.c.task_id==task['id']).order_by(s.task_runs.c.attempt).limit(1)).scalar_one()
    for _ in range(600):
        with engine.connect() as db:
            record=db.execute(select(s.task_dispatches.c.receipt,s.task_runs.c.status,s.task_runs.c.error).join(s.task_runs,s.task_dispatches.c.run_id==s.task_runs.c.id).where(s.task_dispatches.c.run_id==receipt_run,s.task_dispatches.c.kind=='execute')).first();receipt=record.receipt
        if receipt.get('native_response'):
            return {**receipt['native_response'],'headers':{**receipt['native_response']['headers'],'X-Clear-Task-ID':task['id']}}
        if record.status in {'failed','cancelled'}:
            from .files import envelope
            message=record.error or '任务在提交到工具前已结束'
            return envelope(root,409,{'content-type':'application/json','X-Clear-Task-ID':task['id']},json.dumps({'detail':message,'message':message,'task_id':task['id']},ensure_ascii=False).encode())
        await asyncio.sleep(.1)
    from .files import envelope
    return envelope(root,503,{'content-type':'application/json','X-Clear-Task-ID':task['id']},json.dumps({'detail':'任务已记录，执行回执待确认，请到任务中心查看','task_id':task['id']},ensure_ascii=False).encode())

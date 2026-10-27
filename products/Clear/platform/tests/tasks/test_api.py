import json
import secrets
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from workbench_platform.tasks.server import create_app
from workbench_platform.tasks import schema_v1 as s
from .conftest import submission

@pytest.fixture
def api(task_case,monkeypatch):
    ledger,admin,editor,viewer,other,workspace=task_case
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN','https://localhost:8889')
    monkeypatch.setenv('CLEAR_WORKER_TOKEN',secrets.token_urlsafe(40))
    def transport(request):
        if request.url.path=='/internal/scope':return httpx.Response(200,json={'workspace_id':workspace})
        if request.url.path=='/internal/inputs':return httpx.Response(200,json={'items':[]})
        return httpx.Response(404,json={'detail':'not found'})
    worker=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://127.0.0.1:8890')
    with TestClient(create_app(ledger.engine,worker),base_url='https://localhost:8889') as client:
        yield client,task_case

def login(client,user):
    response=client.post('/api/platform/v2/auth/login',headers={'Origin':'https://localhost:8889'},json={'username':user['username'],'password':'correct horse battery staple'})
    assert response.status_code==200
    return {'Origin':'https://localhost:8889','X-Clear-CSRF':response.json()['csrf_token']}

def test_authentication_and_csrf_are_required_for_writes(api):
    client,case=api;ledger,admin,editor,viewer,other,workspace=case
    assert client.get('/api/platform/v2/tasks').status_code==401
    headers=login(client,editor)
    response=client.post('/api/platform/v2/tasks',json=submission(workspace))
    assert response.status_code==403
    value=submission(workspace);value['options']={'session_id':'sessionA','version':1}
    assert client.post('/api/platform/v2/tasks',json=value,headers=headers).status_code==202
    assert client.get('/api/platform/v2/tasks').json()['items'][0]['created_by']==editor['id']

def test_actor_injection_is_rejected_and_scope_is_checked(api):
    client,case=api;ledger,admin,editor,viewer,other,workspace=case;headers=login(client,editor)
    value={**submission(workspace),'created_by':admin['id']}
    assert client.post('/api/platform/v2/tasks',json=value,headers=headers).status_code==422
    value=submission('notMyProject')
    assert client.post('/api/platform/v2/tasks',json=value,headers=headers).status_code==403

def test_cross_project_read_and_other_users_control_are_denied(api):
    client,case=api;ledger,admin,editor,viewer,other,workspace=case
    task=ledger.submit(editor,submission(workspace),{})
    headers=login(client,other)
    assert client.get('/api/platform/v2/tasks/'+task['id']).status_code==403
    headers=login(client,viewer)
    assert client.get('/api/platform/v2/tasks/'+task['id']).status_code==200
    assert client.post('/api/platform/v2/tasks/'+task['id']+'/controls',json={'operation':'cancel','expected_revision':task['revision'],'idempotency_key':'no-control'},headers=headers).status_code==403

def test_logout_invalidates_the_server_session(api):
    client,case=api;headers=login(client,case[2]);cookie=client.cookies.get('clear_session')
    assert client.post('/api/platform/v2/auth/logout',json={},headers=headers).status_code==200
    assert client.get('/api/platform/v2/auth/me',headers={'Cookie':'clear_session='+cookie}).status_code==401

def test_plain_http_is_not_accidentally_exposed(api,monkeypatch):
    client,case=api
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN','http://192.168.1.2:8889')
    monkeypatch.setenv('CLEAR_ALLOW_HTTP_LOOPBACK','1')
    with pytest.raises(RuntimeError,match='HTTPS'):create_app(case[0].engine)


def test_explicit_lan_http_preserves_login_csrf_project_scope_and_private_port(task_case,monkeypatch):
    ledger,admin,editor,viewer,other,workspace=task_case
    origin='http://192.168.1.8:9021'
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN',origin)
    monkeypatch.setenv('CLEAR_ALLOW_LAN_HTTP','1')
    monkeypatch.setenv('CLEAR_WORKER_TOKEN',secrets.token_urlsafe(40))
    monkeypatch.setenv('CLEAR_WORKER_URL','http://127.0.0.1:19091')
    def transport(request):
        assert request.headers['Host']=='127.0.0.1:19091'
        assert request.headers['Origin']=='http://127.0.0.1:19091'
        if request.url.path=='/internal/scope':return httpx.Response(200,json={'workspace_id':workspace})
        if request.url.path=='/internal/inputs':return httpx.Response(200,json={'items':[]})
        return httpx.Response(404)
    worker=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://127.0.0.1:19091')
    with TestClient(create_app(ledger.engine,worker),base_url=origin) as client:
        assert client.get('/api/platform/v2/tasks').status_code==401
        assert client.get('/api/platform/v2/health',headers={'Host':'other-server:9021'}).status_code==400
        bad=client.post('/api/platform/v2/auth/login',headers={'Origin':'http://other-server:9021'},json={'username':editor['username'],'password':'correct horse battery staple'})
        assert bad.status_code==403
        response=client.post('/api/platform/v2/auth/login',headers={'Origin':origin},json={'username':editor['username'],'password':'correct horse battery staple'})
        assert response.status_code==200
        session=next(cookie for cookie in response.headers.get_list('set-cookie') if cookie.startswith('clear_session='))
        assert 'HttpOnly' in session and 'SameSite=strict' in session and 'Secure' not in session
        assert 'Strict-Transport-Security' not in response.headers
        assert client.get('/api/platform/v2/auth/me').json()['user']['id']==editor['id']
        headers={'Origin':origin,'X-Clear-CSRF':response.json()['csrf_token']}
        value=submission(workspace,'explicit-lan');value['options']={'session_id':'sessionLAN','version':1}
        assert client.post('/api/platform/v2/tasks',json=value,headers={'Origin':origin}).status_code==403
        assert client.post('/api/platform/v2/tasks',json=value,headers={**headers,'Origin':'http://192.168.1.9:9021'}).status_code==403
        assert client.post('/api/platform/v2/tasks',json=value,headers=headers).status_code==202
        other_workspace=ledger.create_workspace(other,'LAN隔离项目')['workspace_id']
        assert client.get('/api/platform/v2/workspaces/'+other_workspace+'/members').status_code==403
        assert client.post('/api/platform/v2/users',json={'username':'denied','password':'correct horse battery staple','display_name':'管理员'},headers=headers).status_code==403


def test_https_cookies_remain_secure_with_loopback_http_flag(api,monkeypatch):
    client,case=api
    monkeypatch.setenv('CLEAR_ALLOW_HTTP_LOOPBACK','1')
    # Configuration is evaluated at app creation, including accidental leftover flags.
    worker=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(404)),base_url='http://127.0.0.1:8890')
    with TestClient(create_app(case[0].engine,worker),base_url='https://localhost:8889') as secure_client:
        response=secure_client.post('/api/platform/v2/auth/login',headers={'Origin':'https://localhost:8889'},json={'username':case[2]['username'],'password':'correct horse battery staple'})
        assert response.status_code==200
        assert all('Secure' in cookie for cookie in response.headers.get_list('set-cookie'))
        assert 'Strict-Transport-Security' in response.headers

def test_native_route_also_requires_login(api):
    client,case=api
    response=client.get('/native/workspace/ocr/link/index.html')
    assert response.status_code==401

def test_legacy_lists_are_forced_to_the_authorized_project(api):
    client,case=api;ledger,admin,editor,viewer,other,workspace=case;login(client,viewer)
    def transport(request):
        if request.url.path=='/internal/scope':return httpx.Response(200,json={'workspace_id':None})
        assert request.url.params['workspace_id']==workspace
        return httpx.Response(200,json={'items':[]})
    client.app.state.worker=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://127.0.0.1:8890')
    for path in ('data/sessions','batches'):
        assert client.get('/api/platform/v1/'+path,headers={'X-Clear-Workspace':workspace}).status_code==200

def test_native_project_directory_does_not_reveal_other_projects(api):
    from sqlalchemy import insert
    client,case=api;ledger,admin,editor,viewer,other,workspace=case;login(client,viewer)
    with ledger.engine.begin() as db:db.execute(insert(s.module_project_links).values(link_id='link-'+workspace,workspace_id=workspace,app_id='ocr',module_project_id='allowed-native',role='owner'))
    def transport(request):
        if request.url.path=='/internal/scope':return httpx.Response(200,json={'workspace_id':workspace})
        return httpx.Response(200,json={'items':[{'project_id':'allowed-native','name':'可见项目'},{'project_id':'other-native','name':'其他用户私有项目'}]})
    client.app.state.worker=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://127.0.0.1:8890')
    response=client.get(f'/api/platform/v1/workspaces/{workspace}/ocr/projects')
    assert response.status_code==200 and response.json()['items']==[{'project_id':'allowed-native','name':'可见项目'}]


@pytest.mark.parametrize('native_status',['running','waiting_job','waiting_user','paused'])
def test_existing_agent_run_keeps_task_identity_and_owner_control(api,native_status):
    from sqlalchemy import insert,func
    client,case=api;ledger,admin,owner,viewer,other,workspace=case
    task=ledger.submit(owner,{**submission(workspace,'owned-agent'),'tool_id':'agent','action':'message'}, {})
    native_id='agent'+workspace
    with ledger.engine.begin() as db:
        db.execute(insert(s.task_source_links).values(id='link'+workspace,task_id=task['id'],
            run_id=task['current_run_id'],namespace='agent.runs',source_id=native_id,source_generation=6 if native_status=='paused' else 7))
    def transport(request):
        if request.url.path=='/internal/scope':return httpx.Response(200,json={'workspace_id':workspace})
        if request.url.path=='/internal/inputs':return httpx.Response(200,json={'items':[]})
        if request.url.path.endswith('/agent/sessions/sessionOwned'):
            return httpx.Response(200,json={'runs':[{'id':native_id,'generation':7,'status':native_status}]})
        return httpx.Response(404)
    client.app.state.worker=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://127.0.0.1:8890')
    another=ledger.create_user('another-'+task['id'],'其他编制人','correct horse battery staple')
    ledger.set_member(admin,workspace,another['id'],'editor')
    value={**submission(workspace,'steer-owned'),'tool_id':'agent','action':'message',
           'options':{'session_id':'sessionOwned','payload':{'content':'继续原目标'}}}
    denied=client.post('/api/platform/v2/tasks',json=value,headers=login(client,another))
    assert denied.status_code==403
    accepted=client.post('/api/platform/v2/tasks',json=value,headers=login(client,owner))
    assert accepted.status_code==202 and accepted.json()['id']==task['id']
    with ledger.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(s.tasks).where(s.tasks.c.workspace_id==workspace)).scalar_one()==1
        assert db.execute(select(func.count()).select_from(s.control_requests).where(s.control_requests.c.task_id==task['id'])).scalar_one()==1


@pytest.mark.parametrize('operation',['pause','compact'])
def test_harness_history_and_pause_controls_require_original_task_owner(api,operation):
    from sqlalchemy import insert,update,func
    client,case=api;ledger,admin,owner,viewer,other,workspace=case
    task=ledger.submit(owner,{**submission(workspace,'harness-control'),'tool_id':'agent','action':'message'}, {})
    native_id='run'+workspace
    session_id='session'+workspace
    with ledger.engine.begin() as db:
        db.execute(insert(s.task_source_links).values(id='control-link'+workspace,task_id=task['id'],
            run_id=task['current_run_id'],namespace='agent.runs',source_id=native_id,source_generation=6))
        db.execute(update(s.tasks).where(s.tasks.c.id==task['id']).values(status='running',summary={'native_capabilities':['cancel','pause']}))
        db.execute(update(s.task_runs).where(s.task_runs.c.id==task['current_run_id']).values(status='running'))
        ledger.audit(db,owner,'agent.session.create',workspace,session_id)
    forwarded=[]
    def transport(request):
        if request.url.path=='/internal/scope':return httpx.Response(200,json={'workspace_id':workspace})
        if request.method=='POST':forwarded.append(request.url.path)
        if request.url.path.endswith('/compact'):return httpx.Response(202,json={'queued':True})
        if request.url.path.endswith('/agent/sessions/'+session_id):
            return httpx.Response(200,json={'runs':[{'id':native_id,'generation':7,'status':'running'}]})
        return httpx.Response(200,json={'id':native_id,'generation':7,'status':'running'})
    client.app.state.worker=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url='http://127.0.0.1:8890')
    another=ledger.create_user('controller-'+task['id'],'其他编制人','correct horse battery staple')
    ledger.set_member(admin,workspace,another['id'],'editor')
    path=('/api/platform/v1/agent/runs/'+native_id+'/pause' if operation=='pause'
          else '/api/platform/v1/agent/sessions/'+session_id+'/compact')
    for denied_user in (another,viewer):
        response=client.post(path,json={},headers=login(client,denied_user))
        assert response.status_code==403
    assert not forwarded
    accepted=client.post(path,json={},headers=login(client,owner))
    assert accepted.status_code==(200 if operation=='pause' else 202),accepted.json()
    with ledger.engine.connect() as db:
        count=db.execute(select(func.count()).select_from(s.task_dispatches).where(s.task_dispatches.c.task_id==task['id'],s.task_dispatches.c.kind=='control')).scalar_one()
        assert count==(1 if operation=='pause' else 0)
    assert forwarded==([] if operation=='pause' else [path])

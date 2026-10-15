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

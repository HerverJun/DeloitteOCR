"""Exercise immutable batch bridge, uncertain-submit recovery and XLSX mapping.

Native answers are deterministic engineering fixtures, not accuracy evidence.
"""
import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
import zipfile
import importlib

import pytest
from fastapi.testclient import TestClient
from workbench_platform.app import app
from workbench_platform.jobs.native_ocr import OcrUnavailable
from workbench_platform.jobs.screen_batches import ScreenSubmit

LOCAL={'host':'127.0.0.1:8766','origin':'http://127.0.0.1:8766'}


def workbook():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
    return output.getvalue()


@pytest.fixture
def bridge(tmp_path,monkeypatch,request):
    amount=getattr(request,'param',240)
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('WORKBENCH_AGENT_ENABLED','0')
    monkeypatch.setattr(importlib.import_module('workbench_platform.app'),'configure_forensics',lambda value:setattr(value.state,'forensic_service',None))
    with TestClient(app,base_url='http://127.0.0.1:8766') as client:
        workspace=client.post('/api/platform/v1/workspaces',headers=LOCAL,json={'schema_version':'1.0','name':'screen'}).json()['workspace_id']
        assets=f'/api/platform/v1/workspaces/{workspace}/assets'
        ids=[]
        for index in range(amount):
            ids.append(client.post(assets+'/uploads?name=same.png',headers=LOCAL,content=b'image-'+str(index).encode()).json()['asset_id'])
        fixed=client.post(assets+'/sets',headers=LOCAL,json={'asset_ids':ids}).json()
        handoff=app.state.ocr_handoff
        state={'submitted':False,'posts':0,'status':'running','pending':amount,'fail_once':False,'wrong_image':False,'upgrades':{},
            'defaults':{'primary_engine':'glm','group_size':32,'max_upgrades':1},'policy':None}
        original={'handoff_id':'handoffA','link_id':'linkA','asset_set_id':fixed['asset_set_id'],'asset_set_revision':1}
        imported=[{'item_id':item['item_id'],'asset_id':item['asset_id'],'name':'same.png',
            'target_version_id':'version'+str(index),'target_asset_id':'image'+str(index)} for index,item in enumerate(fixed['items'])]
        monkeypatch.setattr(handoff,'begin',lambda *_:{'state':'completed','handoff_id':'handoffA','items':imported})
        monkeypatch.setattr(handoff,'_row',lambda *_:original)
        native_context=SimpleNamespace(module_project_id='projectA',instance_id='instanceA',role='owner')
        monkeypatch.setattr(handoff,'_context',lambda *_:native_context)

        class Native:
            context=native_context
            def _request(self,method,path,body=None,**kwargs):
                parsed=urlsplit(path)
                if 'screen-batch-requests' in path:
                    return {'submitted':state['submitted'],'batch_id':'batchA' if state['submitted'] else None}
                if method=='POST' and parsed.path.endswith('/screen-batches'):
                    state['posts']+=1;state['submitted']=True
                    state['policy']={**state['defaults'],**body['policy']};state['specified_policy']=body['policy']
                    assert len(body['version_ids'])==amount
                    if state['fail_once']:
                        state['fail_once']=False
                        raise OcrUnavailable()
                    return {'batch_id':'batchA'}
                if method=='POST':
                    if body['action']=='upgrade':
                        state['upgrades'].setdefault(body['request_id'],body['version_ids'])
                        return {'batch_id':'batchUpgrade','parent_batch_id':'batchA','policy':{'primary_engine':'hunyuan'}}
                    state['status']='paused' if body['action']=='pause' else 'cancelled' if body['action']=='cancel' else 'running'
                    if body['action']=='cancel':state['pending']=0
                query=parse_qs(parsed.query);offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['30'])[0])
                upgraded='batchUpgrade' in parsed.path
                positions=[int(version.replace('version','')) for versions in state['upgrades'].values() for version in versions] if upgraded else list(range(amount))
                items=[{'input_version_id':'version'+str(index),'image_id':'image'+str(index),
                    'status':'pending' if index<state['pending'] else 'completed','stage':'upgrade' if state['pending'] else 'complete',
                    'result_id':'result'+str(index),'result_revision':0,'issues':[]} for index in positions]
                if upgraded:
                    for item in items:item.update(status='pending',stage='upgrade',result_id=None,result_revision=None)
                if state['wrong_image']:items[0]['image_id']='foreign'
                status=query.get('status',[None])[0]
                selected=[item for item in items if status is None or item['status']==status]
                cancelled=amount if state['status']=='cancelled' else 0
                pending=len(positions) if upgraded else state['pending']
                return {'batch_id':'batchUpgrade' if upgraded else 'batchA','project_id':'projectA','status':'running' if upgraded else state['status'],
                    'counts':{'total':len(positions),'completed':len(positions)-pending-cancelled,'needs_review':0,'failed':0,'cancelled':cancelled,'pending':pending},
                    'items':selected[offset:offset+limit],'filtered_total':len(selected),
                    'policy':{'primary_engine':'hunyuan'} if upgraded else state['policy']}
            def screen_export(self,project_id,batch_id,options):
                output=io.BytesIO();items=[]
                with zipfile.ZipFile(output,'w') as archive:
                    for index in range(amount):
                        name=f'same-{index:04d}.xlsx'
                        archive.writestr(name,workbook())
                        items.append({'input_version_id':'version'+str(index),'image_id':'image'+str(index),
                            'result_id':'result'+str(index),'result_revision':0,'status':'completed','file_name':name})
                    archive.writestr('manifest.json',json.dumps({'items':items,'success_files':amount}))
                return output.getvalue()
        monkeypatch.setattr(handoff,'_adapter',lambda _:Native())
        yield client,workspace,fixed,state,app.state.screen_batches,assets


def arguments(fixed):
    return {'link_id':'linkA','asset_set_id':fixed['asset_set_id'],'asset_set_revision':1,'request_id':'requestA','policy':{}}


def test_240_batch_replay_bounded_progress_and_immutable_policy(bridge):
    client,workspace,fixed,state,service,_=bridge
    route=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches'
    assert client.post(route,json=arguments(fixed)).status_code==403
    first=client.post(route,headers=LOCAL,json=arguments(fixed));assert first.status_code==201,first.text
    assert len(first.json()['items'])==10 and first.json()['counts']['total']==240
    assert client.post(route,headers=LOCAL,json=arguments(fixed)).status_code==201
    assert state['posts']==1
    changed=arguments(fixed);changed['policy']={'mode':'accuracy'}
    assert client.post(route,headers=LOCAL,json=changed).status_code==409
    state['pending']=3
    status=client.get(route+'/batchA?offset=0&limit=2&status=pending').json()
    assert status['counts']['completed']==237 and len(status['items'])==2 and status['filtered_total']==3
    assert client.get(route+'/batchA?limit=1000').status_code==400
    state['wrong_image']=True
    assert client.get(route+'/batchA').status_code==503


def test_lost_submit_recovers_original_request_without_new_task(bridge):
    _,workspace,fixed,state,service,_=bridge
    state['fail_once']=True
    args=ScreenSubmit.model_validate(arguments(fixed))
    with pytest.raises(OcrUnavailable):service.submit(workspace,args)
    restored=service.submit(workspace,args,allow_new=False)
    assert restored['batch_id']=='batchA' and state['posts']==1


@pytest.mark.parametrize('bridge',[4],indirect=True)
def test_native_defaults_are_resolved_once_and_lost_submit_recovers_after_defaults_change(bridge):
    _,workspace,fixed,state,service,_=bridge
    state['fail_once']=True
    args=ScreenSubmit.model_validate(arguments(fixed))
    with pytest.raises(OcrUnavailable):service.submit(workspace,args)
    assert state['specified_policy']=={}
    state['defaults']={'primary_engine':'hunyuan','group_size':16,'max_upgrades':2}
    restored=service.submit(workspace,args,allow_new=False)
    assert restored['policy']=={'primary_engine':'glm','group_size':32,'max_upgrades':1}
    pinned=restored['resolved_policy_sha256']
    assert service.submit(workspace,args)['resolved_policy_sha256']==pinned
    assert state['posts']==1
    state['policy']['primary_engine']='hunyuan'
    with pytest.raises(OcrUnavailable):service.status(workspace,'batchA')


@pytest.mark.parametrize('bridge',[4],indirect=True)
def test_policy_schema_omits_api_defaults_and_explicit_values_cross_bridge(bridge):
    from workbench_platform.agent.tools.screen_batches import Run
    properties=Run.model_json_schema()['$defs']['ScreenPolicy']['properties']
    assert all('default' not in field for field in properties.values())
    assert properties['group_size']['minimum']==1 and properties['max_upgrades']['maximum']==2
    client,workspace,fixed,state,_,_=bridge
    body=arguments(fixed)
    body['policy']={'group_size':8,'max_upgrades':0,'layout_evidence':False,'budget_seconds':None,'request_timeout_seconds':180}
    response=client.post(f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches',headers=LOCAL,json=body)
    assert response.status_code==201,response.text
    assert state['specified_policy']=={'group_size':8,'max_upgrades':0,'layout_evidence':False,'request_timeout_seconds':180}
    assert response.json()['policy']=={'primary_engine':'glm','group_size':8,'max_upgrades':0,'layout_evidence':False,'request_timeout_seconds':180}
    from workbench_platform.jobs.screen_batches import ScreenPolicy
    for invalid in (True,'180',9,901):
        with pytest.raises(ValueError):ScreenPolicy(request_timeout_seconds=invalid)


def test_export_240_unique_workbooks_and_manifest_is_managed(bridge):
    client,workspace,fixed,state,_,_=bridge
    route=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches'
    assert client.post(route,headers=LOCAL,json=arguments(fixed)).status_code==201
    state.update(pending=0,status='completed')
    exported=client.post(route+'/batchA/export',headers=LOCAL,json={});assert exported.status_code==201,exported.text
    bundle=exported.json()
    zipped=next(file['name'] for file in bundle['files'] if file['name'].endswith('.zip'))
    download=client.get(f'/api/platform/v1/workspaces/{workspace}/artifacts/{bundle["bundle_id"]}/files/{zipped}')
    assert download.status_code==200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        assert manifest['success_files']==240 and len(manifest['items'])==240
        assert len({item['file_name'] for item in manifest['items']})==240
    replay=client.post(route+'/batchA/export',headers=LOCAL,json={}).json()
    assert replay['bundle_id']==bundle['bundle_id']


def test_actions_restrict_scope_and_request_rebinding(bridge):
    client,workspace,fixed,_,_,_=bridge
    route=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches'
    client.post(route,headers=LOCAL,json=arguments(fixed))
    foreign={'action':'retry_failed','request_id':'actionA','version_ids':['foreign']}
    assert client.post(route+'/batchA/action',headers=LOCAL,json=foreign).status_code==400
    payload={'action':'pause','request_id':'actionA'}
    assert client.post(route+'/batchA/action',headers=LOCAL,json=payload).json()['status']=='paused'
    payload['action']='cancel'
    assert client.post(route+'/batchA/action',headers=LOCAL,json=payload).status_code==409
    review={'action':'review','request_id':'reviewA','version_ids':['version0']}
    assert client.post(route+'/batchA/action',headers=LOCAL,json=review).status_code==400


@pytest.mark.parametrize('bridge',[4],indirect=True)
def test_upgrade_preserves_original_and_fixes_a_derived_input_subset(bridge):
    client,workspace,fixed,state,service,_=bridge
    route=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches'
    assert client.post(route,headers=LOCAL,json=arguments(fixed)).status_code==201
    state.update(pending=0,status='completed')
    args={'action':'upgrade','request_id':'upgradeA','version_ids':['version1','version3']}
    upgraded=client.post(route+'/batchA/action',headers=LOCAL,json=args)
    assert upgraded.status_code==200,upgraded.text
    value=upgraded.json()
    assert value['batch_id']=='batchUpgrade' and value['counts']['total']==2
    assert value['asset_set_id']!=fixed['asset_set_id']
    derived=service.fixed(workspace,service._row(workspace,'batchUpgrade'))
    assert len(derived.items)==2
    assert {item.asset_id for item in derived.items}=={fixed['items'][1]['asset_id'],fixed['items'][3]['asset_id']}
    assert client.get(route+'/batchA').json()['counts']['completed']==4
    assert client.post(route+'/batchA/action',headers=LOCAL,json=args).json()['batch_id']=='batchUpgrade'
    assert len(state['upgrades'])==1
    args['version_ids']=['version0']
    assert client.post(route+'/batchA/action',headers=LOCAL,json=args).status_code==409

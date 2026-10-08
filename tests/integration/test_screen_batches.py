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
from workbench_platform.jobs.ocr_handoff import _json
import hashlib

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
                if parsed.path.endswith('/delivery-snapshot'):
                    batch_id = parsed.path.split('/')[-2]
                    value = self._request('GET', f'/api/projects/projectA/screen-batches/{batch_id}?limit=1000')
                    items = [{**item, 'result_revision': state.get('result_revision', 0),
                        'review_state': state.get('review_state', 'pending'),
                        'active_version': item['input_version_id'],
                        'human_review_status': state.get('human_review_status', 'pending')}
                        for item in value['items']]
                    identity = {'batch_id': batch_id, 'project_id': 'projectA', 'policy_sha256': 'a' * 64, 'items': items}
                    return {**identity, 'snapshot_id': hashlib.sha256(_json(identity).encode()).hexdigest(), 'fresh': True, 'stale_items': []}
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
                        return {'batch_id':'batchUpgrade','parent_batch_id':'batchA',
                            'policy':{'primary_engine':'glm','upgrade_trigger':'quality','require_human_review':True}}
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
                    'policy':{'primary_engine':'glm','upgrade_trigger':'quality','require_human_review':True} if upgraded else state['policy']}
            def screen_export(self,project_id,batch_id,options):
                output=io.BytesIO();items=[]
                snapshot = self._request('GET', f'/api/projects/{project_id}/screen-batches/{batch_id}/delivery-snapshot')
                with zipfile.ZipFile(output,'w') as archive:
                    for index in range(amount):
                        name=f'same-{index:04d}.xlsx'
                        archive.writestr(name,workbook())
                        items.append({**snapshot['items'][index], 'file_name':name})
                    archive.writestr('manifest.json',json.dumps({'items':items,'success_files':amount,
                        'snapshot_id': '0' * 64 if state.get('stale_export') else snapshot['snapshot_id'],
                        'version_vector': {**{key: snapshot[key] for key in
                            ('snapshot_id','batch_id','project_id','policy_sha256','items')},
                            'export_options': {'include_pending_review': options.get('include_pending_review', False),
                                               'numeric_columns': options.get('numeric_columns')}},
                        'export_options': {'include_pending_review': options.get('include_pending_review', False),
                                           'numeric_columns': options.get('numeric_columns')}}))
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
    body['policy']={'group_size':8,'max_upgrades':0,'layout_evidence':False,'budget_seconds':None,'request_timeout_seconds':180,
        'upgrade_trigger':'failure_only','require_human_review':True}
    response=client.post(f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches',headers=LOCAL,json=body)
    assert response.status_code==201,response.text
    assert state['specified_policy']=={'group_size':8,'max_upgrades':0,'layout_evidence':False,'request_timeout_seconds':180,
        'upgrade_trigger':'failure_only','require_human_review':True}
    assert response.json()['policy']=={'primary_engine':'glm','group_size':8,'max_upgrades':0,'layout_evidence':False,'request_timeout_seconds':180,
        'upgrade_trigger':'failure_only','require_human_review':True}
    from workbench_platform.jobs.screen_batches import ScreenPolicy
    for invalid in (True,'180',9,901):
        with pytest.raises(ValueError):ScreenPolicy(request_timeout_seconds=invalid)
    for invalid in ('always',True):
        with pytest.raises(ValueError):ScreenPolicy(upgrade_trigger=invalid)
    for invalid in (0,1,'true'):
        with pytest.raises(ValueError):ScreenPolicy(require_human_review=invalid)


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
    assert value['policy']=={'primary_engine':'glm','upgrade_trigger':'quality','require_human_review':True}
    assert value['asset_set_id']!=fixed['asset_set_id']
    derived=service.fixed(workspace,service._row(workspace,'batchUpgrade'))
    assert len(derived.items)==2
    assert {item.asset_id for item in derived.items}=={fixed['items'][1]['asset_id'],fixed['items'][3]['asset_id']}
    assert client.get(route+'/batchA').json()['counts']['completed']==4
    assert client.post(route+'/batchA/action',headers=LOCAL,json=args).json()['batch_id']=='batchUpgrade'
    assert len(state['upgrades'])==1
    args['version_ids']=['version0']
    assert client.post(route+'/batchA/action',headers=LOCAL,json=args).status_code==409


@pytest.mark.parametrize('bridge',[4],indirect=True)
@pytest.mark.parametrize('existing_marker',[False,True])
def test_cold_restore_keeps_screen_parent_subset_policy_and_export_identity(bridge,monkeypatch,existing_marker):
    from copy import deepcopy
    from contextlib import closing
    import hashlib
    from scripts.backup_workspace import create,restore,verify
    from scripts.product_compatibility import DATA_COMPATIBILITY,check
    from workbench_platform.jobs.screen_batches import ScreenAction,ScreenExport
    client,workspace,fixed,state,service,_=bridge
    route=f'/api/platform/v1/workspaces/{workspace}/ocr/screen-batches'
    assert client.post(route,headers=LOCAL,json=arguments(fixed)).status_code==201
    state.update(pending=0,status='completed')
    bundle=service.export(workspace,'batchA',ScreenExport())
    upgrade=ScreenAction(action='upgrade',request_id='coldUpgrade',version_ids=['version1','version3'])
    child=service.action(workspace,'batchA',upgrade)
    original=dict(service.handoff._row(workspace,'handoffA'))
    native_context=service.handoff._context(workspace,'linkA');native=service.handoff._adapter(native_context)
    source=service.path.parent;archive=source.with_name(source.name+'-archive');recovered=source.with_name(source.name+'-recovered')
    if existing_marker:
        (source/'product-reader-requirements.json').write_text(json.dumps({'format':'clear-product-reader-requirements-v1',
            'reader_features':['fixed-draft-transfer-v1'],'note':'保留原读取能力'}),encoding='utf-8')
    tables=('screen_batch_refs','screen_batch_actions','screen_batch_exports','screen_batch_subsets','screen_batch_policy_receipts')
    with closing(service._connect()) as db:
        before={table:[dict(row) for row in db.execute('SELECT * FROM '+table)] for table in tables}
    old_artifact=app.state.artifact_center.file(workspace,bundle['bundle_id'],'ocr-results.zip')
    old_hash=old_artifact.blob_hash
    # Close the fixture's ASGI lifespan before the actual cold backup. Native
    # HTTP is deterministic; reference, subset, policy and artifact stores are
    # the production services. This is persistence evidence, not OCR quality.
    client.__exit__(None,None,None)
    source_marker=source/'product-reader-requirements.json'
    source_marker_before=source_marker.read_bytes() if source_marker.exists() else None
    manifest=create(source,archive)
    assert 'screen-batches.sqlite3' in manifest['files']
    assert 'product-reader-requirements.json' in manifest['files']
    assert verify(archive)['files']==manifest['files']
    assert (source_marker.read_bytes() if source_marker.exists() else None)==source_marker_before
    restore(archive,recovered)
    marker=json.loads((recovered/'product-reader-requirements.json').read_text(encoding='utf-8'))
    assert 'screen-batch-v1' in marker['reader_features']
    if existing_marker:
        assert 'fixed-draft-transfer-v1' in marker['reader_features'] and marker['note']=='保留原读取能力'
    assert check(recovered,DATA_COMPATIBILITY)['compatible']
    old=deepcopy(DATA_COMPATIBILITY);old['reader_features'].remove('screen-batch-v1')
    with pytest.raises(ValueError,match='不能直接降级'):check(recovered,old)
    monkeypatch.setenv('WORKBENCH_DATA_DIR',str(recovered))
    with TestClient(app,base_url='http://127.0.0.1:8766'):
        reopened=app.state.screen_batches
        monkeypatch.setattr(reopened.handoff,'_row',lambda *_:original)
        monkeypatch.setattr(reopened.handoff,'_context',lambda *_:native_context)
        monkeypatch.setattr(reopened.handoff,'_adapter',lambda *_:native)
        with closing(reopened._connect()) as db:
            after={table:[dict(row) for row in db.execute('SELECT * FROM '+table)] for table in tables}
        assert after==before
        assert reopened.submit(workspace,ScreenSubmit.model_validate(arguments(fixed)),allow_new=False)['batch_id']=='batchA'
        assert reopened.fixed(workspace,reopened._row(workspace,child['batch_id'])).asset_set_id==child['asset_set_id']
        assert reopened.action(workspace,'batchA',upgrade)['batch_id']==child['batch_id']
        assert reopened.export(workspace,'batchA',ScreenExport())['bundle_id']==bundle['bundle_id']
        artifact=app.state.artifact_center.file(workspace,bundle['bundle_id'],'ocr-results.zip')
        with app.state.artifact_center.store.stage_download(workspace,artifact.artifact_id,
            expected_revision=artifact.revision,expected_hash=artifact.blob_hash) as path:
            assert hashlib.sha256(path.read_bytes()).hexdigest()==old_hash
    assert state['posts']==1 and len(state['upgrades'])==1
    incomplete=json.loads((archive/'manifest.json').read_text(encoding='utf-8'))
    incomplete['files'].pop('product-reader-requirements.json')
    (archive/'manifest.json').write_text(json.dumps(incomplete),encoding='utf-8')
    with pytest.raises(ValueError,match='capabilities are missing'):
        verify(archive)

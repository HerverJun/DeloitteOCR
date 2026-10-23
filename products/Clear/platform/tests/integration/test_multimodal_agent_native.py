"""Native binding/fault tests with a declared synthetic worker (no model claim)."""
from contextlib import closing
from dataclasses import asdict, dataclass
import copy
import csv
import io
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_multimodal_dispatch import service, until, raw
from workbench_platform.batches.store import encoded, fingerprint
from workbench_platform.data_api.service import DataError
from workbench_platform.resources.gpu import Owner, ResourceEvidence
from workbench_platform.multimodal.service import MultimodalService, SOURCE

NATIVE_CAPABILITIES=MultimodalService.capabilities


def terminal(value, job, state='succeeded'):
    return until(lambda: item if (item := value.get('wTest', job))['status'] == state else None)


def seed_reference(value, identifier='fAllowed', workspace='wTest'):
    item={'reference_id':identifier,'revision':1,'label':'授权参考','feature':[.1,.2,.3],
          'source':{'asset_id':'aReference','asset_revision':1,'blob_hash':'b'*64},
          'box':[1,2,40,60],'model_hash':'c'*64,'created_at':'2026-10-16T00:00:00Z','job_id':'mReference'}
    with value.transaction() as db:
        db.execute('INSERT INTO manual_face_reference VALUES(?,?,?,?,?)',
            (identifier,workspace,'add'+identifier,fingerprint(item),encoded(item)))
    return item


def test_frozen_face_scope_survives_library_delete_and_rejects_other_workspace(service):
    value,create,calls,_=service
    reference=seed_reference(value);seed_reference(value,'fForeign','wOther')
    value.coordinator.request('blockSearch','ocr')
    input_ref={'asset_id':'aTest','asset_revision':1,'blob_hash':value.registry.get('wTest','aTest').blob_hash}
    def submit(request,ids):
        return value.create('wTest',{'request_id':request,'kind':'face','inputs':[input_ref],
            'options':{'action':'search','device':'cuda','reference_ids':ids}})
    with pytest.raises(DataError,match='不属于'):submit('foreignReference',['fForeign'])
    with pytest.raises(DataError,match='至少一个'):submit('emptyReference',[])
    job=submit('authorizedSearch',['fAllowed'])
    assert value.lookup_operation('wTest','create','authorizedSearch')['resource_id']==job['job_id']
    with pytest.raises(DataError):value.delete_reference('wTest','fAllowed',{'request_id':'wrongVersion','expected_revision':2})
    deleted=value.delete_reference('wTest','fAllowed',{'request_id':'deleteAuthorized','expected_revision':1})
    assert value.delete_reference('wTest','fAllowed',{'request_id':'deleteAuthorized','expected_revision':1})==deleted
    assert value.references('wTest')['items']==[]
    value.coordinator.cancel_pending('blockSearch','ocr');value.wake.set()
    terminal(value,job['job_id'])
    assert calls[0]['references']==[reference]
    frozen=value.snapshot('wTest',job['job_id'])
    assert frozen['reference_scope'][0]['reference_id']=='fAllowed'
    assert 'feature' not in encoded(frozen)
    with pytest.raises(DataError):value.snapshot('wOther',job['job_id'],frozen['snapshot_id'])


def test_lost_control_response_replays_same_generation_and_lookup_receipt(service):
    value,create,calls,outcomes=service;outcomes.append('failed')
    job=create('initialFailure',device='cpu');failed=terminal(value,job['job_id'],'failed')
    until(lambda:job['job_id'] not in value.inflight)
    command={'request_id':'retryLostResponse','expected_revision':failed['revision'],'expected_generation':1}
    first=value.control('wTest',job['job_id'],'retry',command)
    terminal(value,job['job_id'])
    assert value.control('wTest',job['job_id'],'retry',command)==first
    receipt=value.lookup_operation('wTest','control:retry','retryLostResponse')
    assert receipt['found'] and receipt['receipt']['generation']==2
    assert len(calls)==2 and value.list('wTest')['total']==1
    with pytest.raises(DataError,match='绑定其他参数'):
        value.control('wTest',job['job_id'],'retry',{**command,'expected_generation':2})
    with pytest.raises(DataError):value.control('wOther',job['job_id'],'cancel',{'request_id':'crossWorkspace'})
    assert value.lookup_operation('wOther','create','initialFailure')['found'] is False


def seal_result(value,job):
    with value.transaction() as db:
        item=value.row(db,'wTest',job)
        source=value.region_source(item['inputs'][0],1,[1,2,100,100])
        item['result']={'device':'cpu','model_hashes':{'seal':'d'*64},'output_files':{},
            'seals':[{'text':'原始候选 001','subject':'候选公司','number':'000123','confidence':.73,
                      'page_number':1,'box':[1,2,100,100],'source':source}]}
        from workbench_platform.multimodal.revisions import attach_row_ids
        attach_row_ids(item,item['result']);item['result_hash']=fingerprint(item['result']);value.save(db,item)
    return value.snapshot('wTest',job)


def test_result_snapshot_survives_review_retry_and_immutable_media(service):
    value,create,_,_=service;job=create('sealSnapshot',device='cpu',kind='seal');terminal(value,job['job_id'])
    before=seal_result(value,job['job_id']);original=copy.deepcopy(before)
    with value.transaction() as db:
        item=value.row(db,'wTest',job['job_id'])
        output=value.root/job['job_id']/'attempt-1'/'page-1.png';output.write_bytes(b'original-page')
        item['result']['output_files']={'page-1.png':{'bytes':output.stat().st_size,'sha256':value.file_hash(output)}}
        item['result_hash']=fingerprint(item['result']);value.save(db,item)
    with_media=value.snapshot('wTest',job['job_id'])
    review={'request_id':'userSavedReview','expected_revision':1,'actor':'实际用户','note':'按原件核准',
            'assessment':'confirmed','edits':[{'index':0,'number':'000124'}]}
    saved=value.review('wTest',job['job_id'],review)
    assert value.review('wTest',job['job_id'],review)==saved
    assert value.lookup_operation('wTest','review','userSavedReview')['receipt']==saved
    after=value.snapshot('wTest',job['job_id'])
    assert after['snapshot_id']!=before['snapshot_id'] and after['review_revision']==2
    assert value.snapshot('wTest',job['job_id'],before['snapshot_id'])==original
    assert value.snapshot_at_revision('wTest',job['job_id'],2)==after
    assert value.snapshot_output('wTest',job['job_id'],with_media['snapshot_id'],'page-1.png')[0].read_bytes()==b'original-page'
    with value.transaction() as db:
        item=value.row(db,'wTest',job['job_id']);item['status']='failed';value.save(db,item)
    until(lambda:job['job_id'] not in value.inflight)
    retried=value.control('wTest',job['job_id'],'retry',{'request_id':'retryAfterReview','expected_revision':2,'expected_generation':1})
    assert retried['review'] is None and retried['result'] is None and retried['generation']==2
    terminal(value,job['job_id'])
    assert value.snapshot('wTest',job['job_id'],before['snapshot_id'])==original
    output.write_bytes(b'tampered-page')
    with pytest.raises(DataError,match='校验失败'):
        value.snapshot_output('wTest',job['job_id'],with_media['snapshot_id'],'page-1.png')


def test_draft_conversion_keeps_original_values_scope_and_never_forges_review(service,monkeypatch):
    value,create,_,_=service;job=create('draftSeal',device='cpu',kind='seal');terminal(value,job['job_id'])
    frozen=seal_result(value,job['job_id']);captured=[]
    @dataclass
    class Context:
        operation_id:str='callerOperation'
        session_id:str='ownedSession'
    def install(payload,name,media,sources,leaves,provenance,ctx):
        captured.append({'csv':payload,'provenance':provenance,'ctx':ctx,'sources':sources,'leaves':leaves})
        return {'data':{'asset_id':'aDraft','asset_revision':1,'blob_hash':'a'*64}}
    monkeypatch.setattr('workbench_platform.agent.tools.results.install',install)
    monkeypatch.setattr('workbench_platform.application_services.data.stage_asset_upload',lambda *_:{'id':'uDraft'})
    class Data:
        def create(self,*_,**kwargs):return {'id':'draftSession','version':1}
        def operation(self,*_):return None
        def submit(self,*_,**kwargs):return {'id':'draftImport','status':'queued'}
    value.app.state.data_workbench=Data()
    command={'request_id':'draftLostResponse','snapshot_id':frozen['snapshot_id'],'expected_revision':1}
    converted=value.draft_to_data('wTest',job['job_id'],command,ctx=Context())
    assert value.draft_to_data('wTest',job['job_id'],command,ctx=Context())==converted and len(captured)==1
    rows=list(csv.DictReader(io.StringIO(captured[0]['csv'].decode('utf-8-sig'))))
    assert rows[0]['候选编号']=='000123' and rows[0]['复核状态']=='未复核'
    assert captured[0]['provenance']['review_state']=='unreviewed'
    assert captured[0]['provenance']['original_values'][0]['confidence']==.73
    assert captured[0]['ctx'].session_id=='ownedSession'
    assert captured[0]['sources'][-1]['application']=='multimodal'
    assert captured[0]['sources'][-1]['coverage']['draft'] is True
    assert value.get('wTest',job['job_id'])['review'] is None
    assert value.lookup_operation('wTest','draft_to_data','draftLostResponse')['found']
    with pytest.raises(DataError):value.draft_to_data('wTest',job['job_id'],{**command,'expected_revision':2},ctx=Context())
    with pytest.raises(DataError,match='人工核准'):value.to_data('wTest',job['job_id'],ctx=Context(),snapshot_id=frozen['snapshot_id'])
    value.review('wTest',job['job_id'],{'request_id':'confirmedBridge','expected_revision':1,
        'actor':'实际用户','note':'已按当前原件核准','assessment':'confirmed','edits':[{'index':0,'number':'000124'}]})
    approved=value.snapshot('wTest',job['job_id'])
    formal=value.to_data('wTest',job['job_id'],ctx=Context(),snapshot_id=approved['snapshot_id'])
    assert formal['session_id']=='draftSession'
    assert captured[-1]['provenance']['draft'] is False
    assert captured[-1]['sources'][-1]['coverage']['draft'] is False
    formal_rows=list(csv.DictReader(io.StringIO(captured[-1]['csv'].decode('utf-8-sig'))))
    assert formal_rows[0]['编号']=='000124' and formal_rows[0]['复核状态']=='已复核'


def test_transfer_workspaces_are_task_owned_and_legacy_receipts_resume_only_their_owner(service):
    value,_,_,_=service
    from unittest.mock import Mock
    legacy='d'+fingerprint(['wTest','mSeal','cmpFixed','draft'])[:40]
    native=Mock()
    native.operation.return_value={'id':'oldImport','session_id':'legacyData'}
    value.app.state.data_workbench=native
    receipt={'status':'pending','data':{'data_session_id':'legacyData'},'result_refs':[
        {'application':'multimodal','parent_id':'mSeal','object_id':'cmpFixed'}]}
    class Store:
        def rows(self,sql,params):
            return [{'result':encoded(receipt)}] if params==('ownerTask',) else []
    owner=SimpleNamespace(session_id='ownerTask',store=Store())
    other=SimpleNamespace(session_id='anotherTask',store=Store())
    assert value._transfer_key('wTest','mSeal','cmpFixed','draft',owner)==legacy
    other_key=value._transfer_key('wTest','mSeal','cmpFixed','draft',other)
    assert other_key!=legacy
    assert value._transfer_key('wTest','mSeal','cmpFixed','draft',other)==other_key
    # A receipt referring to a different snapshot cannot transfer ownership.
    receipt['result_refs'][0]['object_id']='differentSnapshot'
    assert value._transfer_key('wTest','mSeal','cmpFixed','draft',owner) not in {legacy,other_key}
    assert value._transfer_key('wTest','mSeal','cmpFixed','draft',None)==legacy


def test_fixed_draft_collection_is_replayable_and_formal_collection_requires_confirmation(service,tmp_path):
    import sqlite3
    from workbench_platform.artifacts.store import ArtifactStore
    value,create,_,_=service;job=create('draftCollection',device='cpu',kind='seal');terminal(value,job['job_id'])
    frozen=seal_result(value,job['job_id'])
    with pytest.raises(DataError,match='人工核准'):value.collect('wTest',job['job_id'])
    directory=tmp_path/'collected';directory.mkdir();blobs=directory/'blobs';blobs.mkdir()
    store=ArtifactStore(directory/'artifacts.sqlite3',blobs)
    def connect():
        return sqlite3.connect(directory/'artifacts.sqlite3',isolation_level=None)
    with closing(connect()) as db:
        db.execute('CREATE TABLE manual_artifact_bundle(bundle_id TEXT PRIMARY KEY,workspace_id TEXT,created_at TEXT,status TEXT,body TEXT)')
    value.app.state.artifact_center=SimpleNamespace(store=store,_connect=connect)
    collected=value.collect_snapshot('wTest',job['job_id'],frozen['snapshot_id'])
    assert collected['draft'] is True and collected['review_revision']==0 and collected['snapshot_hash']==frozen['snapshot_hash']
    assert {row['name'] for row in collected['files']}=={'draft.csv','result.json','manifest.json'}
    assert value.collect_snapshot('wTest',job['job_id'],frozen['snapshot_id'])==collected
    for row in collected['files']:
        artifact=store.get('wTest',row['artifact_id']);assert artifact.blob_hash==row['blob_hash']


def test_recovery_requires_exact_generation_epoch_and_dead_owned_tree(service,monkeypatch):
    value,create,calls,outcomes=service;outcomes.append('needs_verification')
    job=create('unknownCleanup',device='cpu');terminal(value,job['job_id'],'needs_verification')
    until(lambda:job['job_id'] not in value.inflight)
    command={'request_id':'recoverOriginal','expected_revision':1,'expected_generation':1}
    assert value.recover('wTest',job['job_id'],command)['status']=='needs_verification'
    item=raw(value,job['job_id']);owner=Owner('manual','testLaunch','testInstance',12345,'123456789012345678')
    record={'format':'clear-gpu-owner-v1','owner':asdict(owner),'job_name':'Local\\ClearGpu-Test',
        'kill_on_close':True,'breakaway':False,'job_id':job['job_id'],'generation':1,
        'worker_epoch':'wrongEpoch','terminal_status':'failed'}
    path=value.root/job['job_id']/'attempt-1'/'worker-ownership.json';path.write_text(encoded(record),'utf-8')
    monkeypatch.setattr('workbench_platform.resources.recovery.recovery_evidence',lambda *_:ResourceEvidence(True,False,False,True))
    assert value.recover('wTest',job['job_id'],command)['status']=='needs_verification'
    record['worker_epoch']=item['worker_epoch'];path.write_text(encoded(record),'utf-8')
    monkeypatch.setattr('workbench_platform.resources.recovery.recovery_evidence',lambda *_:ResourceEvidence(True,True,True,False))
    assert value.recover('wTest',job['job_id'],command)['status']=='needs_verification'
    monkeypatch.setattr('workbench_platform.resources.recovery.recovery_evidence',lambda *_:ResourceEvidence(True,False,False,True))
    recovered=value.recover('wTest',job['job_id'],command)
    assert recovered['job_id']==job['job_id'] and recovered['generation']==2
    terminal(value,job['job_id']);assert len(calls)==2
    assert value.recover('wTest',job['job_id'],command)==recovered


def test_native_http_snapshot_lookup_and_bodyless_control_compatibility(service,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from packages.contracts.multimodal import ManualResultSnapshot,ManualOperationReceipt
    from workbench_platform.multimodal import routes
    value,create,_,_=service
    monkeypatch.setattr(routes,'service',lambda *_args,**_kwargs:value)
    application=FastAPI();application.include_router(routes.router)
    job=create('httpSnapshot',device='cpu',kind='seal');terminal(value,job['job_id'])
    seal_result(value,job['job_id'])
    base='/api/platform/v1/workspaces/wTest/multimodal'
    with TestClient(application) as client:
        response=client.get(base+'/jobs/'+job['job_id']+'/snapshot');assert response.status_code==200,response.text
        frozen=ManualResultSnapshot.model_validate(response.json())
        pinned=base+'/jobs/'+job['job_id']+'/snapshots/'+frozen.snapshot_id
        assert client.get(pinned).json()==response.json()
        download=client.get(pinned+'/download');assert download.status_code==200,download.text
        import zipfile
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert 'draft.csv' in archive.namelist() and 'reviewed.csv' not in archive.namelist()
            assert '未复核' in archive.read('draft.csv').decode('utf-8-sig')
        assert client.get(pinned+'/files/result.json').status_code==200
        assert '未复核' in client.get(pinned+'/files/draft.csv').content.decode('utf-8-sig')
        draft_path=value.snapshot_output('wTest',job['job_id'],frozen.snapshot_id,'draft.csv')[0]
        draft_path.write_bytes(b'changed-export')
        with pytest.raises(DataError,match='固定导出文件校验失败'):
            value.snapshot_output('wTest',job['job_id'],frozen.snapshot_id,'draft.csv')
        lookup=client.get(base+'/operations/create/httpSnapshot')
        assert ManualOperationReceipt.model_validate(lookup.json()).found
        value.coordinator.request('httpBlocked','ocr');waiting=create('httpCancel')
        response=client.post(base+'/jobs/'+waiting['job_id']+'/control/cancel')
        assert response.status_code==200,response.text
        assert response.json()['status']=='cancelled'


def test_data_local_cpu_receipt_requires_success_and_exact_program_resource_identity(service):
    value,_,_,_=service
    value.models.mkdir(parents=True,exist_ok=True)
    resources={}
    for filename in ('face/yunet.onnx','face/sface.onnx'):
        path=value.models/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'synthetic-model-identity')
        resources[filename]={'bytes':path.stat().st_size,'sha256':value.file_hash(path)}
    runtime=value.program/'runtimes/forensics/python.exe';runtime.parent.mkdir(parents=True,exist_ok=True);runtime.write_bytes(b'synthetic-runtime-identity')
    bundled=value.models/'validation.json';bundled.write_text(encoded({'face':{'resources':resources,'cpu':False}}),'utf-8')
    local=value.root.parent/'multimodal-cpu-validation.json'
    record={'face':{'cpu':True,'gpu':False,'resources':resources,
        'adapter_sha256':value.file_hash(SOURCE/'modules/manual_multimodal/worker.py'),
        'source_hashes':{path.relative_to(SOURCE).as_posix():value.file_hash(path) for path in sorted((SOURCE/'modules/manual_multimodal').glob('*.py'))},
        'runtime_sha256':value.file_hash(runtime),'record':{'actual_device':'cpu','cleanup_confirmed':True,'outcome':{'status':'succeeded'},'exit_code':0}}}
    def face():return next(item for item in NATIVE_CAPABILITIES(value)['items'] if item['kind']=='face')
    assert face()['cpu_ready'] is False
    local.write_text(encoded(record),'utf-8');assert face()['cpu_ready'] is True and face()['validation_source']=='data_local'
    record['face']['record']['cleanup_confirmed']=False;local.write_text(encoded(record),'utf-8');assert face()['cpu_ready'] is False
    record['face']['record']['cleanup_confirmed']=True
    record['face']['source_hashes']['modules/manual_multimodal/worker.py']='0'*64
    local.write_text(encoded(record),'utf-8');assert face()['cpu_ready'] is False
    local.write_text('{broken','utf-8');assert face()['cpu_ready'] is False and face()['cpu_reason']
    assert json.loads(bundled.read_text('utf-8'))=={'face':{'resources':resources,'cpu':False}}


def test_first_cpu_analysis_prepares_once_and_optional_diagnostic_stays_compatible(service,monkeypatch):
    value,_,calls,_=service;ready=[False];prepared=[]
    monkeypatch.setattr(MultimodalService,'capabilities',lambda _self:{'items':[
        {'kind':kind,'runtime':__import__('sys').executable,'cpu_ready':ready[0],'gpu_ready':False,'missing':[]}
        for kind in ('face','audio','seal','catnet')]})
    def committed(item,output):
        assert item['purpose'] in {'analysis','preparation'} and item['actual_device']=='cpu'
        prepared.append(item['job_id']);ready[0]=True
    monkeypatch.setattr(value,'_record_prepared',committed)
    monkeypatch.setattr(value,'_validate_preparation',lambda _item:None)
    source=value.registry.get('wTest','aTest')
    command={'request_id':'prepareIdentity','inputs':[{'asset_id':'aTest','asset_revision':1,'blob_hash':source.blob_hash}],
             'options':{'device':'cpu','action':'detect'}}
    first=value.create('wTest',{**command,'kind':'face','request_id':'firstBusiness'})
    final=terminal(value,first['job_id'])
    assert final['purpose']=='analysis' and prepared==[first['job_id']] and len(calls)==1
    assert value.create('wTest',{**command,'kind':'face','request_id':'firstBusiness'})['job_id']==first['job_id']
    submitted=value.prepare('wTest','face',command)
    assert submitted['purpose']=='preparation' and submitted['status']=='queued'
    final=terminal(value,submitted['job_id'])
    assert final['purpose']=='preparation' and prepared==[first['job_id'],submitted['job_id']]
    assert value.prepare('wTest','face',command)['job_id']==submitted['job_id'] and len(calls)==2
    assert value.lookup_operation('wTest','prepare','prepareIdentity')['resource_id']==submitted['job_id']
    assert value.lookup_operation('wTest','create','prepareIdentity')['found'] is False
    business=value.create('wTest',{**command,'request_id':'readyBusiness','kind':'face'})
    terminal(value,business['job_id']);assert len(calls)==3
    with pytest.raises(DataError):value.prepare('wTest','face',{**command,'request_id':'gpuNotQualification','options':{'device':'cuda'}})


def test_legacy_review_snapshot_recovers_from_original_receipt_without_altering_failed_job(service):
    value,create,_,_=service;job=create('legacyReview',device='cpu');terminal(value,job['job_id'])
    reviewed=value.review('wTest',job['job_id'],{'request_id':'legacySavedReview','expected_revision':1,
        'actor':'原用户','assessment':'confirmed','note':'原版本复核回执'})
    with value.transaction() as db:
        db.execute("DELETE FROM manual_result_snapshot WHERE job_id=? AND json_extract(body,'$.revision')=2",(job['job_id'],))
        current=value.row(db,'wTest',job['job_id']);current['status']='failed';value.save(db,current)
    recovered=value.snapshot_at_revision('wTest',job['job_id'],2)
    assert recovered['analysis']['review']==reviewed['review'] and recovered['revision']==2
    assert value.get('wTest',job['job_id'])['status']=='failed'
    with value.transaction() as db:
        db.execute('DELETE FROM manual_result_snapshot WHERE snapshot_id=?',(recovered['snapshot_id'],))
    path=value.root/job['job_id']/'attempt-1'/'result.json'
    result=json.loads(path.read_text('utf-8'));result['outcome']='changed-later';path.write_text(encoded(result),'utf-8')
    with pytest.raises(DataError,match='固定复核回执不一致'):
        value.snapshot_at_revision('wTest',job['job_id'],2)


def test_never_started_queue_and_legacy_receipts_survive_reopen_without_guessing_unknown(service):
    value,create,calls,_=service
    value.coordinator.request('restartQueueBlock','ocr')
    job=create('neverStartedQueue');until(lambda:raw(value,job['job_id']).get('gpu_request_id'))
    unknown=create('legacyUnknownWithoutReceipt')
    with value.transaction() as db:
        item=value.row(db,'wTest',job['job_id']);item.update(status='needs_verification',error='old close')
        item.pop('worker_epoch',None);item.pop('recovery_intent',None);value.save(db,item)
        other=value.row(db,'wTest',unknown['job_id']);other.update(status='needs_verification',error='genuine unknown')
        other.pop('worker_epoch',None);other.pop('recovery_intent',None);value.save(db,other)
        db.execute('DELETE FROM manual_operation WHERE resource_id=?',(unknown['job_id'],))
    value.close()
    reopened=MultimodalService(value.app,value.root);reopened.runner=value.runner
    try:
        queued=reopened.get('wTest',job['job_id'])
        assert queued['status']=='queued' and queued['generation']==1
        blocked=reopened.get('wTest',unknown['job_id'])
        assert blocked['status']=='needs_verification' and blocked['error']=='genuine unknown'
        value.coordinator.cancel_pending('restartQueueBlock','ocr');reopened.wake.set()
        terminal(reopened,job['job_id']);assert len(calls)==1
    finally:reopened.close()


def test_recovered_success_requires_native_output_hashes_not_only_result_json(service,monkeypatch):
    value,create,calls,_=service;job=create('lostSuccessReceipt',device='cpu');terminal(value,job['job_id'])
    until(lambda:job['job_id'] not in value.inflight)
    original=raw(value,job['job_id']);output=value.root/job['job_id']/'attempt-1'
    image=output/'preview.png';image.write_bytes(b'owned-original-preview')
    owner=Owner('manual','testLaunch','testInstance',12345,'123456789012345678')
    record={'format':'clear-gpu-owner-v1','owner':asdict(owner),'job_name':'Local\\ClearGpu-Test',
        'kill_on_close':True,'breakaway':False,'job_id':job['job_id'],'generation':1,
        'worker_epoch':original['worker_epoch'],'terminal_status':'succeeded',
        'result_file_hash':value.file_hash(output/'result.json'),
        'output_files':{'preview.png':{'bytes':image.stat().st_size,'sha256':value.file_hash(image)}}}
    (output/'worker-ownership.json').write_text(encoded(record),'utf-8')
    with value.transaction() as db:
        item=value.row(db,'wTest',job['job_id']);item.update(status='needs_verification',recovery_intent='running',result=None,result_hash=None)
        value.save(db,item);db.execute('DELETE FROM manual_result_snapshot WHERE job_id=?',(job['job_id'],))
    monkeypatch.setattr('workbench_platform.resources.recovery.recovery_evidence',lambda *_:ResourceEvidence(True,False,False,True))
    command={'request_id':'recoverSameCompletedJob','expected_revision':1,'expected_generation':1}
    image.write_bytes(b'changed-image')
    assert value.recover('wTest',job['job_id'],command)['status']=='needs_verification'
    image.write_bytes(b'owned-original-preview')
    recovered=value.recover('wTest',job['job_id'],command)
    assert recovered['status']=='succeeded' and recovered['generation']==1 and len(calls)==1


def test_preparation_cannot_execute_a_model_without_sealed_resource_identity(service):
    value,_,calls,_=service
    source=value.registry.get('wTest','aTest')
    submitted=value.prepare('wTest','face',{'request_id':'missingInventory',
        'inputs':[{'asset_id':'aTest','asset_revision':1,'blob_hash':source.blob_hash}]})
    failed=terminal(value,submitted['job_id'],'failed')
    assert '模型资源清单' in failed['error'] and calls==[]


def test_first_cpu_result_survives_readiness_registry_write_failure(service,monkeypatch):
    value,_,calls,_=service
    monkeypatch.setattr(MultimodalService,'capabilities',lambda _self:{'items':[
        {'kind':kind,'runtime':__import__('sys').executable,'cpu_ready':False,'gpu_ready':False,'missing':[]}
        for kind in ('face','audio','seal','catnet')]})
    monkeypatch.setattr(value,'_validate_preparation',lambda _item:None)
    from workbench_platform.multimodal.cpu_validation import ReadinessRecordError
    def failed_registry(_item,_output):raise ReadinessRecordError('readiness registry unavailable')
    monkeypatch.setattr(value,'_record_prepared',failed_registry)
    source=value.registry.get('wTest','aTest')
    command={'request_id':'cpuRegistryFailure','kind':'face',
        'inputs':[{'asset_id':'aTest','asset_revision':1,'blob_hash':source.blob_hash}],
        'options':{'device':'cpu','action':'detect'}}
    job=value.create('wTest',command)
    final=terminal(value,job['job_id'])
    assert final['status']=='succeeded' and raw(value,job['job_id'])['preparation_pending'] is True
    assert value.snapshot('wTest',job['job_id'])['analysis']['result']
    assert value.create('wTest',command)['job_id']==job['job_id'] and len(calls)==1

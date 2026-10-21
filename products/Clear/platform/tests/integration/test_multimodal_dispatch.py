"""Dispatcher/lifecycle tests with an explicit synthetic inference runner."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace

import pytest

from workbench_platform.multimodal.runner import Outcome
from workbench_platform.multimodal.service import MultimodalService
from workbench_platform.resources.gpu import Owner, ResourceEvidence
from workbench_platform.resources.recovery import process_identity


def until(function, timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=function()
        if value:return value
        time.sleep(.02)
    raise AssertionError('synthetic dispatcher did not reach expected state')


def raw(service,job):
    from contextlib import closing
    with closing(service.db()) as db:return service.row(db,'wTest',job)


@pytest.fixture
def service(tmp_path,monkeypatch):
    from workbench_platform.multimodal import artifacts
    monkeypatch.setattr(artifacts,'configure',lambda center:None)
    monkeypatch.delenv('WORKBENCH_GPU_LEDGER',raising=False)
    monkeypatch.setenv('WORKBENCH_DEEP_FORENSICS_BACKEND','catnet')
    monkeypatch.setenv('WORKBENCH_PROGRAM_ROOT',str(tmp_path/'program'))
    legacy=tmp_path/'program/models/multimodal/catnet/CAT_full_v2.pth.tar'
    legacy.parent.mkdir(parents=True);legacy.write_bytes(b'explicit synthetic model identity; no inference')
    # This fixture never decodes audio or loads a model. Give its synthetic
    # resource ledger virtual capacity; dedicated budget tests override this
    # probe and real native-service acceptance retains the actual disk guard.
    usage=shutil.disk_usage(tmp_path)
    monkeypatch.setattr('workbench_platform.multimodal.service.shutil.disk_usage',
        lambda _path:SimpleNamespace(total=usage.total,used=usage.used,free=8*1024**3))
    raw=tmp_path/'input.jpg';raw.write_bytes(b'synthetic-image-no-model')
    blob_hash=hashlib.sha256(raw.read_bytes()).hexdigest()
    source_name=['input.jpg']
    class Registry:
        def get(self,workspace,asset):
            return SimpleNamespace(revision=1,blob_hash=blob_hash,original_name=source_name[0])
        @contextmanager
        def stage_verified(self,*args,**kwargs):
            yield raw
    monkeypatch.setattr(MultimodalService,'capabilities',lambda self,**kwargs:{'items':[
        {'kind':kind,'runtime':sys.executable,'cpu_ready':True,'gpu_ready':True,'missing':[]}
        for kind in ('catnet','face','audio','seal')]})
    app=SimpleNamespace(state=SimpleNamespace(asset_library=[Registry()],artifact_center=None))
    value=MultimodalService(app,tmp_path/'data'/'manual-analysis')
    value.config['dispatch_poll_seconds']=.02
    calls=[];outcomes=[]
    class FakeRunner:
        def run(self,command,output,env,request,request_id=None,**callbacks):
            calls.append(request)
            lease=None
            if request['options']['device']=='cuda':
                owner=Owner('manual',request['runner']['launch_id'],request['runner']['instance_id'],
                    os.getpid(),process_identity(os.getpid()))
                lease=value.coordinator.grant(request_id,owner,owned=lambda _:True)
            outcome=outcomes.pop(0) if outcomes else 'succeeded'
            if outcome=='yielded':
                value.coordinator.request('externalAfterChunk','ocr')
                callbacks['committed']({'chunk_index':0,'processed_ms':300000,'total_ms':600000})
            if outcome=='succeeded':
                result={'device':request['options']['device'],'outcome':'mock','segments':[]}
                if request.get('model_binding'):result['model_binding']=request['model_binding']
                if request['kind']=='audio':
                    result.update(audio={'duration_ms':14400000},performance={'total_chunks':48})
                (output/'result.json').write_text(json.dumps(result))
            if lease:value.coordinator.release(lease,witness=lambda _:ResourceEvidence(True,True,False,True))
            return Outcome(outcome,error='synthetic OOM' if outcome=='oom' else None)
    value.runner=FakeRunner()
    def create(request_id,device='cuda',kind='catnet'):
        source_name[0]='input.wav' if kind=='audio' else 'input.jpg'
        return value.create('wTest',{'request_id':request_id,'kind':kind,
            'inputs':[{'asset_id':'aTest','asset_revision':1,'blob_hash':blob_hash}],
            'options':{'device':device}})
    yield value,create,calls,outcomes
    value.close()


def test_gpu_waiting_leaves_cpu_lane_free(service):
    value,create,calls,_=service
    value.coordinator.request('externalFirst','ocr')
    gpu=create('gpuWaiting')
    cpu=create('cpuIndependent',device='cpu')
    until(lambda:value.get('wTest',cpu['job_id'])['status']=='succeeded')
    waiting=until(lambda:(item if item.get('progress',{}).get('queue_position') else None)
        if (item:=value.get('wTest',gpu['job_id']))['status']=='queued' else None)
    assert waiting['progress']['queue_position']==2
    assert [call['options']['device'] for call in calls]==['cpu']
    value.coordinator.cancel_pending('externalFirst','ocr')
    value.wake.set()
    until(lambda:value.get('wTest',gpu['job_id'])['status']=='succeeded')


def synthetic_safire(value,monkeypatch):
    """Only lifecycle identity is synthetic; this is not model qualification."""
    from workbench_platform.multimodal import deep_backend
    original=deep_backend.bind
    def binding(service,engine=None):
        engine=engine or deep_backend.backend(service.config)
        if engine=='catnet':return original(service,'catnet')
        return {'engine':'safire','version':'synthetic-lifecycle-only','asset_root':str(value.root),
                'resources':{},'runtime':sys.executable,'calibration':None}
    monkeypatch.setattr(deep_backend,'bind',binding)
    monkeypatch.setattr(MultimodalService,'safire_capability',lambda self,kind,*args:{
        'kind':kind,'runtime':sys.executable,'cpu_ready':False,'gpu_ready':True,'missing':[]})
    monkeypatch.setenv('WORKBENCH_DEEP_FORENSICS_BACKEND','safire')


def test_safire_default_and_idempotency_remain_bound_across_rollback(service,monkeypatch):
    value,create,calls,_=service
    synthetic_safire(value,monkeypatch)
    value.coordinator.request('holdSafire','ocr')
    explicit=create('safireExplicit')
    body={'request_id':'safireDefault','kind':'catnet','inputs':[
        {key:row[key] for key in ('asset_id','asset_revision','blob_hash')} for row in explicit['inputs']]}
    created=value.create('wTest',body)
    assert created['options']['device']=='cuda' and created['model_binding']['engine']=='safire'
    value.control('wTest',explicit['job_id'],'cancel')
    monkeypatch.setenv('WORKBENCH_DEEP_FORENSICS_BACKEND','catnet')
    replay=value.create('wTest',body)
    assert replay['job_id']==created['job_id'] and replay['model_binding']==created['model_binding']
    value.coordinator.cancel_pending('holdSafire','ocr');value.wake.set()
    until(lambda:value.get('wTest',created['job_id'])['status']=='succeeded')
    assert calls[-1]['model_binding']['engine']=='safire'


def test_safire_oom_has_one_prompt_batch_retry_and_no_automatic_cpu(service,monkeypatch):
    value,create,calls,outcomes=service
    synthetic_safire(value,monkeypatch)
    outcomes.extend(['oom','oom'])
    item=create('safireOom')
    until(lambda:value.get('wTest',item['job_id'])['status']=='failed')
    assert [call['runner']['safire_points_per_batch'] for call in calls]==[64,32]
    assert all(call['options']['device']=='cuda' for call in calls)
    with pytest.raises(Exception,match='推理验证'):
        value.control('wTest',item['job_id'],'retry_cpu')


def test_yield_uses_new_fifo_request_without_new_business_generation(service):
    value,create,calls,outcomes=service
    outcomes.append('yielded')
    created=create('longAudioYield',kind='audio')
    waiting=until(lambda:(item if item['status']=='queued' and item.get('progress',{}).get('queue_position')==2 else None)
        if (item:=value.get('wTest',created['job_id'])) else None)
    assert waiting['generation']==1
    assert waiting['progress']['processed_ms']==300000
    assert value.coordinator.snapshot().pending[0]=='externalAfterChunk'
    value.coordinator.cancel_pending('externalAfterChunk','ocr');value.wake.set()
    until(lambda:value.get('wTest',created['job_id'])['status']=='succeeded')
    assert len(calls)==2 and calls[0]['runner']['worker_epoch']!=calls[1]['runner']['worker_epoch']
    assert calls[0]['runner']['checkpoint_root']==calls[1]['runner']['checkpoint_root']
    assert calls[0]['runner']['generation']==calls[1]['runner']['generation']==1


def test_oom_has_exactly_one_reduced_retry_then_explicit_cpu_action(service):
    value,create,calls,outcomes=service
    outcomes.extend(['oom','oom'])
    created=create('oomRetry')
    failed=until(lambda:(item if item['status']=='failed' else None)
        if (item:=value.get('wTest',created['job_id'])) else None)
    assert raw(value,created['job_id'])['oom_retries']==1 and len(calls)==2
    assert [call['runner']['catnet_tile_size'] for call in calls]==[1024,512]
    assert value.coordinator.snapshot().owner is None
    value.control('wTest',created['job_id'],'retry_cpu')
    final=until(lambda:(item if item['status']=='succeeded' else None)
        if (item:=value.get('wTest',created['job_id'])) else None)
    assert final['actual_device']=='cpu' and final['generation']==2
    assert final['progress']['explicit_cpu_retry'] is True
    assert calls[-1]['options']['device']=='cpu'


def test_cancel_waiter_removes_its_request_and_never_starts_worker(service):
    value,create,calls,_=service
    value.coordinator.request('externalBlocking','classification')
    created=create('cancelWaiting')
    until(lambda:raw(value,created['job_id']).get('gpu_request_id'))
    value.control('wTest',created['job_id'],'cancel')
    assert value.get('wTest',created['job_id'])['status']=='cancelled'
    assert value.coordinator.snapshot().pending==('externalBlocking',)
    assert calls==[]


def test_cleanup_unknown_is_not_retried_automatically(service):
    value,create,calls,outcomes=service
    outcomes.append('needs_verification')
    created=create('cleanupUnconfirmed')
    until(lambda:value.get('wTest',created['job_id'])['status']=='needs_verification')
    time.sleep(.1)
    assert len(calls)==1


def test_public_job_has_only_strict_contract_fields(service):
    from packages.contracts.multimodal import ManualAnalysis
    value,create,_,_=service
    created=create('strictPublic',device='cpu')
    final=until(lambda:(item if item['status']=='succeeded' else None)
        if (item:=value.get('wTest',created['job_id'])) else None)
    ManualAnalysis.model_validate(final)
    assert not {'worker_epoch','gpu_request_id','oom_retries','active_seconds'}&final.keys()


@pytest.mark.parametrize('device',['cpu','cuda'])
def test_completed_audio_retains_worker_total_chunks(service,device):
    value,create,_,_=service
    created=create('finishedAudio'+device,device=device,kind='audio')
    final=until(lambda:(item if item['status']=='succeeded' else None)
        if (item:=value.get('wTest',created['job_id'])) else None)
    assert final['result']['performance']['total_chunks']==48
    assert final['progress']['completed_chunks']==final['progress']['total_chunks']==48
    assert final['progress']['processed_ms']==final['progress']['duration_ms']==14400000


def test_audio_disk_budget_reserves_queued_cpu_and_gpu_together(service,monkeypatch):
    from workbench_platform.multimodal.service import AUDIO_DISK_BUDGET,DISK_MARGIN
    from workbench_platform.multimodal import service as module
    from workbench_platform.data_api.service import DataError
    value,create,_,_=service
    value.coordinator.request('blockedGpu','ocr')
    monkeypatch.setattr(module.shutil,'disk_usage',lambda path:SimpleNamespace(free=AUDIO_DISK_BUDGET+DISK_MARGIN+1))
    created=create('reservedGpuAudio',kind='audio')
    assert value.get('wTest',created['job_id'])['status']=='queued'
    with pytest.raises(DataError,match='空间不足'):create('cpuAudioBudget',device='cpu',kind='audio')


def test_input_retry_uses_verified_hardlink_and_preserves_source(service,tmp_path,monkeypatch):
    from contextlib import closing
    from workbench_platform.multimodal import service as module
    value,_,_,_=service
    origin=tmp_path/'verified.bin';origin.write_bytes(b'original')
    destination=tmp_path/'attempt-input.bin'
    monkeypatch.setattr(module.shutil,'copyfileobj',lambda *args,**kwargs:pytest.fail('hardlink should avoid copying'))
    digest=hashlib.sha256(origin.read_bytes()).hexdigest()
    with closing(value.db()) as db:
        value._stage_input(db,origin,destination,digest)
        value._stage_input(db,origin,destination,digest)
    assert os.stat(origin).st_ino==os.stat(destination).st_ino
    assert origin.read_bytes()==b'original'


def test_copy_fallback_checks_disk_before_writing(service,tmp_path,monkeypatch):
    from contextlib import closing
    from workbench_platform.multimodal import service as module
    from workbench_platform.data_api.service import DataError
    value,_,_,_=service
    origin=tmp_path/'verified.bin';origin.write_bytes(b'original')
    destination=tmp_path/'copy-input.bin'
    def no_link(*args):raise OSError('synthetic filesystem has no hardlinks')
    monkeypatch.setattr(module.os,'link',no_link)
    monkeypatch.setattr(module.shutil,'disk_usage',lambda path:SimpleNamespace(free=0))
    with closing(value.db()) as db:
        with pytest.raises(DataError,match='空间不足'):
            value._stage_input(db,origin,destination,hashlib.sha256(origin.read_bytes()).hexdigest())
    assert not destination.exists() and origin.read_bytes()==b'original'

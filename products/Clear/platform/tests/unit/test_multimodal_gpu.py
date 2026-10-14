"""Real Windows process ownership with synthetic workers; no CUDA/model imports."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from workbench_platform.multimodal.runner import WorkerRunner, PROTOCOL, read_event, worker_environment
from workbench_platform.multimodal.qualification import Qualification
from workbench_platform.resources.gpu import GpuCoordinator, Owner, ResourceBusy, ReleaseUnconfirmed, ResourceEvidence
from workbench_platform.resources.recovery import recover_owned, recovery_evidence, atomic_json, process_identity


FAKE_WORKER = '''import hashlib,json,pathlib,subprocess,sys,time
request=json.loads(pathlib.Path(sys.argv[1]).read_text())
out=pathlib.Path(sys.argv[1]).parent
epoch=request['runner']['worker_epoch']
start=json.loads(sys.stdin.readline())
assert start=={'command':'start','worker_epoch':epoch}
(out/'started').write_text('gated')
mode=request.get('mode','success')
if mode=='oom':
    print('CUDA out of memory',flush=True)
    sys.exit(9)
if mode=='descendant':
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])
    (out/'child-pid').write_text(str(child.pid))
if mode=='wait':
    time.sleep(120)
if mode=='progress':
    for index in range(8):
        (out/'progress.new').write_text(json.dumps({'worker_epoch':epoch,'stage':'decoding','processed_ms':index+1}))
        for attempt in range(20):
            try:
                (out/'progress.new').replace(out/'progress.json')
                break
            except PermissionError:time.sleep(.01)
        time.sleep(.3)
if mode=='chunk':
    for index in range(2):
        checkpoint=out/('chunk-%d.json'%index)
        checkpoint.write_text(json.dumps({'chunk':index}))
        event={'event':'chunk_committed','worker_epoch':epoch,'chunk_index':index,
            'checkpoint':{'path':str(checkpoint),'sha256':hashlib.sha256(checkpoint.read_bytes()).hexdigest()},
            'processed_ms':(index+1)*300000,'total_ms':600000}
        temp=out/'runner-event.new'
        temp.write_text(json.dumps(event))
        temp.replace(out/'runner-event.json')
        command=json.loads(sys.stdin.readline())
        assert command['worker_epoch']==epoch
        if command['command']=='stop':sys.exit(75)
        assert command['command']=='continue'
(out/'result.json').write_text(json.dumps({'device':request['options']['device'],'outcome':'mock'}))
'''


@pytest.fixture
def worker(tmp_path):
    assert tmp_path.drive.upper() == 'F:'
    script=tmp_path/'fake_worker.py'
    script.write_text(FAKE_WORKER,encoding='utf-8')
    coordinator=GpuCoordinator(tmp_path/'gpu.sqlite3')
    def execute(mode='success',gpu=True,**options):
        output=tmp_path/('run-'+str(time.monotonic_ns()));output.mkdir()
        request={'mode':mode,'options':{'device':'cuda' if gpu else 'cpu'},'runner':{'protocol':PROTOCOL,
            'worker_epoch':'epoch1','launch_id':'manualLaunch','instance_id':'manualInstance','checkpoint_root':str(output)}}
        path=output/'request.json';path.write_text(json.dumps(request),encoding='utf-8')
        request_id='manual-'+str(time.monotonic_ns()) if gpu else None
        if gpu:coordinator.request(request_id,'manual')
        runner=WorkerRunner(coordinator,tmp_path/'owners',poll_seconds=.01)
        result=runner.run([sys.executable,str(script),str(path)],output,
            worker_environment(output,sys.executable,gpu=gpu),request,request_id=request_id,**options)
        return result,output,request_id
    return coordinator,execute


def test_gate_and_shared_reservation_release(worker):
    coordinator,execute=worker
    outcome,output,request_id=execute()
    assert outcome.status=='succeeded'
    assert (output/'started').read_text()=='gated'
    commands=[json.loads(line) for line in (output/'commands.jsonl').read_text().splitlines()]
    assert commands[0]['phase']=='before_write' and commands[1]['phase']=='flushed'
    assert bytes.fromhex(commands[0]['frame_hex']).endswith(b'\n')
    assert len(commands[0]['runner_sha256'])==64
    assert coordinator.snapshot().owner is None
    record=json.loads((output.parent/'owners'/f'{request_id}.json').read_text())
    assert record['kill_on_close'] and not record['breakaway']
    with pytest.raises(ValueError):coordinator.request(request_id,'manual')


def test_busy_worker_cannot_import_or_run_before_grant(worker):
    coordinator,execute=worker
    coordinator.request('ocrBusy','ocr')
    owner=Owner('ocr','launchOcr','instanceOcr',os.getpid(),process_identity(os.getpid()))
    lease=coordinator.grant('ocrBusy',owner,owned=lambda _:True)
    outcome,output,request_id=execute()
    assert outcome.status=='waiting'
    assert not (output/'started').exists()
    assert coordinator.snapshot().owner==owner
    coordinator.cancel_pending(request_id,'manual')
    coordinator.release(lease,witness=lambda _:ResourceEvidence(True,True,False,True))


def test_audio_yields_after_committed_chunk_and_requeues_at_tail(worker):
    coordinator,execute=worker
    receipts=[]
    def committed(event):
        receipts.append(event)
        coordinator.request('classificationWaiting','classification')
    outcome,output,old_request=execute('chunk',committed=committed)
    assert outcome.status=='yielded'
    assert len(receipts)==1 and (output/'chunk-0.json').is_file()
    assert not (output/'chunk-1.json').exists()
    coordinator.request('manualResume','manual')
    assert coordinator.snapshot().pending==('classificationWaiting','manualResume')
    assert coordinator.snapshot().owner is None
    with pytest.raises(ValueError):coordinator.request(old_request,'manual')


def test_audio_keeps_hot_worker_when_no_one_waits(worker):
    _,execute=worker
    receipts=[]
    outcome,_,_=execute('chunk',committed=receipts.append)
    assert outcome.status=='succeeded'
    assert [value['chunk_index'] for value in receipts]==[0,1]


def test_cancel_on_commit_does_not_start_the_next_chunk(worker):
    _,execute=worker
    cancelled=threading.Event()
    outcome,output,_=execute('chunk',committed=lambda _:cancelled.set(),cancelled=cancelled.is_set)
    assert outcome.status=='cancelled'
    assert (output/'chunk-0.json').exists() and not (output/'chunk-1.json').exists()


def test_cpu_does_not_join_gpu_queue(worker):
    coordinator,execute=worker
    coordinator.request('unrelated','ocr')
    outcome,_,_=execute(gpu=False)
    assert outcome.status=='succeeded'
    assert coordinator.snapshot().pending==('unrelated',)


def test_oom_cleans_owned_process_before_reporting_retry(worker):
    coordinator,execute=worker
    outcome,_,_=execute('oom')
    assert outcome.status=='oom'
    assert coordinator.snapshot().owner is None


def test_descendant_tree_is_drained(worker):
    coordinator,execute=worker
    outcome,output,_=execute('descendant')
    assert outcome.status=='succeeded'
    pid=int((output/'child-pid').read_text())
    from workbench_platform.resources.recovery import owner_active
    # The Job accounted for and killed the grandchild after the worker exited.
    try:identity=process_identity(pid)
    except OSError:identity='missing-identity'
    assert owner_active(Owner('manual','childLaunch','childInstance',pid,identity)) is False
    assert coordinator.snapshot().owner is None


def test_timeout_drains_before_releasing(worker):
    coordinator,execute=worker
    outcome,_,_=execute('wait',timeout=.15)
    assert outcome.status=='failed' and '时限' in outcome.error
    assert coordinator.snapshot().owner is None


def test_continued_progress_does_not_hit_a_total_task_timeout(worker):
    _,execute=worker
    outcome,_,_=execute('progress',timeout=1.5)
    assert outcome.status=='succeeded' and outcome.elapsed_seconds>2,outcome


def test_event_rejects_stale_epoch_and_modified_checkpoint(tmp_path):
    path=tmp_path/'chunk.json';path.write_text('original')
    event={'event':'chunk_committed','worker_epoch':'old','chunk_index':0,
        'checkpoint':{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
    atomic_json(tmp_path/'runner-event.json',event)
    assert read_event(tmp_path,'current',tmp_path) is None
    event['worker_epoch']='current';atomic_json(tmp_path/'runner-event.json',event)
    path.write_text('modified')
    with pytest.raises(ValueError,match='changed'):read_event(tmp_path,'current',tmp_path)


def test_reclaim_requires_dead_owner_and_named_job_evidence(tmp_path):
    clock=[0]
    coordinator=GpuCoordinator(tmp_path/'gpu.sqlite3',ttl_seconds=1,clock=lambda:clock[0])
    owner=Owner('manual','launchManual','instanceManual',12345,'123456789012345678')
    coordinator.request('oldManual','manual')
    lease=coordinator.grant('oldManual',owner,owned=lambda _:True)
    directory=tmp_path/'owners';directory.mkdir()
    record={'format':'clear-gpu-owner-v1','request_id':'oldManual','generation':lease.generation,
        'owner':asdict(owner),'job_name':'Local\\ClearGpu-proof','kill_on_close':True,'breakaway':False}
    atomic_json(directory/'oldManual.json',record)
    clock[0]=2
    with pytest.raises(ReleaseUnconfirmed):
        recover_owned(coordinator,directory,active_probe=lambda _:False,job_probe=lambda _:True)
    assert coordinator.snapshot().owner==owner
    assert recover_owned(coordinator,directory,active_probe=lambda _:False,job_probe=lambda _:False)
    assert coordinator.snapshot().owner is None


def test_no_receipt_or_ttl_only_never_reclaims(tmp_path):
    clock=[0];coordinator=GpuCoordinator(tmp_path/'gpu.sqlite3',ttl_seconds=1,clock=lambda:clock[0])
    owner=Owner('ocr','launchOcr','instanceOcr',12345,'123456789012345678')
    coordinator.request('oldOcr','ocr');coordinator.grant('oldOcr',owner,owned=lambda _:True)
    clock[0]=2
    with pytest.raises(ReleaseUnconfirmed):recover_owned(coordinator,tmp_path)
    assert coordinator.snapshot().owner==owner


def test_local_qualification_changes_with_library_program_model_and_driver(tmp_path):
    runtime=tmp_path/'runtime'/'python.exe';runtime.parent.mkdir();runtime.write_bytes(b'python')
    library=runtime.parent/'Lib'/'fake.py';library.parent.mkdir();library.write_text('v1')
    program=tmp_path/'worker.py';program.write_text('v1')
    device={'index':0,'uuid':'GPU-local','driver':'1','memory_mib':16384}
    qualification=Qualification(tmp_path/'qualification.json',inventory=lambda:[device.copy()])
    binding=qualification.binding('audio',runtime,[program],{'asr':{'sha256':'a'*64}})
    evidence={'succeeded':True,'actual_device':'cuda','cold_cache':True,'network_blocked':True,
        'cleanup_confirmed':True,'peak_memory_mib':12000,'memory_method':'device_total','result_sha256':'b'*64,
        'execution':{'fresh_inference_chunks':1,'device_execution':'fresh_inference',
            'chunk_devices':{'0':{key:'cuda:0' for key in ('asr','vad','punctuation','speaker')}}}}
    qualification.record('audio',binding,evidence)
    assert qualification.ready('audio',binding)
    library.write_text('v2-longer')
    assert not qualification.ready('audio',qualification.binding('audio',runtime,[program],binding['resources']))
    program.write_text('v2')
    assert not qualification.ready('audio',qualification.binding('audio',runtime,[program],binding['resources']))
    assert not qualification.ready('audio',{**binding,'resources':{'asr':{'sha256':'c'*64}}})
    assert not qualification.ready('audio',{**binding,'device':{**device,'driver':'2'}})
    assert not qualification.ready('audio',{**binding,'device':{**device,'uuid':'GPU-other'}})
    with pytest.raises(ValueError):qualification.record('audio',binding,{**evidence,'cleanup_confirmed':False})
    with pytest.raises(ValueError):qualification.record('audio',binding,{**evidence,'peak_memory_mib':float('nan')})
    with pytest.raises(ValueError):qualification.record('audio',binding,{**evidence,'execution':{
        **evidence['execution'],'fresh_inference_chunks':0,'device_execution':'verified_checkpoint_reuse_only'}})

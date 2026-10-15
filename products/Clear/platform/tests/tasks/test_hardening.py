import json
import pytest
from sqlalchemy import select
from workbench_platform.tasks import schema_v1 as s
from workbench_platform.tasks.files import envelope,response_bytes,response_name,response_path
from workbench_platform.tasks.store import LedgerError
from .conftest import submission

def test_private_parameters_do_not_collapse_idempotency(task_case):
    ledger,admin,editor,viewer,stranger,workspace=task_case
    value=submission(workspace);value['options']={'api_key':'first-secret'}
    task=ledger.submit(editor,value,{'method':'POST'})
    with ledger.engine.connect() as db:
        stored=db.execute(select(s.tasks.c.options).where(s.tasks.c.id==task['id'])).scalar_one()
    assert stored['api_key']=='[redacted]'
    assert ledger.existing_submission(editor,value)['id']==task['id']
    with pytest.raises(LedgerError):ledger.submit(editor,{**value,'options':{'api_key':'different-secret'}},{})

def test_revoke_immediately_removes_access_and_preserves_last_owner(task_case):
    ledger,admin,editor,viewer,stranger,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{})
    ledger.remove_member(admin,workspace,viewer['id'])
    with pytest.raises(LedgerError):ledger.detail(viewer,task['id'])
    with pytest.raises(LedgerError):ledger.remove_member(admin,workspace,admin['id'])

def test_response_envelope_is_file_backed_and_tamper_checked(tmp_path):
    data=b'ZIP-CONTENT'*10000
    value=envelope(tmp_path,200,{'content-type':'application/zip','content-disposition':'attachment; filename="成果.zip"'},data)
    assert 'content_hex' not in value and len(json.dumps(value))<1024
    assert response_bytes(tmp_path,value)==data
    assert response_name(value['headers'])=='成果.zip'
    response_path(tmp_path,value).write_bytes(b'changed')
    with pytest.raises(LedgerError):response_bytes(tmp_path,value)

def test_detail_cursor_scope_and_complete_steps(task_case):
    from sqlalchemy import insert
    ledger,admin,editor,viewer,stranger,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{})
    with ledger.engine.begin() as db:
        for number in range(5):db.execute(insert(s.task_step_runs).values(id=f"page-{task['id']}-{number}",task_id=task['id'],run_id=task['current_run_id'],step_key=str(number),attempt=1,tool_id='data',action='read',status='succeeded',details={}))
    first=ledger.collection(viewer,task['id'],'steps',2)
    second=ledger.collection(viewer,task['id'],'steps',2,first['next_cursor'])
    third=ledger.collection(viewer,task['id'],'steps',2,second['next_cursor'])
    assert len({row['id'] for page in (first,second,third) for row in page['items']})==5
    assert third['next_cursor'] is None
    with pytest.raises(LedgerError):ledger.collection(viewer,task['id'],'inputs',2,first['next_cursor'])

def test_admission_is_atomic_and_does_not_block_file_uploads(task_case):
    from concurrent.futures import ThreadPoolExecutor
    ledger,admin,editor,viewer,stranger,workspace=task_case
    for number in range(8):ledger.submit(editor,submission(workspace,'heavy-'+str(number)),{})
    with ThreadPoolExecutor(max_workers=8) as pool:claimed=list(pool.map(lambda n:ledger.claim('admission-'+str(n),heavy_limit=1),range(8)))
    assert sum(row is not None for row in claimed)==1
    upload=ledger.submit(editor,submission(workspace,'light-upload'),{'resource_class':'io'})
    assert ledger.claim('upload-worker',heavy_limit=1)['task_id']==upload['id']

def test_child_native_retry_preserves_previous_attempt(task_case,tmp_path):
    import sqlite3
    from sqlalchemy import insert,update
    from workbench_platform.tasks.worker import ExecutionHost
    ledger,admin,editor,viewer,stranger,workspace=task_case
    parent=ledger.submit(editor,submission(workspace,'parent-batch'),{})
    child=ledger.submit(editor,submission(workspace,'child-job'),{},parent={'task_id':parent['id'],'run_id':parent['current_run_id']})
    native=tmp_path/'data-workbench';native.mkdir()
    with sqlite3.connect(native/'sessions.sqlite3') as db:db.execute('CREATE TABLE placeholder(id TEXT)')
    with ledger.engine.begin() as db:
        db.execute(insert(s.task_source_links).values(id='source-'+child['id'],task_id=child['id'],run_id=child['current_run_id'],namespace='data.jobs',source_id='native-child',source_generation=1,metadata={}))
        db.execute(update(s.tasks).where(s.tasks.c.id==child['id']).values(status='failed'))
        db.execute(update(s.task_runs).where(s.task_runs.c.id==child['current_run_id']).values(status='failed',error='原失败证据'))
    host=ExecutionHost.__new__(ExecutionHost);host.engine=ledger.engine;host.ledger=ledger;host.root=tmp_path
    host.track_child_retry('data.jobs','native-child')
    result=ledger.detail(viewer,child['id'])
    assert len(result['runs'])==2 and result['runs'][0]['error']=='原失败证据'
    assert result['runs'][1]['scope']['parent_run_id']==parent['current_run_id']
    assert host.current_native_generation('data.jobs','native-child')==2

def test_known_original_size_survives_a_missing_native_file(task_case):
    from workbench_platform.tasks.native_inputs import known_sizes
    ledger,admin,editor,viewer,stranger,workspace=task_case
    ledger.submit(editor,submission(workspace,'original-size'),{},[{'asset_id':'original','asset_revision':1,'blob_hash':'a'*64,'name':'original.pdf','size_bytes':606}])
    values=known_sizes(ledger.engine,workspace,[{'blob_hash':'a'*64,'size_bytes':None}])
    assert values[0]['size_bytes']==606
    other=ledger.create_workspace(admin,'另一个大小隔离项目')['workspace_id']
    assert known_sizes(ledger.engine,other,[{'blob_hash':'a'*64,'size_bytes':None}])[0]['size_bytes'] is None

def test_cancellation_intent_survives_a_running_observation(task_case):
    ledger,admin,editor,viewer,stranger,workspace=task_case
    task=ledger.submit(editor,submission(workspace,'cancel-fence'),{})
    dispatch=ledger.claim('worker')
    ledger.observe(dispatch,'running',identity=task['id']+'running',observation={'summary':{'native_capabilities':['cancel']}})
    current=ledger.detail(editor,task['id'])['task']
    ledger.control(editor,task['id'],'cancel',current['revision'],'cancel-request')
    ledger.observe(dispatch,'running',identity=task['id']+'still-running')
    assert ledger.detail(editor,task['id'])['task']['status']=='cancelling'
    ledger.observe(dispatch,'cancelled',identity=task['id']+'confirmed',terminal=True)
    current=ledger.detail(editor,task['id'])['task']
    ledger.control(editor,task['id'],'retry',current['revision'],'new-attempt')
    with ledger.engine.connect() as db:
        states=db.execute(select(s.task_dispatches.c.state).where(s.task_dispatches.c.run_id==dispatch['run_id'],s.task_dispatches.c.kind=='control')).scalars().all()
    assert states==['obsolete']

def test_unknown_previous_control_blocks_a_new_attempt(task_case):
    from sqlalchemy import insert
    ledger,admin,editor,viewer,stranger,workspace=task_case
    task=ledger.submit(editor,submission(workspace,'uncertain-control'),{})
    dispatch=ledger.claim('worker');ledger.observe(dispatch,'failed',identity=task['id']+'failure',terminal=True)
    with ledger.engine.begin() as db:db.execute(insert(s.task_dispatches).values(id='pending-'+task['id'],task_id=task['id'],run_id=dispatch['run_id'],kind='control',state='needs_verification',generation=1,command={'action':'cancel'}))
    current=ledger.detail(editor,task['id'])['task']
    with pytest.raises(LedgerError,match='控制请求尚未核实'):ledger.control(editor,task['id'],'retry',current['revision'],'unsafe-retry')

def test_cancel_after_claim_does_not_send_a_native_request(task_case,tmp_path):
    import asyncio
    import httpx
    from workbench_platform.tasks.worker import ExecutionHost
    ledger,admin,editor,viewer,stranger,workspace=task_case
    task=ledger.submit(editor,submission(workspace,'cancel-before-send'),{'tool_id':'data','action':'import','method':'POST','path':'/never-send','payload':{}})
    dispatch=ledger.claim('worker')
    current=ledger.detail(editor,task['id'])['task'];ledger.control(editor,task['id'],'cancel',current['revision'],'cancel-claimed')
    async def exercise():
        def reject(request):raise AssertionError('A cancelled unstarted task reached its native tool')
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            host=ExecutionHost(ledger.engine,tmp_path,client)
            await host.execute(dispatch);await host.close()
    asyncio.run(exercise())
    assert ledger.detail(editor,task['id'])['task']['status']=='cancelled'

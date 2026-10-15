from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select,update,func
from workbench_platform.tasks import schema_v1 as s
from workbench_platform.tasks.store import LedgerError,now
from .conftest import submission

def test_submission_is_atomic_and_duplicate_requests_share_identity(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    value=submission(workspace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(lambda _:ledger.submit(editor,value,{'path':'test'}),range(8)))
    assert len({r['id'] for r in rows})==1
    with ledger.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(s.task_runs).where(s.task_runs.c.task_id==rows[0]['id'])).scalar_one()==1
        assert db.execute(select(func.count()).select_from(s.task_dispatches).where(s.task_dispatches.c.task_id==rows[0]['id'])).scalar_one()==1
    with pytest.raises(LedgerError,match='另一组参数'):ledger.submit(editor,{**value,'action':'export'},{})

def test_project_shared_read_but_own_control_only(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{})
    assert ledger.detail(viewer,task['id'])['task']['id']==task['id']
    assert ledger.list(other,{'workspace_id':workspace})['items']==[]
    with pytest.raises(LedgerError):ledger.detail(other,task['id'])
    with pytest.raises(LedgerError):ledger.control(viewer,task['id'],'cancel',task['revision'],'c1')
    second=ledger.create_user('second-'+task['id'],'其他编制人','correct horse battery staple')
    ledger.set_member(admin,workspace,second['id'],'editor')
    with pytest.raises(LedgerError):ledger.control(second,task['id'],'cancel',task['revision'],'c2')
    assert ledger.control(editor,task['id'],'cancel',task['revision'],'c3')['request_state']=='recorded'
    assert ledger.detail(viewer,task['id'])['task']['status']=='cancelled'

def test_retry_adds_attempt_without_overwriting_failed_inputs_and_error(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{'path':'test'},[{'asset_id':'asset1','asset_revision':1,'blob_hash':'a'*64,'size_bytes':12,'name':'票据.csv'}])
    dispatch=ledger.claim('w1')
    # Other tests share a database, so pick this dispatch explicitly if necessary.
    while dispatch and dispatch['task_id']!=task['id']:
        ledger.observe(dispatch,'failed',identity=dispatch['id']+':test-close',terminal=True)
        dispatch=ledger.claim('w1')
    assert dispatch
    ledger.observe(dispatch,'failed',identity='failure-'+task['id'],observation={'error':'真实失败证据'},terminal=True)
    before=ledger.detail(editor,task['id'])
    ledger.control(editor,task['id'],'retry',before['task']['revision'],'retry-'+task['id'])
    after=ledger.detail(editor,task['id'])
    assert len(after['runs'])==2 and after['runs'][0]['error']=='真实失败证据'
    assert before['inputs']==after['inputs'] and after['task']['input_total_bytes']==12
    assert after['runs'][0]['finished_at']==before['runs'][0]['finished_at']

def test_claim_skips_locked_rows_and_generation_fences_old_results(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    ids={ledger.submit(editor,submission(workspace,'r'+str(i)),{})['id'] for i in range(12)}
    with ThreadPoolExecutor(max_workers=12) as pool:claims=list(pool.map(lambda n:ledger.claim('worker'+str(n)),range(12)))
    assert len({row['id'] for row in claims if row})==len([r for r in claims if r])
    selected=next((r for r in claims if r and r['task_id'] in ids),None)
    assert selected
    with ledger.engine.begin() as db:db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==selected['id']).values(lease_token='new-owner'))
    with pytest.raises(LedgerError,match='旧执行代际'):ledger.observe(selected,'succeeded',identity='stale'+selected['id'],terminal=True)

def test_expired_lease_does_not_requeue_native_execution(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{})
    with ledger.engine.begin() as db:db.execute(update(s.task_dispatches).where(s.task_dispatches.c.task_id==task['id']).values(state='leased',lease_until=now()-timedelta(seconds=1)))
    assert ledger.expire_leases()>=1
    assert ledger.detail(editor,task['id'])['task']['status']=='needs_verification'
    with pytest.raises(LedgerError,match='尚未核实'):ledger.control(editor,task['id'],'retry',ledger.detail(editor,task['id'])['task']['revision'],'cannot-retry')

def test_keyset_paging_is_stable_and_filter_bound(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    for n in range(6):ledger.submit(editor,submission(workspace,'page'+str(n)),{})
    first=ledger.list(editor,{'workspace_id':workspace},3)
    ledger.submit(editor,submission(workspace,'new-page'),{})
    second=ledger.list(editor,{'workspace_id':workspace},3,first['next_cursor'])
    assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
    with pytest.raises(LedgerError):ledger.list(editor,{'workspace_id':workspace,'status':'failed'},3,first['next_cursor'])

def test_revoked_membership_and_disabled_login_take_effect(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{})
    user,token,csrf=ledger.login(editor['username'],'correct horse battery staple')
    with ledger.engine.begin() as db:db.execute(update(s.users).where(s.users.c.id==editor['id']).values(disabled=True))
    with pytest.raises(LedgerError):ledger.authenticate(token)
    with ledger.engine.begin() as db:
        from sqlalchemy import delete
        db.execute(delete(s.workspace_members).where(s.workspace_members.c.user_id==viewer['id'],s.workspace_members.c.workspace_id==workspace))
    with pytest.raises(LedgerError):ledger.detail(viewer,task['id'])

def test_event_replay_and_terminal_state_are_idempotent(task_case):
    ledger,admin,editor,viewer,other,workspace=task_case
    task=ledger.submit(editor,submission(workspace),{})
    with ledger.engine.begin() as db:
        row=db.execute(select(s.task_dispatches).where(s.task_dispatches.c.task_id==task['id'])).mappings().one()
        db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(lease_token='test',state='leased'))
    dispatch={**row,'lease_token':'test'}
    ledger.observe(dispatch,'succeeded',identity='done'+task['id'],terminal=True)
    assert not ledger.observe(dispatch,'succeeded',identity='done'+task['id'],terminal=True)
    assert not ledger.observe(dispatch,'running',identity='late'+task['id'])
    events=ledger.events(editor,task['id'])
    assert len(events['items'])==2
    assert ledger.events(editor,task['id'],events['next_cursor'])['items']==[]

import sqlite3
import pytest
from workbench_platform.tasks import journal

def test_native_transaction_and_its_event_commit_or_rollback_together(tmp_path):
    path=tmp_path/'domain.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,error TEXT)')
    journal.install(path,[('data.jobs','jobs','id',None)])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO jobs VALUES('a','queued',NULL)")
    assert len(journal.pending(path))==1
    with pytest.raises(RuntimeError):
        with sqlite3.connect(path) as db:
            db.execute("UPDATE jobs SET status='running' WHERE id='a'")
            raise RuntimeError('crash before commit')
    assert len(journal.pending(path))==1
    journal.acknowledge(path,1)
    assert journal.pending(path)==[]

def test_background_retry_transitions_remain_in_order(tmp_path):
    path=tmp_path/'domain.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE records(job_id TEXT PRIMARY KEY,body TEXT)')
    journal.install(path,[('manual.manual_analysis','records','job_id','body')])
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO records VALUES(?,?)',('m1','{"generation":1,"status":"failed"}'))
        db.execute('UPDATE records SET body=? WHERE job_id=?',('{"generation":2,"status":"queued"}','m1'))
    assert [r['generation'] for r in journal.pending(path)]==[1,2]

def test_backlog_counts_only_unacknowledged_events(tmp_path):
    folder=tmp_path/'data-workbench';folder.mkdir()
    path=folder/'sessions.sqlite3'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT)')
    journal.install(path,[('data.jobs','jobs','id',None)])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO jobs VALUES('a','running')")
        db.execute("UPDATE jobs SET status='succeeded' WHERE id='a'")
    journal.acknowledge(path,1)
    assert journal.backlog(tmp_path)=={'pending_events':1,'sources':{'data-workbench/sessions.sqlite3':1}}

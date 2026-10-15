import asyncio
import json
import sqlite3
from sqlalchemy import insert
from workbench_platform.tasks import journal,schema_v1 as s
from workbench_platform.tasks.worker import ExecutionHost
from .conftest import submission


def test_one_invalid_event_does_not_starve_later_committed_events(task_case,tmp_path):
    ledger,admin,editor,viewer,stranger,workspace=task_case
    task=ledger.submit(editor,submission(workspace,'journal-recovery'),{})
    path=tmp_path/'data-workbench/sessions.sqlite3';path.parent.mkdir()
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE jobs(id TEXT,status TEXT)')
    journal.install(path,[('data.jobs','jobs','id',None)])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO central_task_outbox(namespace,source_id,generation,payload) VALUES('data.jobs','bad',1,'{broken')")
        db.execute("INSERT INTO jobs VALUES('good','queued')")
    with ledger.engine.begin() as db:db.execute(insert(s.task_source_links).values(id='source-'+task['id'],task_id=task['id'],run_id=task['current_run_id'],namespace='data.jobs',source_id='good',source_generation=1,metadata={}))
    host=ExecutionHost.__new__(ExecutionHost);host.root=tmp_path;host.engine=ledger.engine;host.ledger=ledger;host.publish_offsets={};host.worker_id='test-publisher'
    asyncio.run(host.publish([path]))
    pending=journal.pending(path)
    assert len(pending)==1 and pending[0]['source_id']=='bad'
    assert ledger.events(viewer,task['id'])['items'][-1]['kind']=='native_snapshot'
    assert json.loads((tmp_path/'central-event-sync-error.json').read_text())['error_type']=='JSONDecodeError'

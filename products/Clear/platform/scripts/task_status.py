"""Inspect only ledger state and execution errors, without credentials."""
import json
from sqlalchemy import select
from workbench_platform.tasks.database import engine_for
from workbench_platform.tasks import schema_v1 as s
from workbench_platform.tasks.store import wire
engine=engine_for()
with engine.connect() as db:
    tasks=[dict(row) for row in db.execute(select(s.tasks.c.id,s.tasks.c.name,s.tasks.c.status,s.tasks.c.current_run_id).order_by(s.tasks.c.updated_at.desc()).limit(5)).mappings()]
    for task in tasks:
        task['runs']=[dict(row) for row in db.execute(select(s.task_runs.c.attempt,s.task_runs.c.status,s.task_runs.c.error).where(s.task_runs.c.task_id==task['id'])).mappings()]
        task['dispatches']=[{'state':row['state'],'kind':row['kind'],'http_status':row['receipt'].get('http_status'),'binding':row['receipt'].get('binding')} for row in db.execute(select(s.task_dispatches.c.state,s.task_dispatches.c.kind,s.task_dispatches.c.receipt).where(s.task_dispatches.c.task_id==task['id'])).mappings()]
print(json.dumps(wire(tasks),ensure_ascii=False,indent=2))

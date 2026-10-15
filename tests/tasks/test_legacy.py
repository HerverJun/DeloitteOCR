import json
import sqlite3
from sqlalchemy import select
from workbench_platform.tasks.legacy import import_legacy
from workbench_platform.tasks import schema_v1 as s

def test_import_is_repeatable_and_preserves_missing_fields(task_engine,tmp_path):
    (tmp_path/'legacy-snapshot.json').write_text('{"cold":true}')
    with sqlite3.connect(tmp_path/'workspaces.sqlite3') as db:
        db.execute('CREATE TABLE workspace(workspace_id TEXT,name TEXT,revision INTEGER,created_at TEXT,updated_at TEXT)')
        db.execute("INSERT INTO workspace VALUES('legacySpace','历史项目',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
    with sqlite3.connect(tmp_path/'jobs.sqlite3') as db:
        db.execute('CREATE TABLE projected_jobs(platform_job_id TEXT,workspace_id TEXT,app_id TEXT,generation INTEGER,native_ids TEXT,payload TEXT)')
        db.execute('INSERT INTO projected_jobs VALUES(?,?,?,?,?,?)',('oldProjection','legacySpace','ocr',1,'["oldNative"]','{"status":"failed"}'))
    native=tmp_path/'native/ocr';native.mkdir(parents=True)
    with sqlite3.connect(native/'workbench.sqlite3') as db:
        db.execute('CREATE TABLE tasks(id TEXT,project_id TEXT,status TEXT,created TEXT,error TEXT)')
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)',('oldNative','unknownProject','failed',None,'原失败原因'))
    first=import_legacy(task_engine,tmp_path);second=import_legacy(task_engine,tmp_path)
    assert first['tasks']==1 and first['aliases']==1 and second['tasks']==0
    with task_engine.connect() as db:
        row=db.execute(select(s.tasks).join(s.task_source_links,s.tasks.c.id==s.task_source_links.c.task_id).where(s.task_source_links.c.namespace=='projection',s.task_source_links.c.source_id=='oldProjection')).mappings().one()
    assert row['created_by'] is None and row['created_at'] is None

def test_live_directory_is_not_silently_migrated(task_engine,tmp_path):
    import pytest
    with pytest.raises(ValueError,match='snapshot'):import_legacy(task_engine,tmp_path)

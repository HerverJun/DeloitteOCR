import sqlite3
import pytest
from workbench_platform.tasks.native import check_native_scope,execution_action
from workbench_platform.tasks.store import LedgerError

def test_native_result_and_payload_ids_cannot_cross_projects(tmp_path):
    root=tmp_path/'native/ocr';root.mkdir(parents=True)
    with sqlite3.connect(root/'workbench.sqlite3') as db:
        db.executescript('CREATE TABLE projects(id TEXT);CREATE TABLE tasks(id TEXT,project_id TEXT);CREATE TABLE results(id TEXT,task_id TEXT);')
        db.execute("INSERT INTO projects VALUES('projectA')");db.execute("INSERT INTO projects VALUES('projectB')")
        db.execute("INSERT INTO tasks VALUES('taskB','projectB')");db.execute("INSERT INTO results VALUES('resultB','taskB')")
    with pytest.raises(LedgerError):check_native_scope(tmp_path,'ocr','projectA','api/results/resultB',{},'')
    with pytest.raises(LedgerError):check_native_scope(tmp_path,'ocr','projectA','api/projects/projectA/tasks',{'task_ids':['taskB']},'')
    check_native_scope(tmp_path,'ocr','projectA','api/projects/projectA/tasks',{},'')

def test_compute_import_and_export_boundaries_are_recognized():
    assert execution_action('POST','api/v1/projects/a/experiments',{})=='train'
    assert execution_action('POST','api/projects/a/images',{})=='import'
    assert execution_action('GET','api/v1/jobs/a/export',{})=='export'
    assert execution_action('PATCH','api/projects/a',{}) is None

import hashlib
import json
from workbench_platform.agent.credentials import CredentialVault
from workbench_platform.tasks.maintenance import relocate_native_request


def test_restore_relocates_encrypted_native_json_but_preserves_multipart(tmp_path):
    folder=tmp_path/'native-requests';folder.mkdir()
    source=folder/'request.bin';raw=json.dumps({'directory':'F:\\old-workspace\\inputs','name':'留存说明'}).encode()
    source.write_bytes(CredentialVault._crypt(raw))
    command={'native':{'body_file':str(source),'body_encoding':'dpapi','content_type':'application/json','body_hash':hashlib.sha256(raw).hexdigest()}}
    def relocate(value):
        if isinstance(value,dict):return {key:relocate(item) for key,item in value.items()}
        if isinstance(value,str):return value.replace('F:\\old-workspace',str(tmp_path))
        return value
    moved=relocate_native_request(tmp_path,command,relocate)
    decoded=CredentialVault._crypt(source.read_bytes(),decrypt=True)
    assert json.loads(decoded)['directory']==str(tmp_path)+'\\inputs'
    assert moved['native']['body_hash']==hashlib.sha256(decoded).hexdigest()
    assert relocate_native_request(tmp_path,command,relocate)==moved
    unchanged={**command,'native':{**command['native'],'content_type':'multipart/form-data; boundary=test'}}
    before=source.read_bytes()
    assert relocate_native_request(tmp_path,unchanged,relocate)==unchanged
    assert source.read_bytes()==before

def test_backup_recovers_a_hot_journal_without_copying_uncommitted_rows(tmp_path):
    import sqlite3
    import subprocess
    import sys
    from workbench_platform.tasks.maintenance import sqlite_snapshot
    source=tmp_path/'source.sqlite3';target=tmp_path/'backup.sqlite3'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE values_(id INTEGER PRIMARY KEY,body BLOB)')
        db.execute("INSERT INTO values_ VALUES(1,zeroblob(4000000))")
    code="""import sqlite3,sys,os
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA cache_size=10')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE values_ SET body=randomblob(4000000) WHERE id=1')
os._exit(0)
"""
    subprocess.run([sys.executable,'-B','-c',code,str(source)],check=True,cwd=tmp_path)
    assert source.with_name(source.name+'-journal').is_file()
    sqlite_snapshot(source,target)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT body=zeroblob(4000000) FROM values_').fetchone()[0]==1
    assert not source.with_name(source.name+'-journal').exists()

def test_postgres_restore_matches_windows_path_separators_exactly(task_case):
    from sqlalchemy import insert,select
    from workbench_platform.tasks import schema_v1 as s
    from workbench_platform.tasks.maintenance import relocate_artifact_paths
    from .conftest import submission
    ledger,admin,editor,viewer,other,workspace=task_case
    task=ledger.submit(editor,submission(workspace,'windows-restore'),{})
    old='F:\\restore-check\\workspace';new='F:\\restore-check\\new-workspace'
    with ledger.engine.begin() as db:
        for number,path in enumerate([old+'\\task-files\\report.json',old+'/task-files/page.png',old+'-other\\keep.json']):
            db.execute(insert(s.task_artifact_links).values(id=task['id']+str(number),task_id=task['id'],run_id=task['current_run_id'],name=str(number),kind='output',source_path=path))
        relocate_artifact_paths(db,old,new)
        rows=db.execute(select(s.task_artifact_links.c.source_path).where(s.task_artifact_links.c.task_id==task['id']).order_by(s.task_artifact_links.c.name)).scalars().all()
    assert rows==[new+'\\task-files\\report.json',new+'/task-files/page.png',old+'-other\\keep.json']

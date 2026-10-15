"""F-drive consistent backups, new-directory restores and conservative cleanup."""
from contextlib import closing,contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
from sqlalchemy import select,text,update,func
from sqlalchemy.engine import make_url
from . import schema_v1 as s
from .database import f_path,check_schema
from .store import now,wire,key,TERMINAL

MEMBERS={'asset-blobs','asset-uploads','artifact-center','data-workbench','batches','audit','forensics','agent','manual-analysis','native','task-files','task-response-files','task-upload-inputs','native-requests','ai-credentials','task-command-secrets'}
ROOT_FILES={'assets.sqlite3','workspaces.sqlite3','jobs.sqlite3','worker-commands.sqlite3','recent-work.sqlite3','ocr-handoffs.sqlite3','classification-handoffs.sqlite3','product-reader-requirements.json','central-db-identity.json','task-fingerprint.key'}
def hash_file(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def sqlite_snapshot(source,target):
    # The caller owns the global write barrier and has drained native work.
    # mode=rw permits SQLite to roll back a hot journal left by a crashed
    # process; mode=ro cannot recover it. It never creates a missing source.
    with closing(sqlite3.connect(source.as_uri()+'?mode=rw',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        if src.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Source SQLite integrity failed')
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('SQLite backup integrity failed')

def relocate_native_request(root,command,relocate):
    """Move operational JSON paths while keeping task input snapshots intact.

    A consistent backup has no unconfirmed sent requests. Multipart original
    files are byte-preserved; only JSON bodies used by future dispatches change.
    """
    native=command.get('native')
    if not native or not native.get('body_file') or 'application/json' not in native.get('content_type',''):return command
    source=f_path(native['body_file'])
    if not source.is_relative_to(root/'native-requests'):raise ValueError('Restored native request escapes its workspace')
    from workbench_platform.agent.credentials import CredentialVault
    raw=source.read_bytes()
    if native.get('body_encoding')=='dpapi':raw=CredentialVault._crypt(raw,decrypt=True)
    # Shared request files may already have been relocated for another command.
    value=json.loads(raw);moved=relocate(value)
    if moved!=value:
        if hashlib.sha256(raw).hexdigest()!=native['body_hash']:raise ValueError('Native request hash mismatch during restore')
        raw=json.dumps(moved,ensure_ascii=False).encode()
        source.write_bytes(CredentialVault._crypt(raw) if native.get('body_encoding')=='dpapi' else raw)
    return {**command,'native':{**native,'body_hash':hashlib.sha256(raw).hexdigest()}}
def postgres_bin():
    path=f_path(os.environ['CLEAR_POSTGRES_BIN'])
    if not (path/'pg_dump.exe').is_file():raise ValueError('Explicit F-drive PostgreSQL binaries are required')
    return path
def pg_args(engine):
    url=engine.url
    env=dict(os.environ,PGPASSWORD=url.password or '')
    return ['-h',url.host or '127.0.0.1','-p',str(url.port or 5432),'-U',url.username,'-d',url.database],env
def run_pg(binary,args,env):
    result=subprocess.run([str(postgres_bin()/binary),*args],env=env,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('PostgreSQL backup/restore command failed; original data retained')
def maintenance_file(root):return root/'central-maintenance.json'
BACKUP_LOCK=74001420261015

@contextmanager
def shared_write_guard(engine):
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as db:
        db.execute(text('SELECT pg_advisory_lock_shared(:key)'),{'key':BACKUP_LOCK})
        try:yield
        finally:
            try:db.execute(text('SELECT pg_advisory_unlock_shared(:key)'),{'key':BACKUP_LOCK})
            except Exception:db.invalidate();raise

def external_assets(root):
    path=root/'native/classification/platform.sqlite3'
    if not path.is_file():return []
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        paths=[r[0] for r in db.execute('SELECT DISTINCT path FROM assets')] if 'assets' in names else []
    return [Path(value).resolve() for value in paths if value and Path(value).is_file() and not Path(value).resolve().is_relative_to(root)]

def backup(engine,root):
    flag=maintenance_file(root)
    try:
        with flag.open('x',encoding='utf-8') as stream:json.dump({'phase':'draining','started_at':wire(now())},stream)
    except FileExistsError:raise RuntimeError('Another maintenance operation is active') from None
    destination=root/'backups'/(now().strftime('%Y%m%dT%H%M%SZ')+'-'+key('b')[1:9])
    exclusive=None
    try:
        with engine.connect() as db:database_bytes=db.execute(text('SELECT pg_database_size(current_database())')).scalar_one()
        file_bytes=sum(path.stat().st_size for name in MEMBERS if (root/name).is_dir() for path in (root/name).rglob('*') if path.is_file() and not path.is_symlink())
        external=external_assets(root);file_bytes+=sum(path.stat().st_size for path in external)
        if shutil.disk_usage(root).free<database_bytes+file_bytes+512*1024**2:raise RuntimeError('F drive has insufficient space for a consistent backup; no other drive will be used')
        deadline=time.monotonic()+300
        while True:
            with engine.connect() as db:active=db.execute(select(func.count()).select_from(s.task_dispatches.join(s.task_runs,s.task_dispatches.c.run_id==s.task_runs.c.id)).where(
                s.task_dispatches.c.state.in_(['leased','observing','needs_verification']),
                (s.task_dispatches.c.state=='needs_verification') | (s.task_dispatches.c.kind=='control') | s.task_runs.c.status.not_in(['paused','waiting_user']))).scalar_one()
            if not active:break
            if time.monotonic()>deadline:raise RuntimeError('In-flight executions did not become idle; backup cancelled without stopping them')
            time.sleep(1)
        exclusive=engine.connect().execution_options(isolation_level='AUTOCOMMIT')
        exclusive.execute(text('SET statement_timeout=300000'))
        exclusive.execute(text('SELECT pg_advisory_lock(:key)'),{'key':BACKUP_LOCK})
        temporary=flag.with_suffix('.new')
        temporary.write_text(json.dumps({'phase':'snapshot','started_at':wire(now())}),'utf-8');os.replace(temporary,flag)
        destination.mkdir(parents=True)
        args,env=pg_args(engine)
        run_pg('pg_dump.exe',[*args,'--format=custom','--file',str(destination/'central.pgdump')],env)
        records=[]
        for current,dirs,files in os.walk(root,followlinks=False):
            current=Path(current)
            if current==root:dirs[:]=[name for name in dirs if name in MEMBERS]
            dirs[:]=[name for name in dirs if name not in {'cache','temp','__pycache__'}]
            for name in dirs:
                child=current/name
                if child.is_symlink() or child.is_junction():raise ValueError('Linked business directories are not valid backup members')
            for name in files:
                source=current/name;relative=source.relative_to(root)
                if current==root and name not in ROOT_FILES:continue
                if name.endswith(('-wal','-shm','-journal','.lock')):continue
                if source.is_symlink():raise ValueError('Linked business files are not valid backup members')
                target=destination/'files'/relative;target.parent.mkdir(parents=True,exist_ok=True)
                if source.suffix=='.sqlite3':
                    try:sqlite_snapshot(source,target)
                    except sqlite3.Error as error:raise RuntimeError('SQLite backup failed: '+relative.as_posix()) from error
                else:shutil.copy2(source,target)
                records.append({'path':relative.as_posix(),'sha256':hash_file(target),'size_bytes':target.stat().st_size})
        external_map={}
        for source in external:
            relative=Path('native/classification/external-backup')/(hash_file(source)+source.suffix.lower())
            target=destination/'files'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
            expected=hash_file(source)
            if hash_file(target)!=expected:raise ValueError('External source changed during backup')
            records.append({'path':relative.as_posix(),'sha256':expected,'size_bytes':target.stat().st_size});external_map[str(source)]=relative.as_posix()
        manifest={'format':'clear-central-backup-v1','source_root':str(root),'created_at':wire(now()),'database':engine.url.database,'external_path_map':external_map,
            'database_sha256':hash_file(destination/'central.pgdump'),'files':records}
        (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
        (root/'central-last-backup.json').write_text(json.dumps({'path':str(destination),'completed_at':wire(now())}),'utf-8')
        return {'backup':str(destination),'files':len(records),'verified':verify_backup(engine,destination)['verified']}
    finally:
        if exclusive is not None:
            try:exclusive.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':BACKUP_LOCK})
            finally:exclusive.close()
        flag.unlink(missing_ok=True)

def verify_backup(engine,root):
    manifest=json.loads((root/'manifest.json').read_text('utf-8'))
    if manifest['format']!='clear-central-backup-v1' or hash_file(root/'central.pgdump')!=manifest['database_sha256']:raise ValueError('Backup database hash mismatch')
    for record in manifest['files']:
        path=(root/'files'/record['path']).resolve()
        if not path.is_relative_to(root/'files') or not path.is_file() or hash_file(path)!=record['sha256']:raise ValueError('Backup file hash mismatch')
        if path.suffix=='.sqlite3':
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Backup SQLite integrity failed')
    return {'verified':True,'files':len(manifest['files'])}

def restore(engine,root,destination):
    verify_backup(engine,root)
    if destination.exists():raise ValueError('Restore requires a new destination directory')
    manifest=json.loads((root/'manifest.json').read_text('utf-8'))
    database='clear_restore_'+key('d')[1:17]
    from sqlalchemy import create_engine
    admin=create_engine(engine.url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    with admin.connect() as db:db.execute(text(f'CREATE DATABASE "{database}"'))
    admin.dispose()
    restored=create_engine(engine.url.set(database=database))
    try:
        args,env=pg_args(restored)
        run_pg('pg_restore.exe',[*args,'--no-owner','--no-privileges','--exit-on-error',str(root/'central.pgdump')],env)
        shutil.copytree(root/'files',destination)
        old=manifest['source_root'];new=str(destination)
        def relocate(value):
            if isinstance(value,dict):return {name:relocate(item) for name,item in value.items()}
            if isinstance(value,list):return [relocate(item) for item in value]
            if isinstance(value,str) and (value==old or value.startswith(old+'\\') or value.startswith(old+'/')):return new+value[len(old):]
            return value
        # Relocate operational paths only. Immutable request and evidence snapshots retain their original bytes.
        for path in destination.rglob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as db,db:
                tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in tables:
                    columns=[r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                    for column in columns:
                        if column in {'path','original_path','package_path','manifest_path','source_path'}:
                            db.execute(f'UPDATE "{table}" SET "{column}"=replace("{column}",?,?) WHERE "{column}" LIKE ?', (old,new,old+'%'))
                            for original,relative in manifest.get('external_path_map',{}).items():
                                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?',(str(destination/relative),original))
                    if table=='operation_requests' and 'request' in columns:
                        for identity,raw in db.execute('SELECT request_key,request FROM operation_requests').fetchall():
                            value=json.loads(raw)
                            if isinstance(value.get('session_dir'),str) and value['session_dir'].startswith(old):
                                value['session_dir']=new+value['session_dir'][len(old):]
                                db.execute('UPDATE operation_requests SET request=? WHERE request_key=?',(json.dumps(value,ensure_ascii=False),identity))
                    if table=='command_receipts':
                        for identifier,raw in db.execute('SELECT dispatch_id,receipt FROM command_receipts WHERE receipt IS NOT NULL').fetchall():
                            db.execute('UPDATE command_receipts SET receipt=? WHERE dispatch_id=?',(json.dumps(relocate(json.loads(raw)),ensure_ascii=False),identifier))
        with restored.begin() as db:
            db.execute(update(s.task_artifact_links).where(s.task_artifact_links.c.source_path.startswith(old)).values(source_path=func.replace(s.task_artifact_links.c.source_path,old,new)))
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.state.in_(['leased','observing'])).values(state='needs_verification'))
            from sqlalchemy import delete
            db.execute(delete(s.user_sessions))
            from workbench_platform.agent.credentials import CredentialVault,write_json
            import base64
            vault=CredentialVault(destination/'task-command-secrets')
            for row in db.execute(select(s.task_dispatches.c.id,s.task_dispatches.c.command,s.task_dispatches.c.receipt)).mappings():
                command=relocate(row['command'])
                reference=command.get('private_payload_ref')
                if reference:
                    decoded=json.loads(vault.read(reference));relocated=relocate_native_request(destination,relocate(decoded),relocate)
                    encrypted=CredentialVault._crypt(json.dumps(relocated,ensure_ascii=False).encode())
                    write_json(vault._path(reference),{'dpapi':base64.b64encode(encrypted).decode()},durable=True)
                else:command=relocate_native_request(destination,command,relocate)
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(command=command,receipt=relocate(row['receipt'])))
        check_schema(restored)
        from .directory import bind_directory,identity
        (destination/'central-db-identity.json').write_text(json.dumps(identity(restored)),'utf-8')
        bind_directory(restored,destination)
        record={'database_url':restored.url.render_as_string(hide_password=False),'source_backup':str(root),'original_root':old,'restored_at':wire(now())}
        private=destination/'central-restore-private.json';private.write_text(json.dumps(record,ensure_ascii=False,indent=2),'utf-8')
        if os.name=='nt':
            import getpass
            subprocess.run(['icacls',str(private),'/inheritance:r','/grant:r',getpass.getuser()+':(F)'],capture_output=True,check=True)
        return {'destination':str(destination),'database':database,'verified':True}
    finally:restored.dispose()

def retention(engine,root,apply=False):
    current=now().timestamp();candidates=[]
    with engine.connect() as db:
        active=db.execute(select(s.task_dispatches.c.command,s.task_dispatches.c.receipt)).all()
        referenced={str(row) for row in db.execute(select(s.task_artifact_links.c.source_path).where(s.task_artifact_links.c.source_path.is_not(None))).scalars()}
    def strings(value):
        if isinstance(value,dict):
            for item in value.values():yield from strings(item)
        elif isinstance(value,list):
            for item in value:yield from strings(item)
        elif isinstance(value,str):yield value
    private_unreadable=False
    for command,receipt in active:
        referenced.update(strings(command));referenced.update(strings(receipt))
        if command.get('private_payload_ref'):
            try:
                from .private_payloads import unseal
                referenced.update(strings(unseal(root,command)))
            except Exception:private_unreadable=True
    directories=[('debug-logs',30)]
    if not private_unreadable:directories.extend([('temp',7),('native-requests',7),('task-upload-inputs',7),('task-response-files',7)])
    for directory,days in directories:
        folder=root/directory
        if not folder.is_dir():continue
        for path in folder.rglob('*'):
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()) and str(path) not in referenced and current-path.stat().st_mtime>days*86400:
                candidates.append(str(path))
                if apply:
                    try:path.unlink()
                    except PermissionError:continue
    return {'applied':apply,'files':candidates}

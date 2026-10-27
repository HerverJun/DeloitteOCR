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
from packages.runtime.snapshots import sqlite_snapshot, external_assets

MEMBERS={'asset-blobs','asset-uploads','artifact-center','data-workbench','batches','audit','forensics','agent','manual-analysis','native','task-files','task-response-files','task-upload-inputs','native-requests','documents','comparisons','ai-credentials','task-command-secrets'}
ROOT_FILES={'assets.sqlite3','workspaces.sqlite3','jobs.sqlite3','worker-commands.sqlite3','recent-work.sqlite3','ocr-handoffs.sqlite3','classification-handoffs.sqlite3','product-reader-requirements.json','central-db-identity.json','task-fingerprint.key'}
def hash_file(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def relocate_artifact_paths(db,old,new):
    column=s.task_artifact_links.c.source_path
    # PostgreSQL LIKE treats backslashes as escapes. Match a complete path
    # prefix explicitly, including its separator, for Windows workspaces.
    owned=(column==old) | func.left(column,len(old)+1).in_([old+'\\',old+'/'])
    db.execute(update(s.task_artifact_links).where(owned).values(source_path=func.concat(new,func.substr(column,len(old)+1))))

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
    from packages.runtime import require_path
    path=require_path(os.environ['CLEAR_POSTGRES_BIN'], 'PostgreSQL binaries', role='read')
    if not (path/'pg_dump.exe').is_file():raise ValueError('Explicit local PostgreSQL binaries are required')
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

def backup(engine,root):
    flag=maintenance_file(root)
    try:
        with flag.open('x',encoding='utf-8') as stream:json.dump({'phase':'draining','started_at':wire(now())},stream)
    except FileExistsError:raise RuntimeError('Another maintenance operation is active') from None
    backup_root=f_path(os.environ.get('CLEAR_BACKUP_ROOT') or root/'backups')
    backup_root.mkdir(parents=True,exist_ok=True)
    destination=backup_root/(now().strftime('%Y%m%dT%H%M%SZ')+'-'+key('b')[1:9])
    exclusive=None
    try:
        with engine.connect() as db:database_bytes=db.execute(text('SELECT pg_database_size(current_database())')).scalar_one()
        file_bytes=sum(path.stat().st_size for name in MEMBERS if (root/name).is_dir() for path in (root/name).rglob('*') if path.is_file() and not path.is_symlink())
        external=external_assets(root);file_bytes+=sum(path.stat().st_size for path in external)
        if shutil.disk_usage(backup_root).free<database_bytes+file_bytes+512*1024**2:raise RuntimeError('Configured backup drive has insufficient space for a consistent snapshot')
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
        from packages.runtime import current_install
        installation=current_install()
        policy=installation.policy if installation else {}
        manifest={'format':'clear-central-backup-v1','source_root':str(root),'created_at':wire(now()),'database':engine.url.database,'external_path_map':external_map,
            'legacy_data_roots':policy.get('legacy_data_roots',[]),'legacy_file_map':policy.get('legacy_file_map',{}),
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
        from packages.runtime.migration import relocate_operational, relocate_sqlite
        relocate=lambda value:relocate_operational(value,old,new,context='command')
        migration_reports=[]
        for path in destination.rglob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as db,db:
                report=relocate_sqlite(db,old,new,manifest.get('external_path_map',{}))
                migration_reports.append({'database':path.relative_to(destination).as_posix(),**report})
        # Encrypted source material is evidence only. Target-account credentials
        # and service tokens are initialized independently by the installation CLI.
        retained=destination/'migration'/'account-secrets-reenter'
        for name in ('ai-credentials','task-command-secrets'):
            original=destination/name
            if original.exists():
                retained.mkdir(parents=True,exist_ok=True)
                original=f_path(original);target=f_path(retained/name)
                if not original.is_relative_to(destination) or not target.is_relative_to(destination):
                    raise ValueError('Retained credentials escape restored DATA')
                shutil.move(str(original),str(target))
        with restored.begin() as db:
            relocate_artifact_paths(db,old,new)
            db.execute(update(s.task_dispatches).where(s.task_dispatches.c.state.in_(['leased','observing'])).values(state='needs_verification'))
            from sqlalchemy import delete
            db.execute(delete(s.user_sessions))
            for row in db.execute(select(s.task_dispatches.c.id,s.task_dispatches.c.command,s.task_dispatches.c.receipt,s.task_dispatches.c.state)).mappings():
                command=relocate(row['command'])
                reference=command.get('private_payload_ref')
                if not reference:
                    command=relocate_native_request(destination,command,
                        lambda value:relocate_operational(value,old,new,context='request'))
                receipt=relocate_operational(row['receipt'],old,new,context='receipt')
                values={'command':command,'receipt':receipt}
                if reference and row['state'] in {'queued','leased','observing','needs_verification'}:
                    values['state']='needs_verification'
                db.execute(update(s.task_dispatches).where(s.task_dispatches.c.id==row['id']).values(**values))
        (destination/'migration').mkdir(exist_ok=True)
        (destination/'migration/path-map-v1.json').write_text(json.dumps({
            'mapping_version':1,'source_root':old,'destination_root':new,
            'sqlite':migration_reports,'credentials':'reenter_under_target_windows_account',
            'frozen_requests_results_and_model_bindings':'preserved'},ensure_ascii=False,indent=2),'utf-8')
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

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _windows_case(tmp_path, body):
    platform = Path(__file__).resolve().parents[2]
    ocr = platform.parent / 'ocr/src'
    setup = r'''import hashlib,json,os,subprocess,sys,uuid
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from packages.runtime import load_install,require_model_mapping
from packages.runtime.io_guard import install
root=Path(sys.argv[3]);program=root/'程序 models'
paths={key:root/key for key in ('data','cache','temp','paddle_temp','secrets','backup','postgres')}
paths.update(program=program,assets=program/'assets',runtimes=program/'runtimes',tools=program/'tools')
for path in paths.values():path.mkdir(parents=True,exist_ok=True)
models=paths['assets']/'ocr/models';models.mkdir(parents=True)
model=models/'model.bin';model.write_bytes(b'fixed-model-bytes')
before=hashlib.sha256(model.read_bytes()).hexdigest()
config_path=root/'clear.install.json'
config_path.write_text(json.dumps({'schema_version':1,'installation_id':str(uuid.uuid4()),
 'mode':'single','paths':{name:str(path) for name,path in paths.items()},
 'resources':{'ocr_models':'assets/ocr/models'},
 'network':{'transport':'loopback-http','listen':'127.0.0.1','public_origin':'http://127.0.0.1:18889',
 'portal_port':18889,'worker_port':18890,'postgres_port':15432},'policy':{'allowed_drives':['F:']}}))
os.environ['CLEAR_INSTALL_CONFIG']=str(config_path)
os.environ['CLEAR_PADDLE_TEMP']=str(paths['paddle_temp'])
config=load_install(config_path)
def junction(link,target):
 subprocess.run(['cmd.exe','/c','mklink','/J',str(link),str(target)],check=True,capture_output=True)
def rejected(action,exception=PermissionError):
 try:action()
 except exception:return True
 return False
'''
    environment = {**os.environ, 'CLEAR_IO_GUARD': '0'}
    environment.pop('CLEAR_INSTALL_CONFIG', None)
    result = subprocess.run([sys.executable, '-I', '-B', '-c', setup + body,
        str(platform), str(ocr), str(tmp_path)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads(result.stdout)
    assert values and all(values.values()), values
    return values


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows Paddle junction cleanup')
@pytest.mark.parametrize('exceptional', [False, True])
def test_guard_allows_registered_paddle_alias_cleanup_without_model_writes(tmp_path, exceptional):
    _windows_case(tmp_path, r'''
from ocr_workbench.windows_paths import ascii_model_directory
other=paths['assets']/'other-models';other.mkdir()
install(config)
class StopSample(Exception):pass
checks={}
try:
 with ascii_model_directory(models) as alias:
  checks['real_junction']=alias.is_junction() and alias.resolve()==models
  checks['read_model']=(alias/'model.bin').read_bytes()==b'fixed-model-bytes'
  checks['write_denied']=rejected(lambda:(alias/'model.bin').write_bytes(b'bad'))
  checks['remove_model_denied']=rejected(lambda:os.remove(alias/'model.bin'))
  checks['chmod_denied']=rejected(lambda:os.chmod(alias/'model.bin',0o600))
  checks['utime_denied']=rejected(lambda:os.utime(alias/'model.bin',(1,1)))
  checks['wrong_target_denied']=rejected(lambda:require_model_mapping(alias.parent/'models',other),ValueError)
  checks['wrong_location_denied']=rejected(lambda:require_model_mapping(paths['data']/'offlineocr-paddle-test/models',models),ValueError)
  if EXCEPTIONAL:raise StopSample()
except StopSample:pass
checks['alias_and_parent_removed']=not alias.exists() and not alias.parent.exists()
checks['model_unchanged']=model.is_file() and hashlib.sha256(model.read_bytes()).hexdigest()==before
print(json.dumps(checks))
'''.replace('EXCEPTIONAL', str(exceptional)))


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows junction parent escape')
def test_guard_keeps_parent_junction_and_ambiguous_removal_blocked(tmp_path):
    try:
        _windows_case(tmp_path, r'''
empty=models/'empty';empty.mkdir()
nested=models/'nested-alias';junction(nested,empty)
parent=paths['cache']/'parent';junction(parent,models)
leaf=paths['cache']/'leaf';junction(leaf,models)
outside=root/'outside-parent';junction(outside,paths['cache'])
install(config)
checks={
 'parent_write_denied':rejected(lambda:(parent/'model.bin').write_bytes(b'bad')),
 'parent_remove_file_denied':rejected(lambda:os.remove(parent/'model.bin')),
 'parent_remove_directory_denied':rejected(lambda:os.rmdir(parent/'empty')),
 'parent_remove_nested_junction_denied':rejected(lambda:os.rmdir(parent/'nested-alias')),
 'outside_lexical_entry_denied':rejected(lambda:os.rmdir(outside/'leaf')),
}
for index,suffix in enumerate(('\\.','\\..','\\.\\','\\..\\')):
 checks['ambiguous_'+str(index)]=rejected(lambda:os.rmdir(str(leaf)+suffix))
checks['all_targets_retained']=empty.is_dir() and nested.is_junction() and leaf.is_junction()
checks['model_unchanged']=hashlib.sha256(model.read_bytes()).hexdigest()==before
os.rmdir(leaf)
os.rmdir(parent)
checks['owned_entries_removed']=not leaf.exists() and not parent.exists() and models.is_dir()
print(json.dumps(checks))
''')
    finally:
        # The audit hook ends with the child. Remove only our known F: entries.
        for relative in ('cache/parent', 'cache/leaf', 'outside-parent', '程序 models/assets/ocr/models/nested-alias'):
            link = tmp_path / relative
            if link.is_junction():
                link.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows cross-drive junction target')
def test_guard_removes_owned_junction_to_existing_other_drive_without_changing_target(tmp_path):
    if Path(os.environ['SystemRoot']).drive.casefold() == tmp_path.drive.casefold():
        pytest.skip('This host has no separate existing Windows system drive target')
    try:
        _windows_case(tmp_path, r'''
target=Path(os.environ['SystemRoot'])
sentinel=target/'System32/kernel32.dll'
stamp=sentinel.stat()
link=paths['cache']/'system-drive-alias';junction(link,target)
install(config)
os.rmdir(link)
after=sentinel.stat()
print(json.dumps({'entry_removed':not link.exists(),'target_retained':target.is_dir(),
 'system_file_unchanged':(stamp.st_ino,stamp.st_size,stamp.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns),
 'model_unchanged':hashlib.sha256(model.read_bytes()).hexdigest()==before}))
''')
    finally:
        link = tmp_path / 'cache/system-drive-alias'
        if link.is_junction():
            link.rmdir()


def test_isolation_denies_old_reads_and_writes_and_can_bind_a_new_data_root(tmp_path):
    source = Path(__file__).resolve().parents[2]
    code = '''import json,sys,sqlite3
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from packages.runtime.config import WRITE_ROOTS
from packages.runtime.io_guard import install
root=Path(sys.argv[2]);old=root/'old-development';first=root/'first';second=root/'second'
for path in (old,first,second):path.mkdir()
(old/'input.txt').write_text('historical')
dbpath=root/'outside.sqlite3'
with sqlite3.connect(dbpath) as db:db.execute('CREATE TABLE sample(value)');db.execute('INSERT INTO sample VALUES(1)')
install(SimpleNamespace(roots={key:first for key in WRITE_ROOTS},policy={'deny_read_roots':[str(old)]}))
def rejected(action):
 try:action()
 except PermissionError:return True
 return False
denied_read=rejected(lambda:(old/'input.txt').read_text())
denied_write=rejected(lambda:(root/'escape.txt').write_text('bad'))
(first/'allowed.txt').write_text('ok')
with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:readonly=db.execute('SELECT value FROM sample').fetchone()[0]==1
install(SimpleNamespace(roots={key:second for key in WRITE_ROOTS},policy={'deny_read_roots':[str(old)]}))
old_write=rejected(lambda:(first/'after.txt').write_text('bad'))
(second/'allowed.txt').write_text('new')
print(json.dumps(dict(denied_read=denied_read,denied_write=denied_write,readonly_sqlite=readonly,old_write_denied=old_write)))
'''
    environment = {**os.environ, 'CLEAR_IO_GUARD': '0'}
    environment.pop('CLEAR_INSTALL_CONFIG', None)
    result = subprocess.run([sys.executable,'-I','-B','-c',code,str(source),str(tmp_path)],
        cwd=tmp_path,env=environment,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    assert all(json.loads(result.stdout).values())
    assert not (tmp_path/'escape.txt').exists()
    assert not (tmp_path/'first/after.txt').exists()

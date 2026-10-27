import json
import os
from pathlib import Path
import subprocess
import sys


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

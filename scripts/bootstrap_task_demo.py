"""Prepare non-business demo credentials without emitting secrets in logs."""
import json
import os
from pathlib import Path
import secrets
import subprocess
from workbench_platform.tasks.database import engine_for,migrate
from workbench_platform.tasks.store import Ledger

root=Path(os.environ['CLEAR_TASK_ROOT']).resolve()
if root.drive.upper()!='F:':raise ValueError('Use F storage')
engine=engine_for();migrate(engine)
path=root/'runs/demo-login-private.json'
if not path.exists():
    password=secrets.token_urlsafe(24)
    user=Ledger(engine).create_user('clear_admin','Clear 管理员',password,admin=True)
    workspace=Ledger(engine).create_workspace(user,'任务持久化验证项目')
    path.write_text(json.dumps({'username':user['username'],'password':password,'workspace_id':workspace['workspace_id']},ensure_ascii=False,indent=2),'utf-8')
    if os.name=='nt':
        import getpass
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',getpass.getuser()+':(F)'],capture_output=True,check=True)
print('Demo login is saved in the private F-drive file; no business data was used.')

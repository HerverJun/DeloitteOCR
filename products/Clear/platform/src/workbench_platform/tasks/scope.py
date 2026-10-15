"""Resolve unscoped legacy resource URLs from server-owned SQLite identities."""
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .store import LedgerError

def lookup(root,relative,query,identity):
    path=Path(root)/relative
    if not path.is_file(): raise LedgerError('资源不存在',404)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        row=db.execute(query,(identity,)).fetchone()
    if not row or not row[0]: raise LedgerError('资源未关联项目，需管理员整理',403)
    return row[0]

def resolve(root,path,payload=None,query_string=''):
    payload=payload or {}; query=parse_qs(query_string)
    explicit=set()
    found=re.match(r'/api/platform/v1/workspaces/([A-Za-z0-9_-]+)(?:/|$)',path)
    if found: explicit.add(found[1])
    for value in (payload.get('workspace_id'),*(query.get('workspace_id',[]))):
        if value: explicit.add(value)
    rules=[
      (r'/api/platform/v1/data/sessions/([A-Za-z0-9_-]+)(?:/|$)','data-workbench/sessions.sqlite3','SELECT workspace_id FROM sessions WHERE id=?'),
      (r'/api/platform/v1/batches/([A-Za-z0-9_-]+)(?:/|$)','batches/batches.sqlite3','SELECT workspace_id FROM batches WHERE batch_id=?'),
      (r'/api/platform/v1/audit-projects/([A-Za-z0-9_-]+)(?:/|$)','audit/audit.sqlite3','SELECT workspace_id FROM projects WHERE project_id=?'),
      (r'/api/platform/v1/agent/sessions/([A-Za-z0-9_-]+)(?:/|$)','agent/operations.sqlite3','SELECT workspace_id FROM sessions WHERE id=?'),
      (r'/api/platform/v1/agent/runs/([A-Za-z0-9_-]+)(?:/|$)','agent/operations.sqlite3','SELECT workspace_id FROM runs WHERE id=?'),
      (r'/api/platform/v1/agent/decisions/([A-Za-z0-9_-]+)(?:/|$)','agent/operations.sqlite3','SELECT r.workspace_id FROM decisions d JOIN runs r ON d.run_id=r.id WHERE d.id=?'),
    ]
    reserved={'preflight','native-options','options','from-data-session'}
    for pattern,relative,sql in rules:
        match=re.match(pattern,path)
        if match and match[1] not in reserved: explicit.add(lookup(root,relative,sql,match[1]))
    if len(explicit)>1: raise LedgerError('请求资源属于不同项目',403)
    return next(iter(explicit),None)

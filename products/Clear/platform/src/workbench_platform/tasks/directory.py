"""Bind a business directory to its central database and reject wrong readers."""
import json
import os
from .database import SCHEMA_REVISION

def identity(engine):
    return {'format':'clear-central-directory-v1','provider':'postgresql','host':engine.url.host,'port':engine.url.port or 5432,'database':engine.url.database,'schema_revision':SCHEMA_REVISION}

def bind_directory(engine,root):
    path=root/'central-db-identity.json';expected=identity(engine)
    if path.is_file():
        saved=json.loads(path.read_text('utf-8'))
        if any(saved.get(name)!=expected[name] for name in ('format','provider','host','port','database')):
            raise RuntimeError('This data directory belongs to another central database; use a verified restore')
    temporary=path.with_suffix('.new')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(expected,stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)
    from workbench_platform.resources.reader_features import require_features
    require_features(root,'task-ledger-pg-v1')

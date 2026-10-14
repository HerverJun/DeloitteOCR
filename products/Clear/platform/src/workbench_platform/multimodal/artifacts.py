from contextlib import closing
import json
from workbench_platform.artifacts.store import ArtifactUnavailable

def configure(center):
    with closing(center._connect()) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS manual_artifact_bundle(
            bundle_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL)''')

def get(center,workspace,bundle):
    with closing(center._connect()) as db:
        row=db.execute("SELECT body FROM manual_artifact_bundle WHERE workspace_id=? AND bundle_id=? AND status='ready'",(workspace,bundle)).fetchone()
    if row is None:raise ArtifactUnavailable('NOT_FOUND')
    return json.loads(row[0])

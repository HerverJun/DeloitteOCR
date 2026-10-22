"""Publish versioned comparison bundles through the existing artifact center."""
from contextlib import closing
import hashlib
import io
import json

from packages.contracts.models import JobRef, Counts, SourceLocator
from workbench_platform.artifacts.store import ArtifactUnavailable
from .store import canonical, digest


def configure(center):
    with closing(center._connect()) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS comparison_artifact_bundle(
            bundle_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,created_at TEXT NOT NULL,
            status TEXT NOT NULL,body TEXT NOT NULL)''')


def get(center, workspace_id, bundle_id):
    with closing(center._connect()) as db:
        row = db.execute("SELECT body FROM comparison_artifact_bundle WHERE workspace_id=? AND bundle_id=? AND status='ready'",
                         (workspace_id, bundle_id)).fetchone()
    if row is None:
        raise ArtifactUnavailable('NOT_FOUND')
    value = json.loads(row[0])
    service = getattr(center, 'comparison_service', None)
    if service:
        try:
            current = service.store.job(workspace_id, value['comparison_id'])
            if current['revision'] != value['comparison_revision']:
                value['status'] = 'outdated'
        except Exception:
            value['status'] = 'needs_verification'
    return value


def publish(center, value, bodies):
    configure(center)
    bundle_id = value['comparison_id'] + 'r' + str(value['revision'])
    with center.store.publication_lock(bundle_id):
        sources = [s for side in ('left', 'right') for s in value[side]['assets']]
        payload_hash = digest({name: hashlib.sha256(text.encode('utf-8')).hexdigest() for name, text in bodies.items()})
        with closing(center._connect()) as db:
            row = db.execute('SELECT body FROM comparison_artifact_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
            if row:
                bundle = json.loads(row[0])
                if bundle['payload_hash'] != payload_hash or bundle['workspace_id'] != value['workspace_id']:
                    raise ArtifactUnavailable('INPUT_CHANGED')
            else:
                bundle = {'bundle_id': bundle_id, 'workspace_id': value['workspace_id'], 'kind': 'document_comparison',
                          'title': value['title'], 'created_at': value['created_at'], 'status': 'ready',
                          'comparison_id': value['comparison_id'], 'comparison_revision': value['revision'],
                          'payload_hash': payload_hash, 'inputs': [{**s, 'name': s['original_name'], 'revision': s['asset_revision']} for s in sources],
                          'files': []}
                db.execute('INSERT INTO comparison_artifact_bundle VALUES(?,?,?,?,?)',
                           (bundle_id, value['workspace_id'], value['created_at'], 'publishing', canonical(bundle)))
        job = JobRef(schema_version='1.0', platform_job_id=value['comparison_id'], app_id='data', instance_id='platform_comparison',
                     native_job_id=value['comparison_id'], operation_id=bundle_id, input_manifest_hash=digest(sources),
                     native_status='succeeded', status='succeeded', stage='comparison_report',
                     counts=Counts(schema_version='1.0', total=1, succeeded=1, failed=0, cancelled=0, pending=0),
                     capabilities=[], last_event_seq=0, updated_at=value['created_at'].replace('+00:00', 'Z'))
        refs = [SourceLocator(schema_version='1.0', kind='file', asset_id=s['asset_id'], asset_revision=s['asset_revision'],
                              blob_hash=s['blob_hash'], precision='unknown') for s in sources]
        for name, text in bodies.items():
            old = next((entry for entry in bundle['files'] if entry['name'] == name), None)
            payload = text.encode('utf-8')
            checksum = hashlib.sha256(payload).hexdigest()
            if old:
                artifact = center.store.get(value['workspace_id'], old['artifact_id'])
                if artifact.blob_hash != checksum:
                    raise ArtifactUnavailable('INPUT_CHANGED')
                continue
            media = 'text/html' if name.endswith('.html') else 'text/markdown' if name.endswith('.md') else 'application/json'
            def register(db, artifact):
                entry = {'name': name, 'artifact_id': artifact.artifact_id, 'revision': artifact.revision,
                         'media_type': media, 'size_bytes': len(payload), 'availability': artifact.availability}
                bundle['files'].append(entry)
                db.execute('UPDATE comparison_artifact_bundle SET body=? WHERE bundle_id=?', (canonical(bundle), bundle_id))
            center.store.publish_managed(value['workspace_id'], io.BytesIO(payload), expected_hash=checksum,
                expected_size=len(payload), job_ref=job, source_refs=refs, media_type=media,
                application_version='0.18.0', rule_or_model_version='clear-comparison-html-v1', on_insert=register)
        with closing(center._connect()) as db:
            db.execute("UPDATE comparison_artifact_bundle SET status='ready' WHERE bundle_id=?", (bundle_id,))
        return bundle['files']

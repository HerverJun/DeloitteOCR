"""Immutable inspection report bundles using the existing verified artifact store."""
from contextlib import closing
import io
import json
from pathlib import Path
from workbench_platform.artifacts.store import ArtifactUnavailable
from workbench_platform.batches.store import encoded, fingerprint
from packages.contracts.forensics import ForensicBundle


def configure(center):
    with closing(center._connect()) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS forensic_artifact_bundle(
            bundle_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,created_at TEXT NOT NULL,
            status TEXT NOT NULL,body TEXT NOT NULL)''')


def get(center, workspace_id, bundle_id):
    with closing(center._connect()) as db:
        row = db.execute("SELECT body FROM forensic_artifact_bundle WHERE workspace_id=? AND bundle_id=? AND status='ready'",
                         (workspace_id, bundle_id)).fetchone()
    if row is None:
        raise ArtifactUnavailable('NOT_FOUND')
    return ForensicBundle.model_validate(json.loads(row[0])).model_dump(mode='json')



def publication_versions(item):
    """Use the report's executed code identity; old reports keep their old meaning."""
    report=item.get('report') or {}
    runtime_record=report.get('runtime')
    runtime=runtime_record.get('toolbox') if isinstance(runtime_record,dict) else None
    execution=report.get('toolbox_execution')
    if execution is None and (runtime is None or isinstance(runtime,dict) and runtime.get('version')=='0.1.0'):
        return 'forensic-toolbox-0.1.0',fingerprint(item['tools'])
    from .service import source_identity
    try:
        identity=source_identity(report['toolbox_source'])
    except (KeyError,TypeError,ValueError):
        raise ArtifactUnavailable('INPUT_CHANGED') from None
    expected=fingerprint(identity)
    if (not isinstance(execution,dict) or not isinstance(runtime,dict)
            or execution.get('source_identity_sha256')!=expected
            or execution.get('version')!=identity['version'] or runtime.get('version')!=identity['version']
            or execution.get('kind')!=identity['kind']
            or any(execution.get(key)!=value for key,value in identity.items())
            or not isinstance(runtime.get('path'),str) or not isinstance(execution.get('runtime_path'),str)
            or Path(runtime['path']).resolve()!=Path(execution['runtime_path']).resolve()):
        raise ArtifactUnavailable('INPUT_CHANGED')
    if identity['kind']=='legacy':return 'forensic-toolbox-0.1.0',fingerprint(item['tools'])
    return 'forensic-toolbox-'+identity['version'],fingerprint({'tools':item['tools'],'toolbox_source_identity':expected})


def publish(service, workspace_id, inspection_id):
    item = service.get(workspace_id, inspection_id)
    if item['status'] not in {'succeeded', 'partial'}:
        raise ArtifactUnavailable('NOT_FOUND')
    center = service.app.state.artifact_center
    with center.lock, center.store.publication_lock(inspection_id):
        try:
            return get(center, workspace_id, inspection_id)
        except ArtifactUnavailable:
            pass
        application_version, rule_version = publication_versions(item)
        with closing(center._connect()) as db:
            row = db.execute('SELECT body FROM forensic_artifact_bundle WHERE bundle_id=?', (inspection_id,)).fetchone()
            bundle = json.loads(row[0]) if row else {'schema_version': '1.0', 'bundle_id': inspection_id, 'workspace_id': workspace_id,
                'kind': 'image_forensics', 'title': item['original_name'] + ' · 图像取证',
                'inspection_id': inspection_id, 'report_hash': item['report_hash'], 'created_at': item['created_at'],
                'inputs': [{'role': 'image', 'name': item['original_name'], 'asset_id': item['asset_id'], 'revision': item['asset_revision']}],
                'files': []}
            if row is None:
                db.execute('INSERT INTO forensic_artifact_bundle VALUES(?,?,?,?,?)',
                           (inspection_id, workspace_id, item['created_at'], 'publishing', encoded(bundle)))
        source = {'schema_version': '1.0', 'kind': 'file', 'asset_id': item['asset_id'],
                  'asset_revision': item['asset_revision'], 'blob_hash': item['blob_hash'], 'precision': 'exact'}
        job = {'schema_version': '1.0', 'platform_job_id': inspection_id, 'app_id': 'imageForensics',
               'instance_id': 'localForensics', 'native_job_id': inspection_id, 'operation_id': item['request_id'],
               'input_manifest_hash': fingerprint(source), 'native_status': 'completed', 'status': 'succeeded',
               'stage': 'evidence_report', 'counts': {'schema_version': '1.0', 'total': 1, 'succeeded': 1, 'failed': 0, 'cancelled': 0, 'pending': 0},
               'capabilities': [], 'last_event_seq': 0, 'updated_at': item['updated_at']}
        for record in item['files']:
            if any(file['name'] == record['name'] for file in bundle['files']):
                continue
            content, media = service.file(workspace_id, inspection_id, record['name'])
            def insert(db, artifact):
                bundle['files'].append({'name': record['name'], 'artifact_id': artifact.artifact_id,
                    'revision': artifact.revision, 'blob_hash': artifact.blob_hash,
                    'size_bytes': artifact.size_bytes, 'media_type': artifact.media_type, 'availability': 'available'})
                db.execute('UPDATE forensic_artifact_bundle SET body=? WHERE bundle_id=?', (encoded(bundle), inspection_id))
            center.store.publish_managed(workspace_id, io.BytesIO(content), expected_hash=record['sha256'],
                expected_size=record['size_bytes'], job_ref=job, source_refs=[source], media_type=media,
                application_version=application_version, rule_or_model_version=rule_version, on_insert=insert)
        ForensicBundle.model_validate(bundle)
        with closing(center._connect()) as db:
            db.execute("UPDATE forensic_artifact_bundle SET status='ready' WHERE bundle_id=?", (inspection_id,))
        return bundle

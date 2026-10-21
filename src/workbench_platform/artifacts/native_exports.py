"""Managed exports from verified native objects using the shared artifact store."""
from contextlib import closing
import hashlib
import io
import json
from workbench_platform.application_services.identity import canonical, digest
from workbench_platform.artifacts.store import ArtifactUnavailable
from packages.contracts.models import JobRef, Counts


def configure(center):
    with closing(center._connect()) as db:
        db.execute('CREATE TABLE IF NOT EXISTS native_export_bundles (bundle_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)')


def get(center, workspace_id, bundle_id):
    with closing(center._connect()) as db:
        row = db.execute('SELECT payload FROM native_export_bundles WHERE workspace_id=? AND bundle_id=?', (workspace_id, bundle_id)).fetchone()
    if not row:
        raise ArtifactUnavailable('NOT_FOUND')
    value = json.loads(row['payload'])
    if value['status'] != 'ready':
        raise ArtifactUnavailable('PUBLISHING')
    return value


def lookup_result(ctx):
    from packages.contracts.agent import AgentRef, AgentToolResult
    from workbench_platform.application_services.references import verify
    try:
        bundle = get(ctx.services.artifact_center, ctx.workspace_id, 'b'+ctx.operation_id[1:])
    except ArtifactUnavailable as error:
        if error.code == 'NOT_FOUND':
            return None
        raise
    reference = AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=bundle['bundle_id'], revision=1)
    verify(reference.model_dump(), ctx)
    return AgentToolResult(status='success', summary='已核实原操作导出的固定成果与文件字节，未重新导出',
        data={'bundle': bundle}, artifact_refs=[reference]).model_dump()


def publish(ctx, title, files, inputs, source_refs, version):
    from .center import _utc
    center = ctx.services.artifact_center
    bundle_id = 'b'+ctx.operation_id[1:]
    signature = digest({'title': title, 'inputs': inputs, 'files': {name: hashlib.sha256(payload).hexdigest() for name, (payload, _) in files.items()}})
    with center.lock:
        with closing(center._connect()) as db:
            row = db.execute('SELECT payload FROM native_export_bundles WHERE bundle_id=?', (bundle_id,)).fetchone()
        if row:
            value = json.loads(row['payload'])
            if value['workspace_id'] != ctx.workspace_id or value['payload_hash'] != signature:
                raise ValueError('原生成果请求身份变化')
            if value['status'] == 'ready':
                return value
        else:
            value = {'bundle_id': bundle_id, 'workspace_id': ctx.workspace_id, 'title': title, 'kind': 'native_export', 'inputs': inputs,
                     'files': [], 'created_at': _utc(), 'status': 'publishing', 'payload_hash': signature}
            with closing(center._connect()) as db:
                db.execute('INSERT INTO native_export_bundles VALUES(?,?,?,?)', (bundle_id, ctx.workspace_id, value['created_at'], canonical(value)))
        job = JobRef(schema_version='1.0', platform_job_id='j'+ctx.operation_id[1:], app_id='platform', instance_id='managed_export',
            native_job_id=ctx.operation_id, operation_id=ctx.operation_id, input_manifest_hash=digest(inputs), native_status='succeeded',
            status='succeeded', stage='原生成果归集', counts=Counts(schema_version='1.0', total=1, succeeded=1, failed=0, cancelled=0, pending=0),
            capabilities=[], last_event_seq=1, updated_at=value['created_at'])
        for name, (payload, media_type) in files.items():
            if '/' in name or '\\' in name or not name or len(name) > 240:
                raise ValueError('导出文件名无效')
            if any(item['name'] == name for item in value['files']):
                continue
            artifact = center.store.publish_managed(ctx.workspace_id, io.BytesIO(payload),
                expected_hash=hashlib.sha256(payload).hexdigest(), expected_size=len(payload), job_ref=job,
                source_refs=source_refs, media_type=media_type, application_version='1.0', rule_or_model_version=version)
            value['files'].append({'name': name, 'artifact_id': artifact.artifact_id, 'revision': artifact.revision,
                'media_type': media_type, 'size_bytes': artifact.size_bytes, 'availability': artifact.availability})
            with closing(center._connect()) as db:
                db.execute('UPDATE native_export_bundles SET payload=? WHERE bundle_id=?', (canonical(value), bundle_id))
        value['status'] = 'ready'
        with closing(center._connect()) as db:
            db.execute('UPDATE native_export_bundles SET payload=? WHERE bundle_id=?', (canonical(value), bundle_id))
        return value

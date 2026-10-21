"""Publish editable audit drafts through the managed artifact center."""
from contextlib import closing
import csv
import hashlib
import html
import io
import json

from packages.contracts.models import JobRef, Counts, SourceLocator
from workbench_platform.agent.store import canonical
from workbench_platform.artifacts.store import ArtifactUnavailable


def row(center, workspace_id, bundle_id):
    with closing(center._connect()) as db:
        value = db.execute("SELECT * FROM audit_draft_bundle WHERE workspace_id=? AND bundle_id=? AND status='ready'", (workspace_id, bundle_id)).fetchone()
    if value is None:
        raise ArtifactUnavailable('NOT_FOUND')
    return value


def display(center, value):
    snapshot = json.loads(value['snapshot'])
    outdated = False
    if center.audit_service:
        runtime = getattr(center.audit_service.app.state, 'agent', None)
        with closing(center.audit_service.store.db()) as db:
            for draft in snapshot['drafts']:
                current = db.execute('SELECT revision FROM audit_finding_drafts WHERE draft_id=?', (draft['draft_id'],)).fetchone()
                outdated |= current is None or current['revision'] != draft['revision']
        if runtime:
            from workbench_platform.application_services.context import SessionContext
            task = runtime.tasks.get(snapshot['task_id'])
            ctx = SessionContext(value['workspace_id'], task['id'], '', 0, 0, '', tuple(task['selected_assets']), runtime.services, runtime.store,
                grants=frozenset({'read'}))
            outdated |= snapshot['profile']['revision'] != runtime.tasks.profile(value['workspace_id'])['revision']
            outdated |= snapshot.get('scope_revision', task['scope_revision']) != task['scope_revision'] or bool(task['pending_scope'])
            for draft in snapshot['drafts']:
                try:
                    outdated |= center.audit_service.drafts.get(draft['draft_id'], ctx)['outdated']
                except ValueError:
                    outdated = True
    files = []
    for name, identifier in json.loads(value['files_json']).items():
        file = center.store.get(value['workspace_id'], identifier)
        files.append({'name': name, 'artifact_id': identifier, 'revision': file.revision, 'media_type': file.media_type,
                      'size_bytes': file.size_bytes, 'availability': file.availability})
    inputs = []
    for evidence in snapshot['evidence']:
        for source in evidence.get('source_assets') or evidence['sources']:
            asset = center.registry.get(value['workspace_id'], source['asset_id'])
            member = {'asset_id': source['asset_id'], 'revision': source['asset_revision'], 'blob_hash': source['blob_hash'], 'name': asset.original_name, 'role': 'evidence'}
            if member not in inputs:
                inputs.append(member)
    return {'bundle_id': value['bundle_id'], 'workspace_id': value['workspace_id'], 'kind': 'audit_draft',
            'title': snapshot['title'], 'created_at': snapshot['created_at'], 'task_id': snapshot['task_id'],
            'status': 'outdated' if outdated or any(draft['outdated'] for draft in snapshot['drafts']) else 'draft',
            'inputs': inputs, 'files': files, 'author': snapshot['author'], 'snapshot_hash': value['snapshot_hash']}


def render(snapshot):
    lines = ['# ' + snapshot['title'], '', '> 可编辑审计草稿。事实来自所列证据；解释与建议仍可修订。本文件不包含编制或复核署名。', '',
             '项目：' + snapshot['profile']['workspace']['name'], '审计对象：' + (snapshot['profile']['audit_object'] or '待填写'), '',
             '期间：' + (snapshot['profile']['period_start'] or '待填写') + ' 至 ' + (snapshot['profile']['period_end'] or '待填写'), '']
    for draft in snapshot['drafts']:
        lines += ['## ' + draft['title'], '', '版本：' + str(draft['revision']) + ' · 状态：' + draft['status'], '']
        if draft['outdated']:
            lines += ['需更新：' + '；'.join(draft['outdated_reasons']), '']
        for key, title in [('facts', '观察事实'), ('interpretation', '解释与判断'), ('rule', '采用口径'), ('missing_information', '待补信息'), ('recommendations', '处理建议')]:
            lines += ['### ' + title, '', draft[key] or '未填写', '']
        lines += ['关联证据：' + ('、'.join(draft['evidence_ids']) or '未取得'), '']
    lines += ['## 证据来源', '']
    for evidence in snapshot['evidence']:
        lines += ['- ' + evidence['evidence_id'] + ' · ' + evidence['summary'], '  固定结果：' + canonical(evidence['reference']),
                  '  来源位置：' + evidence['location_url'], '  快照：' + evidence['snapshot_hash'], '']
    markdown = '\n'.join(lines)
    # Self-contained HTML remains editable when opened in a browser. All source
    # and model text is escaped; no generated HTML or scripts are executed.
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>' + html.escape(snapshot['title']) + '</title><style>body{max-width:960px;margin:48px auto;padding:0 24px;font:16px/1.8 "Microsoft YaHei",sans-serif;color:#253124}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><main contenteditable="true"><pre>' + html.escape(markdown) + '</pre></main></html>'
    source_file = io.StringIO(newline='')
    writer = csv.writer(source_file)
    writer.writerow(['证据ID', '资料ID', '资料版本', 'SHA256', '工作表', '页', '行', '列', '定位精度'])
    for evidence in snapshot['evidence']:
        for source in evidence['sources']:
            writer.writerow([evidence['evidence_id'], source['asset_id'], source['asset_revision'], source['blob_hash'],
                             "'" + source['sheet_name'] if source.get('sheet_name') else '', source.get('page_number'), source.get('row_number'), source.get('column_number'), source['precision']])
    return {'workpaper.md': markdown.encode('utf-8'), 'workpaper.html': document.encode('utf-8'),
            'findings.json': canonical(snapshot['drafts']).encode('utf-8'), 'evidence.json': canonical(snapshot['evidence']).encode('utf-8'),
            'sources.csv': source_file.getvalue().encode('utf-8-sig')}


def publish(center, bundle_id, request_id, payload_hash, snapshot):
    with center.store.publication_lock(bundle_id):
        with closing(center._connect()) as db:
            existing = db.execute('SELECT * FROM audit_draft_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
            if existing:
                if existing['payload_hash'] != payload_hash or existing['workspace_id'] != snapshot['workspace_id']:
                    raise ArtifactUnavailable('INPUT_CHANGED')
                snapshot = json.loads(existing['snapshot'])
                if existing['status'] == 'ready':
                    return display(center, existing)
            else:
                db.execute('INSERT INTO audit_draft_bundle VALUES(?,?,?,?,?,?,?,?)', (bundle_id, snapshot['workspace_id'], request_id,
                    payload_hash, hashlib.sha256(canonical(snapshot).encode()).hexdigest(), canonical(snapshot), '{}', 'publishing'))
            existing = db.execute('SELECT * FROM audit_draft_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
        mapping = json.loads(existing['files_json'])
        refs = [SourceLocator.model_validate(source) for evidence in snapshot['evidence'] for source in evidence['sources']]
        job = JobRef(schema_version='1.0', platform_job_id=bundle_id, app_id='data', instance_id='platform_audit_draft',
            native_job_id=bundle_id, operation_id=request_id, input_manifest_hash=existing['snapshot_hash'], native_status='succeeded',
            status='succeeded', stage='audit_draft', counts=Counts(schema_version='1.0', total=1, succeeded=1, failed=0, cancelled=0, pending=0),
            capabilities=[], last_event_seq=0, updated_at=snapshot['created_at'].replace('+00:00', 'Z'))
        for name, payload in render(snapshot).items():
            checksum = hashlib.sha256(payload).hexdigest()
            if name in mapping:
                file = center.store.get(snapshot['workspace_id'], mapping[name])
                if (file.blob_hash, file.size_bytes) != (checksum, len(payload)):
                    raise ArtifactUnavailable('INPUT_CHANGED')
                continue
            def register(db, artifact):
                saved = db.execute('SELECT files_json FROM audit_draft_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
                files = json.loads(saved[0]); files[name] = artifact.artifact_id
                db.execute('UPDATE audit_draft_bundle SET files_json=? WHERE bundle_id=?', (canonical(files), bundle_id))
            artifact = center.store.publish_managed(snapshot['workspace_id'], io.BytesIO(payload), expected_hash=checksum, expected_size=len(payload),
                job_ref=job, source_refs=refs, media_type={'md': 'text/markdown', 'html': 'text/html', 'csv': 'text/csv', 'json': 'application/json'}[name.rsplit('.', 1)[1]],
                application_version='0.5.0', rule_or_model_version='audit-draft-v1', on_insert=register)
            mapping[name] = artifact.artifact_id
        with closing(center._connect()) as db:
            db.execute("UPDATE audit_draft_bundle SET status='ready' WHERE bundle_id=?", (bundle_id,))
        return display(center, row(center, snapshot['workspace_id'], bundle_id))

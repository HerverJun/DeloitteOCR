"""Editable findings and workpaper drafts in the existing audit business store."""
from contextlib import closing
from dataclasses import replace
import json

from workbench_platform.agent.store import canonical, digest, now, Conflict
from workbench_platform.application_services.references import verify, asset_allowed
from .direct import snapshot


def reference_changes(reference, ctx):
    value = verify(reference, ctx)
    if reference.get('application') == 'data' and reference['kind'] in {'table', 'result'}:
        current = ctx.services.data_workbench.get(reference['parent_id'])['state']
        if reference['kind'] == 'table' and reference['object_id'] not in {table['revision'] for table in current['tables'].values()}:
            return ['数据表已产生新版本，仍引用原版本']
        if reference['kind'] == 'result' and reference['object_id'] != current['result_id']:
            return ['核对结果已更新，仍引用原结果']
    if reference.get('application') == 'ocr' and reference['kind'] == 'result' and value['current_revision'] != reference['revision']:
        return ['OCR结果已编辑，仍引用原版本']
    return []


class AuditDrafts:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        with self.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS direct_audit_evidence(evidence_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,task_id TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_finding_drafts(draft_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,task_id TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_draft_commands(request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,response TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_draft_history(draft_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    body TEXT NOT NULL,PRIMARY KEY(draft_id,revision));
            ''')

    def bind(self, args, ctx):
        if args.reference.kind == 'evidence':
            return verify(args.reference.model_dump(mode='json'), ctx)
        value = snapshot(args, ctx)
        with self.store.transaction() as db:
            old = db.execute('SELECT body FROM direct_audit_evidence WHERE evidence_id=?', (value['evidence_id'],)).fetchone()
            if old:
                return json.loads(old['body'])
            if ctx.run_id:
                ctx.current()
            db.execute('INSERT INTO direct_audit_evidence VALUES(?,?,?,?)', (value['evidence_id'], ctx.workspace_id, ctx.session_id, canonical(value)))
        return value

    def evidence(self, evidence_id, ctx):
        with closing(self.store.db()) as db:
            row = db.execute('SELECT body FROM direct_audit_evidence WHERE evidence_id=? AND workspace_id=?', (evidence_id, ctx.workspace_id)).fetchone()
        if row is None:
            raise ValueError('证据不属于当前项目')
        value = json.loads(row['body'])
        content = {key: value[key] for key in ('reference', 'observation', 'sources')}
        if value.get('snapshot_version', 1) == 2:
            content.update(source_count=value['source_count'], source_assets=value['source_assets'])
            if digest(content) != value['snapshot_hash']:
                raise ValueError('固定证据内容哈希变化')
            sources = value['source_assets']
        else:
            # Old snapshots hashed all locators but saved only the first 200.
            # Reconstruct the original window only when its native version is
            # still verifiable; incomplete lineage never authorizes a read.
            if value['source_count'] > len(value['sources']):
                from packages.contracts.audit import DirectEvidenceRequest
                window = value['observation'].get('window', {})
                rebuilt = snapshot(DirectEvidenceRequest(reference=value['reference'],
                    offset=window.get('offset', 0), limit=window.get('limit', 20)), ctx, legacy=True)
                if rebuilt['snapshot_hash'] != value['snapshot_hash']:
                    raise ValueError('旧证据完整来源不可核实，请重新绑定')
                verify(value['reference'], ctx)
            elif digest(content) != value['snapshot_hash']:
                raise ValueError('固定证据内容哈希变化')
            sources = value['sources']
            if not sources:
                verify(value['reference'], ctx)
        registry = ctx.services.asset_library[0]
        for source in sources:
            if not asset_allowed(ctx, source['asset_id']):
                raise ValueError('固定证据来源超出当前资料范围')
            with registry.stage_verified(ctx.workspace_id, source['asset_id'],
                    revision=source['asset_revision'], blob_hash=source['blob_hash']):
                pass
        try:
            reasons = reference_changes(value['reference'], ctx)
        except ValueError:
            reasons = ['原生结果已更新或不可用，以下为保存时的固定证据']
        return {**value, 'stale': bool(reasons), 'outdated_reasons': reasons}

    def evidence_status(self, evidence_id, ctx):
        return self.evidence(evidence_id, ctx)

    def _replay(self, db, request_id, command):
        row = db.execute('SELECT * FROM audit_draft_commands WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if row['fingerprint'] != digest(command):
                raise Conflict('原请求不能替换发现内容')
            return json.loads(row['response'])

    def write(self, args, ctx, author):
        command = {'workspace_id': ctx.workspace_id, 'task_id': ctx.session_id, **args.model_dump()}
        with self.store.transaction() as db:
            old = self._replay(db, args.request_id, command)
            if old:
                return old
        evidence = [self.evidence(identifier, ctx) for identifier in args.evidence_ids]
        if args.facts.strip() and not evidence:
            raise ValueError('事实部分必须关联实际证据；未取得资料时请记录待补信息')
        with self.store.transaction() as db:
            old = self._replay(db, args.request_id, command)
            if old:
                return old
            identifier = args.draft_id or 'f' + digest([ctx.workspace_id, ctx.session_id, args.request_id])[:40]
            row = db.execute('SELECT * FROM audit_finding_drafts WHERE draft_id=?', (identifier,)).fetchone()
            if row and (row['workspace_id'], row['task_id']) != (ctx.workspace_id, ctx.session_id):
                raise ValueError('发现不属于本任务')
            revision = row['revision'] if row else 0
            if revision != args.expected_revision or args.draft_id is not None and not row:
                raise Conflict('发现已更新，请读取最新版本后修订')
            if author['kind'] == 'agent':
                ctx.current()
            value = {key: item for key, item in args.model_dump().items() if key not in {'request_id', 'expected_revision'}}
            value.update(draft_id=identifier, workspace_id=ctx.workspace_id, task_id=ctx.session_id,
                         revision=revision + 1, author=author, evidence_hashes={item['evidence_id']: item['snapshot_hash'] for item in evidence},
                         scope_revision=ctx.services.agent.tasks.get(ctx.session_id)['scope_revision'],
                         project_context_revision=ctx.services.agent.tasks.profile(ctx.workspace_id)['revision'],
                         task_goal=ctx.services.agent.tasks.get(ctx.session_id)['goal'], updated_at=now())
            db.execute('INSERT OR REPLACE INTO audit_finding_drafts VALUES(?,?,?,?,?)', (identifier, ctx.workspace_id, ctx.session_id, revision + 1, canonical(value)))
            db.execute('INSERT INTO audit_draft_history VALUES(?,?,?)', (identifier, revision + 1, canonical(value)))
            db.execute('INSERT INTO audit_draft_commands VALUES(?,?,?)', (args.request_id, digest(command), canonical(value)))
        return value

    def get(self, draft_id, ctx):
        with closing(self.store.db()) as db:
            row = db.execute('SELECT body FROM audit_finding_drafts WHERE draft_id=? AND workspace_id=?', (draft_id, ctx.workspace_id)).fetchone()
        if row is None:
            raise ValueError('发现不存在')
        value = json.loads(row['body'])
        reasons = []
        task = ctx.services.agent.tasks.get(value['task_id'])
        source_ctx = replace(ctx, session_id=value['task_id'], selected_assets=tuple(task['selected_assets']))
        for identifier in value['evidence_ids']:
            try:
                evidence = self.evidence(identifier, source_ctx)
                reasons.extend(evidence['outdated_reasons'])
            except ValueError:
                reasons.append('证据结果已修订或超出当前资料范围，请重新读取')
        if value['scope_revision'] != task['scope_revision'] or task['pending_scope']:
            reasons.append('任务资料范围已变更')
        if value.get('project_context_revision', 0) != ctx.services.agent.tasks.profile(ctx.workspace_id)['revision']:
            reasons.append('项目背景或审计口径已变更')
        if value.get('task_goal', task['goal']) != task['goal']:
            reasons.append('任务目标已变更')
        source = {key: value[key] for key in ('task_id', 'draft_id', 'revision', 'scope_revision', 'project_context_revision')}
        source['snapshot_hash'] = digest(value)
        return {**value, 'outdated': bool(reasons), 'outdated_reasons': list(dict.fromkeys(reasons)),
                'transfer_source': source,
                'formal_links': self.service.source_links(ctx.workspace_id, draft_id, value['revision'], source['snapshot_hash'])}

    def list(self, ctx, task_id=None, limit=100):
        with closing(self.store.db()) as db:
            query = 'SELECT draft_id FROM audit_finding_drafts WHERE workspace_id=?'
            params = [ctx.workspace_id]
            if task_id:
                query += ' AND task_id=?'; params.append(task_id)
            total = db.execute('SELECT count(*) FROM (' + query + ')', params).fetchone()[0]
            rows = db.execute(query + ' ORDER BY rowid DESC LIMIT ?', (*params, limit)).fetchall()
        return {'items': [self.get(row['draft_id'], ctx) for row in rows], 'total': total}

    def export(self, args, ctx, author):
        command = {'workspace_id': ctx.workspace_id, 'task_id': ctx.session_id, **args.model_dump()}
        with self.store.transaction() as db:
            old = self._replay(db, args.request_id, command)
            if old:
                return ctx.services.artifact_center.get(ctx.workspace_id, old['bundle_id'])
        bundle_id = 'b' + digest([ctx.workspace_id, args.request_id])[:40]
        with closing(ctx.services.artifact_center._connect()) as db:
            reserved = db.execute('SELECT payload_hash,snapshot FROM audit_draft_bundle WHERE bundle_id=?', (bundle_id,)).fetchone()
        if reserved:
            if reserved['payload_hash'] != digest(command):
                raise Conflict('原底稿请求不能替换所选发现')
            content = json.loads(reserved['snapshot'])
        else:
            content = None
        if content is None:
            return self._new_export(args, ctx, author, command, bundle_id)
        return self._publish_export(args, ctx, command, bundle_id, content)

    def _new_export(self, args, ctx, author, command, bundle_id):
        drafts = [self.get(identifier, ctx) for identifier in args.draft_ids]
        evidence = {identifier: self.evidence(identifier, ctx) for draft in drafts for identifier in draft['evidence_ids']}
        profile = ctx.services.agent.tasks.profile(ctx.workspace_id)
        content = {'title': args.title, 'workspace_id': ctx.workspace_id, 'task_id': ctx.session_id, 'drafts': drafts,
                   'evidence': list(evidence.values()), 'profile': profile, 'author': author, 'created_at': now(),
                   'scope_revision': ctx.services.agent.tasks.get(ctx.session_id)['scope_revision']}
        # Artifact publication reserves and preserves its snapshot before files
        # are written. A retried request reuses that snapshot after a crash.
        return self._publish_export(args, ctx, command, bundle_id, content)

    def _publish_export(self, args, ctx, command, bundle_id, content):
        result = ctx.services.artifact_center.publish_audit_draft(bundle_id, args.request_id, digest(command), content)
        with self.store.transaction() as db:
            old = self._replay(db, args.request_id, command)
            if old:
                return result
            db.execute('INSERT INTO audit_draft_commands VALUES(?,?,?)', (args.request_id, digest(command), canonical({'bundle_id': result['bundle_id']})))
        return result

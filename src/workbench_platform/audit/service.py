"""Audit business workflow over actual fixed processing-batch evidence."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import uuid

from pydantic import ValidationError
from packages.contracts import audit as contract
from workbench_platform.artifacts.store import ArtifactUnavailable
from workbench_platform.batches.store import encoded, fingerprint, now
from workbench_platform.data_api.service import DataError
from .evidence import batch_item_evidence
from .store import AuditStore

WIRE = {'schema_version': '1.0', 'audit_version': '1.0'}


def identity(prefix):
    return prefix + uuid.uuid4().hex


def validated(model, payload):
    try:
        return model.model_validate(payload).model_dump(mode='json')
    except ValidationError:
        raise DataError('审计参数无效，请检查内容、所见版本和本机操作人') from None


class AuditService:
    def __init__(self, app, root: Path):
        self.app = app
        self.store = AuditStore(root)
        self.batches = app.state.batch_service
        self.center = app.state.artifact_center
        self.center.audit_service = self
        from .todos import WorkspaceTodoService
        self.todos = WorkspaceTodoService(self)

    def workspace(self, workspace_id):
        self.batches.workspace(workspace_id)

    def _forensic_lock(self):
        service = getattr(self.app.state, 'forensic_service', None)
        return service.lock if service is not None else nullcontext()

    @staticmethod
    def _records_current(project, entity):
        records = entity['records']
        return (set(records) == {'prepared', 'reviewed'} and all(
            record['content_revision'] == entity['content_revision']
            and record['project_context_revision'] == project['project_context_revision']
            for record in records.values())
            and records['reviewed']['created_at'] >= records['prepared']['created_at'])

    def _display(self, project, kind, entity_id=None):
        if kind == 'project':
            result = {key: project[key] for key in ('project_id', 'workspace_id', 'revision', 'content_revision',
                       'project_context_revision', 'project', 'created_at', 'updated_at')}
            entity = project
            result.update(procedure_count=len(project['procedures']), finding_count=len(project['findings']))
            model = contract.AuditProject
        else:
            entity = self._entity(project, kind, entity_id)
            result = {key: entity[key] for key in ('content_revision', kind, 'created_at', 'updated_at')}
            result.update(project_id=project['project_id'], project_revision=project['revision'],
                          project_context_revision=project['project_context_revision'])
            result[kind + '_id'] = entity_id
            if kind == 'procedure':
                result['batches'] = entity['batches']
                model = contract.AuditProcedure
            else:
                result.update(procedure_id=entity['procedure_id'], evidence_refs=entity['evidence_refs'])
                model = contract.AuditFinding
        result.update(**WIRE, records=list(entity['records'].values()),
                      records_current=self._records_current(project, entity))
        return model.model_validate(result).model_dump(mode='json')

    @staticmethod
    def _entity(project, kind, entity_id):
        if kind == 'project':
            if entity_id != project['project_id']:
                raise DataError('记录不属于此审计项目', 404)
            return project
        entity = project[kind + 's'].get(entity_id)
        if entity is None:
            raise DataError('程序或调查事项不属于此审计项目', 404)
        return entity

    def get(self, project_id, kind='project', entity_id=None):
        with closing(self.store.db()) as db:
            project = self.store.project(db, project_id)
        self.workspace(project['workspace_id'])
        return self._display(project, kind, entity_id)

    @staticmethod
    def _page(items, binding, limit, cursor, snapshot_token):
        token = 's' + fingerprint({'binding': binding, 'items': items})
        if snapshot_token is not None and token != snapshot_token:
            raise DataError('审计列表快照已变化，请刷新列表', 409)
        offset = 0
        if cursor:
            if snapshot_token is None or not cursor.startswith(token + '_'):
                raise DataError('分页游标不属于所见审计快照', 409)
            try:
                offset = int(cursor[len(token) + 1:])
            except ValueError:
                raise DataError('审计分页游标无效') from None
            if offset < 0 or offset > len(items):
                raise DataError('审计分页游标无效')
        return {**WIRE, 'total': len(items), 'items': items[offset:offset + limit],
                'snapshot_token': token, 'next_cursor': token + '_' + str(offset + limit)
                if offset + limit < len(items) else None}

    def projects(self, workspace_id, limit, cursor, snapshot_token):
        self.workspace(workspace_id)
        with closing(self.store.db()) as db:
            values = [json.loads(row[0]) for row in db.execute('SELECT body FROM projects WHERE workspace_id=? '
                      'ORDER BY project_id', (workspace_id,))]
        result = self._page([self._display(value, 'project') for value in values], workspace_id,
                            limit, cursor, snapshot_token)
        return contract.AuditProjectPage.model_validate({**result, 'workspace_id': workspace_id}).model_dump(mode='json')

    def entities(self, project_id, kind, limit, cursor, snapshot_token, procedure_id=None):
        with closing(self.store.db()) as db:
            project = self.store.project(db, project_id)
        self.workspace(project['workspace_id'])
        if procedure_id:
            self._entity(project, 'procedure', procedure_id)
        items = [self._display(project, kind, entity_id) for entity_id in sorted(project[kind + 's'])
                 if not procedure_id or project[kind + 's'][entity_id].get('procedure_id') == procedure_id]
        result = self._page(items, [project_id, kind, procedure_id], limit, cursor, snapshot_token)
        model = contract.AuditProcedurePage if kind == 'procedure' else contract.AuditFindingPage
        return model.model_validate({**result, 'project_id': project_id,
                       'project_revision': project['revision']}).model_dump(mode='json')

    def _receipt(self, db, project, command, payload_hash, kind, entity_id, action, previous, current):
        created_at = now()
        receipt = contract.AuditWriteReceipt.model_validate({**WIRE, 'request_id': command['request_id'],
             'project_id': project['project_id'], 'project_revision': project['revision'],
             'entity_kind': kind, 'entity_id': entity_id, 'payload_hash': payload_hash,
             'actor': command['actor'], 'created_at': created_at}).model_dump(mode='json')
        event = contract.AuditHistoryEvent.model_validate({'event_id': identity('h'),
             'project_id': project['project_id'], 'project_revision': project['revision'],
             'entity_kind': kind, 'entity_id': entity_id, 'request_id': command['request_id'],
             'actor': command['actor'], 'action': action, 'previous': previous, 'current': current,
             'created_at': created_at}).model_dump(mode='json')
        self.store.save(db, project)
        db.execute('INSERT INTO audit_requests VALUES(?,?,?,?,?,?,?)',
                   (command['request_id'], project['project_id'], payload_hash, 'committed', encoded(receipt), None, created_at))
        db.execute('INSERT INTO audit_history VALUES(?,?,?,?)',
                   (event['event_id'], project['project_id'], project['revision'], encoded(event)))
        return receipt

    def create_project(self, workspace_id, payload):
        command = validated(contract.AuditProjectCreate, payload)
        self.workspace(workspace_id)
        digest = fingerprint({'workspace_id': workspace_id, 'command': command, 'action': 'create_project'})
        with self.store.transaction() as db:
            replay = self.store.request(db, command['request_id'], digest)
            if replay:
                return json.loads(replay['receipt'])
            project_id, timestamp = identity('p'), now()
            project = {'project_id': project_id, 'workspace_id': workspace_id, 'revision': 1,
                       'content_revision': 1, 'project_context_revision': 1, 'project': command['project'],
                       'created_at': timestamp, 'updated_at': timestamp, 'records': {},
                       'procedures': {}, 'findings': {}, 'latest_workpaper_id': None}
            return self._receipt(db, project, command, digest, 'project', project_id,
                                 'create_project', None, self._display(project, 'project'))

    def _batch_refs(self, workspace_id, selections):
        if len({choice['batch_id'] for choice in selections}) != len(selections):
            raise DataError('同一程序不能重复关联同一批次')
        result = []
        for choice in selections:
            batch = self.batches.get(choice['batch_id'])
            if batch['workspace_id'] != workspace_id:
                raise DataError('关联批次不属于当前工作空间', 404)
            if choice['batch_revision'] != batch['revision']:
                raise DataError('关联批次版本冲突，请刷新所见批次', 409)
            result.append({**choice, 'template_id': batch['template_id'], 'template_version': batch['template_version'],
                           'config_hash': batch['template']['config_hash']})
        return result

    def _evidence_refs(self, db, project, procedure, choices, previous=None):
        result = []
        retained = {ref['evidence_id']: ref for ref in (previous or {}).get('evidence_refs', [])}
        linked = {ref['batch_id'] for ref in procedure['batches']}
        identities = set()
        for choice in choices:
            if 'evidence_id' in choice:
                ref = retained.get(choice['evidence_id'])
                if ref is None:
                    raise DataError('只能保留此调查事项原有的证据快照', 409)
            elif 'inspection_id' in choice:
                evidence = self.app.state.forensic_service.evidence(project['workspace_id'], choice)
                ref = {**choice, 'evidence_id': identity('e'), 'project_id': project['project_id'],
                       'workspace_id': project['workspace_id'], 'scenario': 'image_forensics',
                       'issue_type': 'result', 'snapshot_hash': evidence['snapshot_hash'],
                       'captured_at': now(), 'sources': evidence['sources'], 'source_count': evidence['source_count']}
                db.execute('INSERT INTO evidence_snapshots VALUES(?,?,?)',
                           (ref['evidence_id'], project['project_id'], encoded(evidence)))
            else:
                if choice['batch_id'] not in linked:
                    raise DataError('调查证据必须来自本程序关联批次', 409)
                evidence = batch_item_evidence(self.batches, choice['batch_id'], choice['item_id'], choice['batch_revision'])
                if evidence['workspace_id'] != project['workspace_id']:
                    raise DataError('证据不属于当前工作空间', 404)
                evidence_id = identity('e')
                ref = {**choice, 'evidence_id': evidence_id, 'project_id': project['project_id'],
                       'workspace_id': project['workspace_id'], 'scenario': evidence['scenario'],
                       'issue_type': evidence['issue_type'], 'snapshot_hash': evidence['snapshot_hash'],
                       'captured_at': now(), 'sources': evidence['sources'], 'source_count': evidence['source_count']}
                db.execute('INSERT INTO evidence_snapshots VALUES(?,?,?)',
                           (evidence_id, project['project_id'], encoded(evidence)))
            key = ('image_forensics', ref['inspection_id'], ref['inspection_revision']) if 'inspection_id' in ref else (ref['batch_id'], ref['item_id'], ref['batch_revision'])
            if key in identities:
                raise DataError('调查事项不能重复关联同一版证据')
            identities.add(key)
            model = contract.ForensicEvidenceRef if 'inspection_id' in ref else contract.AuditEvidenceRef
            result.append(model.model_validate(ref).model_dump(mode='json'))
        return result

    def write(self, project_id, kind, payload, entity_id=None):
        models = {'project': contract.AuditProjectWrite,
                  'procedure': contract.AuditProcedureWrite if entity_id else contract.AuditProcedureCreate,
                  'finding': contract.AuditFindingWrite if entity_id else contract.AuditFindingCreate,
                  'record': contract.AuditRecordCreate}
        command = validated(models[kind], payload)
        action = ('write_' if entity_id or kind in {'project', 'record'} else 'create_') + kind
        digest = fingerprint({'project_id': project_id, 'kind': kind, 'entity_id': entity_id,
                              'action': action, 'command': command})
        # Same lock order for evidence capture, commands and workpaper publication.
        with self.batches.store.lock, self._forensic_lock(), self.store.transaction() as db:
            replay = self.store.request(db, command['request_id'], digest, project_id)
            if replay:
                return json.loads(replay['receipt'])
            project = self.store.project(db, project_id)
            self.workspace(project['workspace_id'])
            self.store.conditional(db, project, command['expected_revision'])
            timestamp = now()
            previous = None
            if kind == 'record':
                entity = self._entity(project, command['entity_kind'], command['entity_id'])
                if (entity['content_revision'] != command['content_revision'] or
                        project['project_context_revision'] != command['project_context_revision']):
                    raise DataError('编制复核记录版本冲突，请检查所见内容', 409)
                if command['role'] == 'reviewed':
                    prepared = entity['records'].get('prepared')
                    if (not prepared or prepared['content_revision'] != entity['content_revision'] or
                            prepared['project_context_revision'] != project['project_context_revision']):
                        raise DataError('请先编制当前版本，再记录复核', 409)
                entity_id = identity('r')
                previous = entity['records'].get(command['role'])
                current = contract.AuditRecord.model_validate({'record_id': entity_id,
                    'entity_kind': command['entity_kind'], 'entity_id': command['entity_id'],
                    'content_revision': entity['content_revision'],
                    'project_context_revision': project['project_context_revision'],
                    'project_revision': project['revision'] + 1, 'role': command['role'],
                    'actor': command['actor'], 'note': command['note'], 'created_at': timestamp}).model_dump(mode='json')
                entity['records'][command['role']] = current
                if command['role'] == 'prepared':
                    # Preparation is a new act; a previous review cannot qualify it.
                    entity['records'].pop('reviewed', None)
                entity['updated_at'] = timestamp
            elif kind == 'project':
                previous = self._display(project, 'project')
                project['project'] = command['project']
                project['project_context_revision'] += 1
                entity_id = project_id
            else:
                entity = self._entity(project, kind, entity_id) if entity_id else None
                if entity:
                    previous = self._display(project, kind, entity_id)
                    entity['content_revision'] += 1
                    entity['updated_at'] = timestamp
                else:
                    entity_id = identity('u' if kind == 'procedure' else 'f')
                    entity = {'content_revision': 1, 'created_at': timestamp, 'updated_at': timestamp, 'records': {}}
                    project[kind + 's'][entity_id] = entity
                entity[kind] = command[kind]
                if kind == 'procedure':
                    refs = self._batch_refs(project['workspace_id'], command['batches'])
                    removed = {choice['batch_id'] for choice in entity.get('batches', [])} - {choice['batch_id'] for choice in refs}
                    if any(ref['batch_id'] in removed for finding in project['findings'].values()
                           if finding['procedure_id'] == entity_id for ref in finding['evidence_refs']):
                        raise DataError('关联批次仍被本程序调查证据引用，不能移除', 409)
                    entity['batches'] = refs
                else:
                    procedure_id = entity.get('procedure_id') or command['procedure_id']
                    procedure = self._entity(project, 'procedure', procedure_id)
                    entity['procedure_id'] = procedure_id
                    entity['evidence_refs'] = self._evidence_refs(db, project, procedure, command['evidence'], previous)
                    if entity['finding']['status'] == 'resolved' and (not entity['finding']['explanation'].strip()
                            or not entity['finding']['conclusion'].strip() or not entity['evidence_refs']):
                        raise DataError('解决调查事项须关联证据并填写解释与结论', 409)
                    procedure['content_revision'] += 1
                    procedure['updated_at'] = timestamp
            project['revision'] += 1
            if kind != 'record':
                project['content_revision'] += 1
            project['updated_at'] = timestamp
            if kind != 'record':
                current = self._display(project, kind, entity_id)
            return self._receipt(db, project, command, digest, kind, entity_id, action, previous, current)

    def evidence(self, project_id, evidence_id):
        with closing(self.store.db()) as db:
            project = self.store.project(db, project_id)
            row = db.execute('SELECT body FROM evidence_snapshots WHERE project_id=? AND evidence_id=?',
                             (project_id, evidence_id)).fetchone()
        self.workspace(project['workspace_id'])
        if row is None:
            raise DataError('证据快照不属于此审计项目', 404)
        return json.loads(row[0])

    def history(self, project_id, limit, cursor, snapshot_token, entity_kind=None, entity_id=None):
        with closing(self.store.db()) as db:
            db.execute('BEGIN')
            project = self.store.project(db, project_id)
            items = [json.loads(row[0]) for row in db.execute('SELECT body FROM audit_history WHERE project_id=? '
                     'ORDER BY project_revision,event_id', (project_id,))]
        self.workspace(project['workspace_id'])
        items = [event for event in items if (not entity_kind or event['entity_kind'] == entity_kind)
                 and (not entity_id or event['entity_id'] == entity_id)]
        page = self._page(items, [project_id, 'history', entity_kind, entity_id], limit, cursor, snapshot_token)
        return contract.AuditHistoryPage.model_validate({**page, 'project_id': project_id,
                        'project_revision': project['revision']}).model_dump(mode='json')

    def request_status(self, project_id, request_id):
        with closing(self.store.db()) as db:
            project = self.store.project(db, project_id)
            row = self.store.request(db, request_id, project_id=project_id)
        self.workspace(project['workspace_id'])
        if row is None:
            raise DataError('审计请求不存在', 404)
        return contract.AuditRequestStatus.model_validate({**WIRE, 'request_id': request_id,
            'project_id': project_id, 'payload_hash': row['payload_hash'], 'state': row['state'],
            'receipt': json.loads(row['receipt']) if row['receipt'] else None}).model_dump(mode='json')

    def _workpaper_snapshot(self, db, project, command):
        project_id = project['project_id']
        procedures = [self._display(project, 'procedure', key) for key in sorted(project['procedures'])]
        findings = [self._display(project, 'finding', key) for key in sorted(project['findings'])]
        reasons = []
        if not procedures:
            reasons.append('尚未建立审计工作程序')
        if not project['project']['objective'].strip():
            reasons.append('审计目标尚未填写')
        for entity in [self._display(project, 'project'), *procedures, *findings]:
            kind = 'project' if 'project' in entity else 'procedure' if 'procedure' in entity else 'finding'
            title = entity[kind]['title']
            if not entity['records_current']:
                reasons.append((title[:150] + '：当前版本编制与复核记录未完成')[:200])
            has_forensic_evidence = any(finding['procedure_id'] == entity.get('procedure_id') and
                any(ref.get('scenario') == 'image_forensics' for ref in finding['evidence_refs']) for finding in findings)
            if kind == 'procedure' and (not entity[kind]['basis'].strip() or not entity[kind]['scope'].strip()
                                      or not entity[kind]['conclusion'].strip() or not (entity['batches'] or has_forensic_evidence)):
                reasons.append((title[:150] + '：依据、范围、关联执行证据或结论未完成')[:200])
            if kind == 'finding' and (entity[kind]['status'] != 'resolved' or not entity[kind]['explanation'].strip()
                                     or not entity[kind]['conclusion'].strip() or not entity['evidence_refs']):
                reasons.append((title[:150] + '：调查证据、解释或结论未解决')[:200])
        selections = {}
        for procedure in procedures:
            for ref in procedure['batches']:
                selections.setdefault(ref['batch_id'], []).append(ref['batch_revision'])
        batches = []
        for batch_id, revisions in sorted(selections.items()):
            batch = self.batches.get(batch_id)
            if any(revision != batch['revision'] for revision in revisions):
                reasons.append('关联批次版本已变化：' + batch_id)
            if not batch['summary'].get('review_complete'):
                reasons.append('关联批次执行或复核未完成：' + batch_id)
            batches.append({'batch_id': batch_id, 'batch_revision': batch['revision'],
                            'template_id': batch['template_id'], 'template_version': batch['template_version'],
                            'config_hash': batch['template']['config_hash'], 'coverage': batch['coverage'],
                            'template_name': batch['template']['name'], 'rules': batch['template']['config'],
                            'execution_status': batch['status'], 'review_complete': batch['summary'].get('review_complete', False)})
        review_refs, covered = [], set()
        for bundle_id in command['review_bundle_ids']:
            try:
                bundle = self.center.get(project['workspace_id'], bundle_id)
            except ArtifactUnavailable:
                raise DataError('所选复核成果不存在', 404) from None
            if bundle['kind'] != 'batch_review' or bundle['batch_id'] not in selections:
                raise DataError('复核成果必须来自当前项目工作程序的关联批次', 409)
            if bundle['batch_id'] in covered:
                raise DataError('同一批次只能明确选择一份复核成果', 409)
            covered.add(bundle['batch_id'])
            if bundle['status'] != 'current' or bundle['delivery_kind'] != 'final':
                reasons.append('所选复核成果为中间材料或版本落后：' + bundle_id)
            file = next((file for file in bundle['files'] if file['name'] == 'results.xlsx'), None)
            if file is None:
                raise DataError('复核成果缺少 results.xlsx', 409)
            artifact = self.center.file(project['workspace_id'], bundle_id, 'results.xlsx')
            with self.center.store.stage_download(project['workspace_id'], artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash):
                pass
            review_refs.append({'bundle_id': bundle_id, 'batch_id': bundle['batch_id'],
                                'batch_revision': bundle['batch_revision'], 'snapshot_hash': bundle['snapshot_hash'],
                                'results_xlsx': file})
        for batch_id in sorted(set(selections) - covered):
            reasons.append('尚未明确选择此批次的正式复核成果：' + batch_id)
        for finding in findings:
            for ref in finding['evidence_refs']:
                if ref.get('scenario') == 'image_forensics':
                    try:
                        self.app.state.forensic_service.evidence(project['workspace_id'], ref)
                    except (DataError, OSError):
                        reasons.append('图像取证原件、报告或人工审阅已变化，需重新选择：' + ref['inspection_id'])
        if command['kind'] == 'final' and reasons:
            raise DataError('业务尚未完成，只能生成中间底稿；' + '；'.join(reasons[:4]), 409)
        evidence_ids = sorted({ref['evidence_id'] for finding in findings for ref in finding['evidence_refs']})
        evidence = [{'evidence_id': evidence_id, 'snapshot': json.loads(db.execute(
            'SELECT body FROM evidence_snapshots WHERE project_id=? AND evidence_id=?',
            (project_id, evidence_id)).fetchone()[0])} for evidence_id in evidence_ids]
        return {'project': self._display(project, 'project'), 'procedures': procedures, 'findings': findings,
                'evidence': evidence, 'batches': batches, 'review_refs': review_refs,
                'unresolved_count': len(reasons), 'unresolved_reasons': reasons,
                'request': command, 'predecessor_workpaper_id': project['latest_workpaper_id']}

    def publish(self, project_id, payload):
        command = validated(contract.WorkpaperRequest, payload)
        digest = fingerprint({'project_id': project_id, 'action': 'workpaper', 'command': command})
        with self.batches.store.lock, self._forensic_lock(), self.store.transaction() as db:
            row = self.store.request(db, command['request_id'], digest, project_id)
            if row and row['state'] == 'committed':
                return json.loads(row['receipt'])
            project = self.store.project(db, project_id)
            self.workspace(project['workspace_id'])
            if row is None:
                self.store.conditional(db, project, command['expected_revision'])
                snapshot = self._workpaper_snapshot(db, project, command)
                operation = {'workpaper_id': identity('w'), 'project_id': project_id,
                             'workspace_id': project['workspace_id'], 'request_id': command['request_id'],
                             'payload_hash': digest, 'snapshot_hash': fingerprint(snapshot),
                             'snapshot': snapshot, 'created_at': now()}
                db.execute('INSERT INTO audit_requests VALUES(?,?,?,?,?,?,?)',
                           (command['request_id'], project_id, digest, 'publishing', None, encoded(operation), operation['created_at']))
            else:
                operation = json.loads(row['snapshot'])
        try:
            bundle = self.center.publish_audit_workpaper(operation)
        except (OSError, ArtifactUnavailable, ValueError) as error:
            raise DataError('底稿发布尚未完成；请查询或原样重试同一请求', 409) from error
        with self.batches.store.lock, self._forensic_lock(), self.store.transaction() as db:
            row = self.store.request(db, command['request_id'], digest, project_id)
            if row['state'] == 'committed':
                return json.loads(row['receipt'])
            project = self.store.project(db, project_id)
            project['latest_workpaper_id'] = operation['workpaper_id']
            self.store.save(db, project)
            bundle = self.center.get(project['workspace_id'], operation['workpaper_id'])
            # A separate read connection cannot see the just-written latest
            # pointer until this transaction commits. The fixed receipt uses
            # the state that this transaction is committing.
            bundle['status'] = 'current' if (project['revision'] == operation['snapshot']['project']['revision']
                and self._snapshot_inputs_current(project['workspace_id'], operation['snapshot'])) else 'outdated'
            event = contract.AuditHistoryEvent.model_validate({'event_id': identity('h'),
                'project_id': project_id, 'project_revision': project['revision'], 'entity_kind': 'workpaper',
                'entity_id': operation['workpaper_id'], 'request_id': command['request_id'], 'actor': command['actor'],
                'action': 'publish_workpaper', 'previous': None, 'current': bundle, 'created_at': operation['created_at']}).model_dump(mode='json')
            db.execute('INSERT INTO audit_history VALUES(?,?,?,?)',
                       (event['event_id'], project_id, project['revision'], encoded(event)))
            db.execute("UPDATE audit_requests SET state='committed',receipt=? WHERE request_id=?", (encoded(bundle), command['request_id']))
            return bundle

    def _snapshot_inputs_current(self, workspace_id, snapshot):
        try:
            for entry in snapshot['evidence']:
                evidence = entry['snapshot']
                if evidence.get('scenario') == 'image_forensics':
                    item = evidence['inspection']
                    self.app.state.forensic_service.evidence(workspace_id, {'inspection_id': item['inspection_id'],
                        'inspection_revision': item['revision'], 'report_hash': item['report_hash']})
            for batch in snapshot['batches']:
                current = self.batches.get(batch['batch_id'])
                if (current['revision'] != batch['batch_revision'] or current['status'] != batch['execution_status']
                        or current['coverage'] != batch['coverage']
                        or current['summary'].get('review_complete', False) != batch['review_complete']):
                    return False
            for ref in snapshot['review_refs']:
                if self.center.get(workspace_id, ref['bundle_id'])['status'] != 'current':
                    return False
        except (DataError, ArtifactUnavailable, OSError):
            return False
        return True

    def workpaper_current(self, project_id, workpaper_id, snapshot):
        with closing(self.store.db()) as db:
            project = self.store.project(db, project_id)
        if project['revision'] != snapshot['project']['revision'] or project['latest_workpaper_id'] != workpaper_id:
            return False
        return self._snapshot_inputs_current(project['workspace_id'], snapshot)

    def workpaper(self, project_id, workpaper_id):
        project = self.get(project_id)
        try:
            bundle = self.center.get(project['workspace_id'], workpaper_id)
        except ArtifactUnavailable:
            raise DataError('底稿不存在', 404) from None
        if bundle.get('kind') != 'audit_workpaper' or bundle.get('project_id') != project_id:
            raise DataError('底稿不属于此审计项目', 404)
        return bundle

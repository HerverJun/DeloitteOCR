"""Workspace-wide SQL todo filtering, stable paging and direct identity lookup."""
from contextlib import closing, contextmanager
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from packages.contracts.audit import AuditScenario, TodoItem, TodoLocate, TodoPage, TodoQuery
from packages.contracts.automation import ExecutionState, Scenario
from workbench_platform.batches.store import fingerprint
from workbench_platform.data_api.service import DataError
from workbench_platform.workspaces.routes import _loopback_host

WIRE = {'schema_version': '1.0', 'audit_version': '1.0'}
router = APIRouter(tags=['workspace-todos'])


class WorkspaceTodoService:
    def __init__(self, audit):
        self.audit = audit

    @contextmanager
    def _read(self, workspace_id, query):
        self.audit.workspace(workspace_id)
        # Match audit writes' lock order. One read transaction pins both local
        # ledgers, and the persistent generations invalidate changed snapshots.
        with self.audit.batches.store.lock, self.audit.store.lock, closing(self.audit.store.db()) as db:
            db.execute('ATTACH DATABASE ? AS batch_domain', (str(self.audit.batches.store.path),))
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            self._check_scope(db, workspace_id, query)
            batch = db.execute('SELECT revision FROM batch_domain.workspace_todo_revisions WHERE workspace_id=?',
                               (workspace_id,)).fetchone()
            audit = db.execute('SELECT revision FROM audit_todo_revisions WHERE workspace_id=?',
                               (workspace_id,)).fetchone()
            binding = query.model_dump(exclude={'cursor', 'snapshot_token'})
            query_hash = fingerprint(binding)
            token = 's' + fingerprint({'workspace_id': workspace_id, 'query_hash': query_hash,
                                       'batch_generation': batch[0] if batch else 0,
                                       'audit_generation': audit[0] if audit else 0})
            if query.snapshot_token is not None and query.snapshot_token != token:
                raise DataError('待办快照已变化，请刷新当前筛选后继续定位', 409)
            yield db, query_hash, token

    @staticmethod
    def _check_scope(db, workspace_id, query):
        if query.project_id and db.execute('SELECT 1 FROM projects WHERE project_id=? AND workspace_id=?',
                                           (query.project_id, workspace_id)).fetchone() is None:
            raise DataError('审计项目不属于当前工作空间', 404)
        if query.procedure_id:
            sql = "SELECT 1 FROM projects p,json_each(p.body,'$.procedures') u WHERE p.workspace_id=? AND u.key=?"
            args = [workspace_id, query.procedure_id]
            if query.project_id:
                sql += ' AND p.project_id=?'
                args.append(query.project_id)
            if db.execute(sql, args).fetchone() is None:
                raise DataError('审计程序不属于当前筛选工作空间或项目', 404)

    @staticmethod
    def _cursor(token, offset):
        return 'c' + token[1:] + '_' + str(offset)

    @staticmethod
    def _offset(query, token):
        if query.cursor is None:
            return 0
        if query.snapshot_token is None or not query.cursor.startswith('c' + token[1:] + '_'):
            raise DataError('待办游标不属于当前工作空间、筛选或排序快照', 409)
        suffix = query.cursor[66:]
        if not re.fullmatch(r'0|[1-9][0-9]{0,9}', suffix):
            raise DataError('待办分页游标无效')
        return int(suffix)

    @staticmethod
    def _sql(workspace_id, query, identity=None):
        args = {'workspace_id': workspace_id, **query.model_dump(exclude={'cursor', 'snapshot_token', 'limit', 'sort'})}
        context_where = ['workspace_id=:workspace_id']
        for name in ('project_id', 'procedure_id'):
            if getattr(query, name):
                context_where.append(name + '=:' + name)
        issue_where = ['b.workspace_id=:workspace_id']
        finding_where = ['f.workspace_id=:workspace_id']
        if query.kind == 'finding' or query.finding_status is not None:
            issue_where.append('0')
        if query.kind == 'issue' or query.disposition is not None:
            finding_where.append('0')
        if query.issue_type:
            issue_where.append('i.issue_type=:issue_type')
            if query.issue_type != 'investigation':
                finding_where.append('0')
        for name, expr in (('scenario', 'b.scenario'), ('execution_state', 'b.execution_state'),
                           ('disposition', 'i.disposition')):
            if getattr(query, name) is not None:
                issue_where.append(expr + '=:' + name)
        if query.priority:
            if query.priority != 'normal':
                issue_where.append('0')
            finding_where.append('f.priority=:priority')
        if query.finding_status:
            finding_where.append('f.state=:finding_status')
        for name in ('project_id', 'procedure_id'):
            if getattr(query, name):
                issue_where.append('cx.context_key IS NOT NULL')
                finding_where.append('f.' + name + '=:' + name)
        association = []
        if query.scenario:
            association.append('wb.scenario=:scenario')
        if query.execution_state:
            association.append('wb.execution_state=:execution_state')
        if association:
            finding_where.append('EXISTS(SELECT 1 FROM links fl JOIN workspace_batches wb ON wb.batch_id=fl.batch_id '
                                 'WHERE fl.project_id=f.project_id AND fl.procedure_id=f.procedure_id AND '
                                 + ' AND '.join(association) + ')' + (
                    " OR EXISTS(SELECT 1 FROM json_each(f.evidence_refs) fe WHERE json_extract(fe.value,'$.scenario')=:scenario)"
                    if query.scenario and not query.execution_state else ''))
            finding_where[-1] = '(' + finding_where[-1] + ')'
        if identity:
            args.update(target_parent=identity['parent_id'], target_entity=identity['entity_id'])
            issue_where.extend(['b.batch_id=:target_parent', 'i.item_id=:target_entity'] if identity['kind'] == 'issue' else ['0'])
            finding_where.extend(['f.project_id=:target_parent', 'f.finding_id=:target_entity'] if identity['kind'] == 'finding' else ['0'])
        sql = '''WITH workspace_batches AS MATERIALIZED (
            SELECT batch_id,workspace_id,json_extract(body,'$.revision') AS batch_revision,
                json_extract(body,'$.template.scenario') AS scenario,json_extract(body,'$.status') AS execution_state,
                json_extract(body,'$.template.name') AS template_name,json_extract(body,'$.template.config.link_id') AS link_id,
                EXISTS(SELECT 1 FROM batch_domain.batch_disposition_operations op
                       WHERE op.batch_id=batches.batch_id AND op.state<>'committed') AS pending_operation
            FROM batch_domain.batches WHERE workspace_id=:workspace_id),
        links AS MATERIALIZED (SELECT * FROM audit_procedure_batches WHERE workspace_id=:workspace_id),
        contexts AS MATERIALIZED (
            SELECT batch_id,min(project_id||'|'||procedure_id) AS context_key FROM links WHERE CONTEXT_WHERE GROUP BY batch_id),
        issues AS (
            SELECT i.*,CASE WHEN category IN ('review','different','only_left','only_right','ambiguous_left','ambiguous_right')
                THEN 'result' ELSE 'technical' END AS issue_type FROM batch_domain.batch_items i),
        all_todos AS (
            SELECT t.todo_id,'issue' AS kind,i.issue_type,b.batch_id,i.item_id,
                substr(cx.context_key,1,instr(cx.context_key,'|')-1) AS project_id,
                substr(cx.context_key,instr(cx.context_key,'|')+1) AS procedure_id,
                NULL AS finding_id,b.batch_revision,p.revision AS project_revision,b.scenario,
                substr(coalesce(nullif(json_extract(i.original_body,'$.title'),''),i.category),1,200) AS title,
                i.disposition AS state,'normal' AS priority,1 AS priority_rank,i.updated_at,
                substr(b.template_name||' · '||coalesce(json_extract(i.original_body,'$.detail'),''),1,1000) AS source_summary,
                i.category,b.pending_operation,EXISTS(SELECT 1 FROM batch_domain.batch_item_adoptions a
                    WHERE a.batch_id=b.batch_id AND a.item_id=i.item_id) AS has_adoption,b.execution_state,b.link_id
            FROM workspace_batches b JOIN issues i ON i.batch_id=b.batch_id
                JOIN batch_domain.batch_todo_identity t ON t.batch_id=i.batch_id AND t.item_id=i.item_id
                LEFT JOIN contexts cx ON cx.batch_id=b.batch_id
                LEFT JOIN projects p ON p.project_id=substr(cx.context_key,1,instr(cx.context_key,'|')-1)
            WHERE ISSUE_WHERE
            UNION ALL
            SELECT f.todo_id,'finding','investigation',NULL,NULL,f.project_id,f.procedure_id,f.finding_id,NULL,
                f.project_revision,(SELECT CASE WHEN count(DISTINCT scenario)=1 THEN min(scenario) END FROM (
                    SELECT wb.scenario FROM links fl JOIN workspace_batches wb ON wb.batch_id=fl.batch_id
                    WHERE fl.project_id=f.project_id AND fl.procedure_id=f.procedure_id
                    UNION ALL SELECT json_extract(fe.value,'$.scenario') AS scenario FROM json_each(f.evidence_refs) fe)),
                f.title,f.state,f.priority,CASE f.priority WHEN 'high' THEN 0 WHEN 'low' THEN 2 ELSE 1 END,
                f.updated_at,substr('审计项目：'||f.project_title||' · 程序：'||coalesce(f.procedure_title,'')||
                    '；证据数：'||json_array_length(f.evidence_refs),1,1000),NULL,0,0,NULL,NULL
            FROM audit_finding_todos f WHERE FINDING_WHERE),
        todos AS (SELECT *,substr(updated_at,1,19)||'.'||CASE WHEN instr(updated_at,'.')=0 THEN '000000'
                   ELSE substr(substr(updated_at,21,instr(updated_at,'Z')-21)||'000000',1,6) END||'Z' AS sort_at
                  FROM all_todos) '''
        return sql.replace('CONTEXT_WHERE', ' AND '.join(context_where)).replace('ISSUE_WHERE', ' AND '.join(issue_where)).replace(
            'FINDING_WHERE', ' AND '.join(finding_where)), args

    @staticmethod
    def _order(query):
        return ('priority_rank,' if query.sort == 'priority_then_updated' else '') + 'sort_at DESC,todo_id'

    @staticmethod
    def _item(row, workspace_id):
        context = {'workspace_id': workspace_id, **{key: row[key] for key in
                   ('project_id', 'procedure_id', 'batch_id', 'item_id', 'finding_id')}}
        actions = ['open_audit'] if row['kind'] == 'finding' else ['open_evidence']
        if row['kind'] == 'issue':
            if row['scenario'] != 'data_compare' and row['link_id']:
                actions.append('open_native')
            if row['project_id']:
                actions.append('open_audit')
            if row['issue_type'] == 'result' and not row['pending_operation']:
                if row['state'] != 'confirmed' and not (row['scenario'] == 'ocr' and row['has_adoption']):
                    actions.append('confirm')
                if row['state'] != 'deferred':
                    actions.append('defer')
                if row['state'] != 'pending':
                    actions.append('reopen')
                if row['scenario'] != 'data_compare':
                    actions.append('adopt')
        return TodoItem.model_validate({'todo_id': row['todo_id'], 'kind': row['kind'], 'issue_type': row['issue_type'],
            'context': context, 'scenario': row['scenario'], 'batch_revision': row['batch_revision'],
            'project_revision': row['project_revision'], 'title': row['title'], 'state': row['state'],
            'priority': row['priority'], 'priority_basis': 'manual_investigation' if row['kind'] == 'finding' else 'default_review_order',
            'updated_at': row['updated_at'], 'source_summary': row['source_summary'], 'actions': actions}).model_dump(mode='json')

    def list(self, workspace_id, query):
        with self._read(workspace_id, query) as (db, query_hash, token):
            offset = self._offset(query, token)
            sql, args = self._sql(workspace_id, query)
            total = db.execute(sql + 'SELECT count(*) FROM todos', args).fetchone()[0]
            if offset > total:
                raise DataError('待办游标超出当前快照范围')
            rows = db.execute(sql + 'SELECT * FROM todos ORDER BY ' + self._order(query) + ' LIMIT :page_limit OFFSET :page_offset',
                              {**args, 'page_limit': query.limit + 1, 'page_offset': offset}).fetchall()
            return TodoPage.model_validate({**WIRE, 'workspace_id': workspace_id, 'query_hash': query_hash,
                'snapshot_token': token, 'next_cursor': self._cursor(token, offset + query.limit) if len(rows) > query.limit else None,
                'total': total, 'sort': query.sort, 'items': [self._item(row, workspace_id) for row in rows[:query.limit]]}).model_dump(mode='json')

    def locate(self, workspace_id, todo_id, query):
        with self._read(workspace_id, query) as (db, _, token):
            self._offset(query, token)
            # Indexed stable identity lookup reaches the actual item even when
            # it is far beyond the first batch issue page or outside filters.
            row = db.execute("SELECT 'issue' kind,t.batch_id parent_id,t.item_id entity_id FROM batch_domain.batch_todo_identity t "
                'JOIN batch_domain.batches b ON b.batch_id=t.batch_id WHERE t.todo_id=? AND b.workspace_id=?',
                (todo_id, workspace_id)).fetchone()
            if row is None:
                row = db.execute("SELECT 'finding' kind,t.project_id parent_id,t.finding_id entity_id FROM audit_todo_identity t "
                    'JOIN projects p ON p.project_id=t.project_id WHERE t.todo_id=? AND p.workspace_id=?',
                    (todo_id, workspace_id)).fetchone()
            if row is None:
                raise DataError('待办事项不属于当前工作空间', 404)
            sql, args = self._sql(workspace_id, query, dict(row))
            item = db.execute(sql + 'SELECT * FROM todos', args).fetchone()
            matches = item is not None
            if item is None:
                # Retain an applicable project/procedure association when one
                # was explicitly selected, while removing unrelated filters.
                context_query = TodoQuery(project_id=query.project_id, procedure_id=query.procedure_id)
                sql, args = self._sql(workspace_id, context_query, dict(row))
                item = db.execute(sql + 'SELECT * FROM todos', args).fetchone()
                if item is None:
                    sql, args = self._sql(workspace_id, TodoQuery(), dict(row))
                    item = db.execute(sql + 'SELECT * FROM todos', args).fetchone()
            if item is None:
                raise DataError('待办事项已移除，请刷新列表', 404)
            page_cursor = None
            if matches:
                sql, args = self._sql(workspace_id, query)
                before = ('priority_rank<:target_priority OR (priority_rank=:target_priority AND '
                          '(sort_at>:target_time OR (sort_at=:target_time AND todo_id<:target_id)))')
                if query.sort == 'updated':
                    before = 'sort_at>:target_time OR (sort_at=:target_time AND todo_id<:target_id)'
                rank = db.execute(sql + 'SELECT count(*) FROM todos WHERE ' + before,
                    {**args, 'target_priority': item['priority_rank'], 'target_time': item['sort_at'], 'target_id': todo_id}).fetchone()[0]
                page_cursor = self._cursor(token, (rank // query.limit) * query.limit)
            return TodoLocate.model_validate({**WIRE, 'item': self._item(item, workspace_id),
                'matches_filters': matches, 'snapshot_token': token, 'page_cursor': page_cursor}).model_dump(mode='json')


def todo_query(kind: Literal['issue', 'finding'] | None = None,
               issue_type: Literal['result', 'technical', 'investigation'] | None = None,
               scenario: AuditScenario | None = None, disposition: Literal['pending', 'confirmed', 'deferred'] | None = None,
               finding_status: Literal['draft', 'resolved'] | None = None, execution_state: ExecutionState | None = None,
               project_id: str | None = None, procedure_id: str | None = None,
               priority: Literal['high', 'normal', 'low'] | None = None,
               sort: Literal['priority_then_updated', 'updated'] = 'priority_then_updated',
               limit: int = Query(50, ge=1, le=200), cursor: str | None = None, snapshot_token: str | None = None):
    try:
        return TodoQuery.model_validate(locals())
    except ValidationError:
        raise DataError('待办筛选或分页参数无效') from None


def _service(request):
    if not _loopback_host(request):
        raise HTTPException(403, '请从本机平台页面查看待办')
    audit = getattr(request.app.state, 'audit_service', None)
    if audit is None:
        raise HTTPException(503, '待办存储尚未配置')
    return audit.todos


@router.get('/workspaces/{workspace_id}/todos')
def workspace_todos(workspace_id: str, request: Request, query: TodoQuery = Depends(todo_query)):
    return _service(request).list(workspace_id, query)


@router.get('/workspaces/{workspace_id}/todos/{todo_id}')
def locate_workspace_todo(workspace_id: str, todo_id: str, request: Request, query: TodoQuery = Depends(todo_query)):
    return _service(request).locate(workspace_id, todo_id, query)

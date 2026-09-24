"""User messages wait for graph boundaries; accepting them never executes a tool."""
import json
from copy import deepcopy

from ocr_workbench.store import Conflict, now
from .contracts import SendMessage
from .store import canonical, digest
from .selection import grant_selection, validate_snapshot


class Inbox:
    def __init__(self, agent, policy, services=None):
        self.agent, self.policy, self.services = agent, policy, services
        self.store = agent.business

    def existing(self, session_id, request):
        body = SendMessage.model_validate(request)
        rows = self.store.rows("""SELECT i.* FROM agent_inbox i JOIN agent_runs r ON r.id=i.run_id
            WHERE r.session_id=? AND i.client_request_id=?""", (session_id, body.client_request_id))
        if rows and rows[0]['request_hash'] != digest(body.model_dump(exclude={'client_request_id'})):
            raise Conflict('相同请求编号对应不同追加消息')
        return rows[0] if rows else None

    def enqueue(self, project_id, session_id, request, selection=None):
        body = SendMessage.model_validate(request)
        with self.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            self.agent._session(db, project_id, session_id)
            rows = db.execute("""SELECT i.* FROM agent_inbox i JOIN agent_runs r ON r.id=i.run_id
                WHERE r.session_id=? AND i.client_request_id=?""", (session_id, body.client_request_id)).fetchall()
            signature = digest(body.model_dump(exclude={'client_request_id'}))
            if rows:
                if rows[0]['request_hash'] != signature:
                    raise Conflict('相同请求编号对应不同追加消息')
                return dict(rows[0])
            active = db.execute("SELECT id FROM agent_runs WHERE session_id=? AND status NOT IN ('completed','failed','cancelled')", (session_id,)).fetchone()
            if not active:
                raise Conflict('原运行已结束，请将消息作为新一轮发送')
            run = self.agent._run(db, project_id, active[0])
            if db.execute("SELECT COUNT(*) FROM agent_inbox WHERE run_id=? AND state='pending'", (run['id'],)).fetchone()[0] >= 50:
                raise Conflict('等待处理的追加消息已达 50 条，请等待安全边界')
            if selection is not None:
                validate_snapshot(db, project_id, selection)
            key = db.execute("""INSERT INTO agent_inbox(run_id,client_request_id,request_hash,content,context,state,created)
                VALUES(?,?,?,?,?,'pending',?) RETURNING id""", (run['id'], body.client_request_id, signature, body.content,
                canonical({'selection_token': body.selection_token, 'selection': selection}), now())).fetchone()[0]
            self.agent._event(db, session_id, run['id'], run['generation'], f'inbox:{key}:received', 'message',
                              {'role': 'user', 'content': body.content, 'inbox_id': key, 'queue_state': 'pending'})
            return dict(db.execute('SELECT * FROM agent_inbox WHERE id=?', (key,)).fetchone())

    def pending(self, run_id):
        return bool(self.store.rows("SELECT 1 FROM agent_inbox WHERE run_id=? AND state='pending' LIMIT 1", (run_id,)))

    def receive(self, state):
        run = self.agent.run(state['project_id'], state['run_id'])
        self.agent.require_generation(run, state['generation'])
        self.agent.require_executor(run)
        # A model POST already committed for this step must keep its exact input.
        recorded = self.store.rows('SELECT 1 FROM agent_model_requests WHERE run_id=? AND step=?', (run['id'], state['step']))
        pending = [] if recorded else self.store.rows("SELECT * FROM agent_inbox WHERE run_id=? AND state='pending' ORDER BY id", (run['id'],))
        for item in pending:
            context = json.loads(item['context'])
            selection = context.get('selection')
            rejection = None
            proposed = deepcopy(run)
            proposed['context'].update(selection=selection, scope_revision=item['id'])
            try:
                if selection is not None:
                    grant_selection(self.policy, proposed, selection, services=self.services)
            except (ValueError, Conflict) as error:
                rejection = str(error)[:500]
            with self.store.transaction() as db:
                db.execute('BEGIN IMMEDIATE')
                run = self.agent._run(db, run['project_id'], run['id'])
                self.agent.require_generation(run, state['generation'])
                self.agent.require_executor(run)
                row = db.execute('SELECT state FROM agent_inbox WHERE id=?', (item['id'],)).fetchone()
                if row[0] != 'pending':
                    continue
                if selection is not None and rejection is None:
                    try:
                        validate_snapshot(db, run['project_id'], selection, allow_initial_render=True)
                    except Conflict as error:
                        rejection = str(error)[:500]
                    else:
                        run['context'].update(selection=selection, selection_token=context['selection_token'], scope_revision=item['id'], scope_blocked=False)
                if rejection:
                    run['context']['scope_blocked'] = True
                db.execute('UPDATE agent_runs SET context=?,updated=? WHERE id=?', (canonical(run['context']), now(), run['id']))
                context.update(applied_step=state['step'], scope_error=rejection)
                db.execute("UPDATE agent_inbox SET state='applied',context=? WHERE id=?", (canonical(context), item['id']))
                self.agent._event(db, run['session_id'], run['id'], run['generation'], f"inbox:{item['id']}:applied", 'run_state',
                    {'status': run['status'], 'inbox_applied': item['id'], 'scope_error': rejection})
        rows = self.store.rows("SELECT * FROM agent_inbox WHERE run_id=? AND state='applied' AND json_extract(context,'$.applied_step')=? ORDER BY id", (run['id'], state['step']))
        return [{'id': f"inbox:{r['id']}", 'role': 'user', 'content': r['content'] +
                 ('\n[工作台范围变更未生效：' + json.loads(r['context'])['scope_error'] + ']' if json.loads(r['context']).get('scope_error') else '')} for r in rows]

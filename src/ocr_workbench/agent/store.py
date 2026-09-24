"""Agent business audit and UI projection; graph messages live only in saver."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from ocr_workbench.store import Conflict, encoded, now, uid
from .contracts import CreateSession, UpdateSession, Event, SendMessage, RunState, RUN_TRANSITIONS, ReplyDecision

GRAPH_VERSION = "ocr-agent-graph-v1"
TERMINAL = {"completed", "failed", "cancelled"}


class InboxPending(Conflict):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class AgentStore:
    def __init__(self, business):
        self.business = business
        self.executor = None

    def require_executor(self, run):
        if self.executor and (run["owner"] != self.executor or (run["lease_expires"] or 0) <= time.time()):
            raise Conflict("执行租约已失效，旧执行者不能继续")

    @staticmethod
    def _session(db, project_id, session_id):
        row = db.execute("SELECT * FROM agent_sessions WHERE id=? AND project_id=?", (session_id, project_id)).fetchone()
        if not row:
            raise KeyError("助手会话不存在于当前项目")
        return dict(row)

    def session(self, project_id, session_id):
        with self.business.transaction() as db:
            return self._session(db, project_id, session_id)

    def create_session(self, project_id, request):
        request = CreateSession.model_validate(request)
        request_hash = digest({"title": request.title})
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError("项目不存在")
            existing = db.execute("SELECT * FROM agent_sessions WHERE project_id=? AND client_request_id=?",
                                  (project_id, request.client_request_id)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise Conflict("相同请求编号对应不同会话内容")
                return dict(existing)
            key = uid()
            db.execute("""INSERT INTO agent_sessions(id,project_id,client_request_id,request_hash,graph_thread_id,
                graph_version,title,status,created,updated) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
                (key, project_id, request.client_request_id, request_hash, uid(), GRAPH_VERSION, request.title, now(), now()))
            return self._session(db, project_id, key)

    @staticmethod
    def mutation(db, session_id, request_id, action, payload, run_id=None):
        signature = digest({'action': action, 'run_id': run_id, 'payload': payload})
        row = db.execute('SELECT * FROM agent_mutations WHERE session_id=? AND request_id=?', (session_id, request_id)).fetchone()
        if row:
            if row['input_hash'] != signature:
                raise Conflict('相同请求编号对应不同会话操作')
            return dict(row)
        db.execute("INSERT INTO agent_mutations VALUES(?,?,?,?,?,'pending',NULL,?)",
                   (session_id, request_id, signature, action, run_id, now()))
        return None

    @staticmethod
    def finish_mutation(db, session_id, request_id, response):
        db.execute("UPDATE agent_mutations SET state='applied',response=? WHERE session_id=? AND request_id=?",
                   (canonical(response), session_id, request_id))

    def update_session(self, project_id, session_id, request):
        body = UpdateSession.model_validate(request)
        with self.business.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._session(db, project_id, session_id)
            old = self.mutation(db, session_id, body.client_request_id, 'update_session', body.model_dump(exclude={'client_request_id'}))
            if old and old['state'] == 'applied':
                return json.loads(old['response'])
            if body.status == 'archived' and db.execute("SELECT 1 FROM agent_runs WHERE session_id=? AND status NOT IN ('completed','failed','cancelled')", (session_id,)).fetchone():
                raise Conflict('请先停止当前助手运行再归档')
            db.execute('UPDATE agent_sessions SET title=?,status=?,updated=? WHERE id=?',
                       (body.title or current['title'], body.status or current['status'], now(), session_id))
            result = self._session(db, project_id, session_id)
            self.finish_mutation(db, session_id, body.client_request_id, result)
            return result

    @staticmethod
    def discard_inbox(db, run_id):
        db.execute("UPDATE agent_inbox SET state='discarded' WHERE run_id=? AND state='pending'", (run_id,))

    @staticmethod
    def _event(db, session_id, run_id, generation, event_key, kind, payload):
        existing = db.execute("SELECT * FROM agent_events WHERE session_id=? AND event_key=?", (session_id, event_key)).fetchone()
        if existing:
            if kind in {"message", "tool_started", "tool_finished", "artifact_ready", "job_progress"} and (existing["run_id"], existing["type"], existing["payload"]) == (run_id, kind, canonical(payload)):
                # An explicitly resumed generation can replay a committed effect.
                # Keep its original event and sequence instead of publishing twice.
                return existing["seq"]
            if (existing["run_id"], existing["generation"], existing["type"], existing["payload"]) != (run_id, generation, kind, canonical(payload)):
                raise Conflict("事件去重键对应不同内容")
            return existing["seq"]
        row = db.execute("UPDATE agent_sessions SET next_seq=next_seq+1,updated=? WHERE id=? RETURNING next_seq-1", (now(), session_id)).fetchone()
        if not row:
            raise KeyError("会话不存在")
        seq = row[0]
        event = Event(session_id=session_id, run_id=run_id, generation=generation, seq=seq, type=kind, payload=payload, created=now())
        db.execute("INSERT INTO agent_events VALUES(?,?,?,?,?,?,?,?)", (session_id, seq, run_id, generation, event_key, kind, canonical(payload), event.created))
        if run_id:
            db.execute("UPDATE agent_runs SET last_event_seq=? WHERE id=?", (seq, run_id))
        return seq

    @staticmethod
    def _run(db, project_id, run_id):
        row = db.execute("""SELECT r.*,s.project_id,s.graph_thread_id,s.graph_version FROM agent_runs r
            JOIN agent_sessions s ON s.id=r.session_id WHERE r.id=? AND s.project_id=?""", (run_id, project_id)).fetchone()
        if not row:
            raise KeyError("助手运行不存在于当前项目")
        result = dict(row)
        for key in ("context", "config", "limits", "usage", "coverage"):
            result[key] = json.loads(result[key])
        return result

    def run(self, project_id, run_id):
        with self.business.transaction() as db:
            return self._run(db, project_id, run_id)

    def create_run(self, project_id, session_id, request, *, context, config, limits):
        request = SendMessage.model_validate(request)
        request_hash = digest(request.model_dump(exclude={"client_request_id"}))
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            session = self._session(db, project_id, session_id)
            old = db.execute("SELECT * FROM agent_runs WHERE session_id=? AND client_request_id=?", (session_id, request.client_request_id)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("相同请求编号对应不同目标")
                return self._run(db, project_id, old["id"])
            if session["status"] != "active" or session["graph_version"] != GRAPH_VERSION:
                raise Conflict("会话已归档或图版本不兼容；请新建会话")
            key = uid()
            try:
                db.execute("""INSERT INTO agent_runs(id,session_id,client_request_id,request_hash,goal,status,
                    context,config,limits,created,updated) VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""",
                    (key, session_id, request.client_request_id, request_hash, request.content, canonical(context), canonical(config), canonical(limits), now(), now()))
            except sqlite3.IntegrityError:
                raise Conflict("当前会话已有运行；追加消息须进入 inbox") from None
            self._event(db, session_id, key, 1, key + ":goal", "message", {"role": "user", "content": request.content})
            self._event(db, session_id, key, 1, key + ":queued", "run_state", {"status": "queued"})
            return self._run(db, project_id, key)

    @staticmethod
    def require_generation(run, generation):
        if run["generation"] != generation or run["status"] in TERMINAL:
            raise Conflict("助手运行已停止或执行代次已失效")

    def transition(self, project_id, run_id, generation, status, *, event_key, outcome=None, final_message=None):
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            if status not in RUN_TRANSITIONS[run["status"]] and status != run["status"]:
                raise Conflict("运行状态转换无效")
            RunState(run_id=run_id, session_id=run["session_id"], generation=generation, status=status, outcome=outcome)
            if status == 'completed' and db.execute("SELECT 1 FROM agent_inbox WHERE run_id=? AND state='pending' LIMIT 1", (run_id,)).fetchone():
                raise InboxPending('有追加消息等待安全边界，暂不结束本轮')
            db.execute("UPDATE agent_runs SET status=?,outcome=?,updated=? WHERE id=?", (status, outcome, now(), run_id))
            if final_message is not None:
                if status != 'completed':
                    raise ValueError('最终答复必须与本轮完成状态一同发布')
                self._event(db, run['session_id'], run_id, generation, event_key + ':answer',
                            'message', {'role': 'assistant', 'content': final_message})
            if status in {'failed', 'cancelled'}:
                self.discard_inbox(db, run_id)
            self._event(db, run["session_id"], run_id, generation, event_key, "run_state", {"status": status, "outcome": outcome})
            return self._run(db, project_id, run_id)

    def events(self, project_id, session_id, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("事件分页参数无效")
        with self.business.transaction() as db:
            self._session(db, project_id, session_id)
            rows = db.execute("SELECT * FROM agent_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (session_id, after, limit)).fetchall()
            return [{k: json.loads(row[k]) if k == "payload" else row[k] for k in row.keys() if k != "event_key"} for row in rows]

    def begin_model_request(self, project_id, run_id, generation, step, input_hash, *, estimated_tokens=0):
        """Persist BEFORE POST. Returning sent/unknown must never cause another POST."""
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            self.require_executor(run)
            old = db.execute("SELECT * FROM agent_model_requests WHERE run_id=? AND step=?", (run_id, step)).fetchone()
            if old:
                if old["input_hash"] != input_hash:
                    raise Conflict("模型请求输入与历史记录不同")
                return dict(old), False
            key = uid()
            from .budgets import reserve_model
            reserve_model(db, run, estimated_tokens)
            db.execute("""INSERT INTO agent_model_requests(request_id,run_id,step,input_hash,generation,state,created,updated)
                VALUES(?,?,?,?,?,'sent',?,?)""", (key, run_id, step, input_hash, generation, now(), now()))
            db.execute("UPDATE agent_model_requests SET usage=? WHERE request_id=?", (canonical({"reserved_tokens": estimated_tokens}), key))
            return dict(db.execute("SELECT * FROM agent_model_requests WHERE request_id=?", (key,)).fetchone()), True

    def finish_model_request(self, project_id, run_id, generation, request_id, response):
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            self.require_executor(run)
            row = db.execute("SELECT * FROM agent_model_requests WHERE request_id=? AND run_id=?", (request_id, run_id)).fetchone()
            if not row:
                raise KeyError("模型请求不存在")
            if row["state"] == "received":
                if row["normalized_response"] != canonical(response):
                    raise Conflict("模型回包与已记账响应不同")
                return
            if row["generation"] != generation or row["state"] != "sent":
                raise Conflict("模型请求已失效")
            from .budgets import reconcile_model
            reservation = json.loads(row["usage"] or "{}")
            reconcile_model(db, run, reservation, response.get("usage", {}))
            db.execute("UPDATE agent_model_requests SET state='received',normalized_response=?,usage=?,updated=? WHERE request_id=?",
                       (canonical(response), canonical({**response.get("usage", {}), **reservation}), now(), request_id))

    def interrupt_on_startup(self):
        """Local reconciliation only; the caller must have exclusive workspace ownership."""
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM agent_runs WHERE status IN ('queued','running','waiting_jobs','waiting_user')").fetchall():
                generation = row["generation"] + 1
                db.execute("UPDATE agent_runs SET status='interrupted',generation=?,owner=NULL,lease_expires=NULL,updated=? WHERE id=?", (generation, now(), row["id"]))
                self._event(db, row["session_id"], row["id"], generation, f"{row['id']}:restart:{generation}", "run_state", {"status": "interrupted"})
            db.execute("UPDATE agent_model_requests SET state='unknown' WHERE state='sent'")

    def reject_model_request(self, project_id, run_id, generation, request_id, code, message):
        with self.business.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            self.require_executor(run)
            # A complete but unusable response is known, not an unknown POST.
            # Its conservative token reservation stays charged without actual usage.
            changed = db.execute("UPDATE agent_model_requests SET state='rejected',normalized_response=?,updated=? WHERE request_id=? AND run_id=? AND generation=? AND state='sent'",
                (canonical({'error': {'code': code, 'message': message}}), now(), request_id, run_id, generation)).rowcount
            if not changed:
                raise Conflict('模型拒绝回执已失效')

    def create_decision(self, project_id, run_id, generation, payload, *, kind="clarification", scope=None, revision=None, expires=None):
        scope = scope or {}
        expires = expires if expires is not None else time.time() + 3600
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            binding = {"payload": payload, "scope": scope, "revision": revision,
                       "config_revision": run["config"].get("revision"), "generation": generation, "kind": kind}
            payload_hash = digest(binding)
            decision_id = digest({"run": run_id, "binding": binding})
            old = db.execute("SELECT * FROM agent_decisions WHERE id=?", (decision_id,)).fetchone()
            if old:
                return dict(old)
            db.execute("""INSERT INTO agent_decisions(id,run_id,kind,payload_hash,payload,scope,revision,config_revision,
                generation,status,expires,created) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                (decision_id, run_id, kind, payload_hash, canonical(payload), canonical(scope), revision,
                 run["config"].get("revision"), generation, expires, now()))
            self._event(db, run["session_id"], run_id, generation, decision_id + ":required", "decision_required",
                        {"decision_id": decision_id, "payload_hash": payload_hash, "kind": kind, **payload})
            return dict(db.execute("SELECT * FROM agent_decisions WHERE id=?", (decision_id,)).fetchone())

    def reply_decision(self, project_id, run_id, decision_id, request):
        request = ReplyDecision.model_validate(request)
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            row = db.execute("SELECT * FROM agent_decisions WHERE id=? AND run_id=?", (decision_id, run_id)).fetchone()
            if not row:
                raise KeyError("决定不存在")
            self.require_generation(run, row["generation"])
            if row['status'] not in {'pending', 'resolved'}:
                raise Conflict('旧问题已被替代，不能继续回复')
            if row["payload_hash"] != request.payload_hash or row["expires"] <= time.time() or row["config_revision"] != run["config"].get("revision"):
                raise Conflict("决定范围、连接或有效期已变化")
            payload, scope = json.loads(row["payload"]), json.loads(row["scope"])
            if row['kind'] == 'partial_export' and request.option_id == 'allow':
                if scope.get('scope_revision') != run['context'].get('scope_revision'):
                    raise Conflict('导出范围已变化，请重新决定')
                for ref in scope['results']:
                    current = db.execute('SELECT r.revision,t.version_id FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND t.project_id=?', (ref['result_id'], project_id)).fetchone()
                    if not current or (current[0], current[1]) != (ref['revision'], ref['version_id']):
                        raise Conflict('部分导出结果已变化，请重新查询后导出')
            if request.option_id not in {o["id"] for o in payload["options"]}:
                raise ValueError("选项不存在")
            if scope.get("result_id"):
                current = db.execute("SELECT revision FROM results WHERE id=?", (scope["result_id"],)).fetchone()
                if not current or current[0] != row["revision"]:
                    raise Conflict("待决定的结果已修改")
            if row["status"] == "resolved":
                if row["reply"] != request.option_id or row["client_request_id"] != request.client_request_id:
                    raise Conflict("该决定已经回复")
                return dict(row)
            if request.option_id == "continue" and payload.get("budget"):
                from .budgets import DEFAULTS
                budget = payload["budget"]
                if run["limits"].get(budget["key"], DEFAULTS[budget["key"]]) < budget["required"]:
                    raise Conflict("请先增加本轮预算再继续")
            db.execute("UPDATE agent_decisions SET status='resolved',reply=?,client_request_id=? WHERE id=?",
                       (request.option_id, request.client_request_id, decision_id))
            self._event(db, run["session_id"], run_id, run["generation"], decision_id + ":resolved", "decision_resolved",
                        {"decision_id": decision_id, "option_id": request.option_id})
            return dict(db.execute("SELECT * FROM agent_decisions WHERE id=?", (decision_id,)).fetchone())

    def append_event(self, project_id, run_id, generation, event_key, kind, payload):
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            return self._event(db, run["session_id"], run_id, generation, event_key, kind, payload)

    def record_calls(self, project_id, run_id, generation, step, calls):
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            from .budgets import check, save_usage
            count = db.execute("SELECT COUNT(*) FROM agent_calls WHERE run_id=?", (run_id,)).fetchone()[0]
            existing = {r[0] for r in db.execute("SELECT provider_call_id FROM agent_calls WHERE run_id=? AND step=?", (run_id, step))}
            total = count + sum(c["call_id"] not in existing for c in calls)
            check(run, "tool_calls_per_run", total)
            for call in calls:
                key = digest({"run": run_id, "step": step, "call": call["call_id"]})
                old = db.execute("SELECT * FROM agent_calls WHERE id=?", (key,)).fetchone()
                args = canonical(call["arguments"])
                if old and (old["canonical_args"] != args or old["tool"] != call["tool"]):
                    raise Conflict("调用记录与重放参数不同")
                db.execute("""INSERT OR IGNORE INTO agent_calls(id,run_id,step,provider_call_id,tool,version,canonical_args,state)
                    VALUES(?,?,?,?,?,1,?,'validated')""", (key, run_id, step, call["call_id"], call["tool"], args))
            save_usage(db, run, {**run["usage"], "tool_calls": total})

    def call_result(self, run_id, step, call_id):
        rows = self.business.rows("SELECT result_ref FROM agent_calls WHERE run_id=? AND step=? AND provider_call_id=?", (run_id, step, call_id))
        return json.loads(rows[0]["result_ref"]) if rows and rows[0]["result_ref"] else None

    def finish_call(self, project_id, run_id, generation, step, call, result):
        with self.business.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self._run(db, project_id, run_id)
            self.require_generation(run, generation)
            row = db.execute("SELECT * FROM agent_calls WHERE run_id=? AND step=? AND provider_call_id=?", (run_id, step, call["call_id"])).fetchone()
            if not row:
                raise KeyError("调用未记账")
            if row["result_ref"] and row["result_ref"] != canonical(result):
                raise Conflict("调用结果已存在")
            db.execute("UPDATE agent_calls SET state='finished',result_ref=? WHERE id=?", (canonical(result), row["id"]))
            self._event(db, run["session_id"], run_id, generation, row["id"] + ":finished", "tool_finished",
                        {"call_id": call["call_id"], "tool": call["tool"], "step": step, "result": result})

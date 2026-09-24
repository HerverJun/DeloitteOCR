"""Atomic operation/effect/job linkage in the business SQLite transaction."""
from __future__ import annotations

import json
import time

from ocr_workbench.store import Conflict, now, uid
from .store import canonical, digest


def semantic_arguments(arguments):
    value = dict(arguments)
    for name in ("page_numbers", "version_ids", "engines", "target_ids", "results", "jobs"):
        if name in value and isinstance(value[name], list):
            value[name] = sorted(value[name], key=canonical)
    return value


class Operations:
    def __init__(self, agent, policy):
        self.agent, self.policy = agent, policy
        self.store = agent.business

    def submit(self, context, tool, arguments, effect, *, fingerprint=None, retry_intent=None, fault=None, cost=None, identity_by_input=False, deferred_finish=False):
        run, generation = context["run"], context["generation"]
        args = semantic_arguments(arguments)
        key = digest({"tool": tool, "version": 1, "arguments": args, "retry_intent": retry_intent})
        with self.store.file_lock, self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.agent._run(db, run["project_id"], run["id"])
            self.agent.require_generation(current, generation)
            self.agent.require_executor(current)
            pending = db.execute("SELECT 1 FROM agent_inbox WHERE run_id=? AND state='pending' LIMIT 1", (run['id'],)).fetchone()
            linked = db.execute('SELECT operation_id FROM agent_calls WHERE run_id=? AND step=? AND provider_call_id=?',
                                (run['id'], context['step'], context['call_id'])).fetchone()
            if pending and (not linked or linked[0] is None):
                from .policy import PolicyDenied
                raise PolicyDenied('cancelled', '追加指令已接收；未提交的旧操作已跳过')
            if run.get("owner") and (current["owner"], current["fencing_token"]) != (run["owner"], run["fencing_token"]):
                raise Conflict("操作执行权已转移，旧调用不能提交")
            competing = db.execute("""SELECT o.run_id FROM agent_operations o JOIN agent_runs r ON r.id=o.run_id
                WHERE o.project_id=? AND o.run_id<>? AND r.status NOT IN ('completed','failed','cancelled') LIMIT 1""",
                (run["project_id"], run["id"])).fetchone()
            if competing:
                from .policy import PolicyDenied
                raise PolicyDenied("business_failed", "本项目已有写入中的助手运行；请等待完成或停止该运行")
            self.policy.authorize(current, generation, tool, arguments)
            if 'scope_revision' in current['context']:
                key = digest({'operation': key, 'scope_revision': current['context']['scope_revision']})
            signature = digest({"arguments": args, "config": current["config"], "inputs": fingerprint(db) if fingerprint else None})
            if identity_by_input:
                key = digest({"operation": key, "input": signature})
            previous = db.execute("SELECT * FROM agent_operations WHERE run_id=? AND operation_key=?", (run["id"], key)).fetchone()
            if previous:
                if previous["input_hash"] != signature:
                    raise Conflict("同一操作输入版本已变化，请显式重试或新建运行")
                result = json.loads(previous["result"])
                operation_id = previous["id"]
            else:
                if cost:
                    from .budgets import reserve_operation
                    reserve_operation(db, current, **cost)
                operation_id = uid()
                db.execute("""INSERT INTO agent_operations(id,project_id,run_id,operation_key,input_hash,generation,state,created,updated)
                    VALUES(?,?,?,?,?,?,'pending',?,?)""", (operation_id, run["project_id"], run["id"], key, signature, generation, now(), now()))
                if fault:
                    fault("before_effect")
                result, jobs = effect(db, operation_id)
                if fault:
                    fault("after_effect")
                for job in jobs:
                    db.execute("INSERT INTO agent_job_links VALUES(?,?,?,?,?,?)", (operation_id, job["kind"], job["job_id"], job["ownership"], job.get("input_revision"), job["state"]))
                if jobs and current['context'].get('selection', {}).get('permissions', {}).get('allow_retry_failed'):
                    for action in ('retry', 'resume'):
                        scope = {'jobs': sorted(j['kind'] + ':' + j['job_id'] for j in jobs), 'previous_run_id': run['id'],
                                 'operation_id': operation_id, 'action': action}
                        if 'scope_revision' in current['context']:
                            scope['scope_revision'] = current['context']['scope_revision']
                        db.execute("INSERT INTO agent_grants VALUES(?,?,?,?,?,?,?,?,0)",
                            (uid(), run['project_id'], run['id'], 'scoped_failed_subset', digest(scope), canonical(scope), 'bound_ui_scope', time.time() + 86400))
                result = {**result, "operation_id": operation_id}
                # A synchronous effect with no linked jobs is complete in this
                # transaction. Export is different: its effect only reserves a
                # staging artifact, so the caller finishes it after publication.
                state = 'submitted' if jobs or deferred_finish else 'finished'
                db.execute("UPDATE agent_operations SET state=?,result=?,updated=? WHERE id=?", (state, canonical(result), now(), operation_id))
            db.execute("UPDATE agent_calls SET operation_id=? WHERE run_id=? AND step=? AND provider_call_id=?",
                       (operation_id, run["id"], context["step"], context["call_id"]))
        if fault:
            fault("after_commit")
        return result

"""Observe only linked real jobs; waiting never calls the model or holds GPU."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from ocr_workbench.store import now

from .contracts import ToolResult
from .store import canonical, digest
from .policy import PolicyDenied

TERMINAL = {"succeeded", "failed", "cancelled"}


class JobBridge:
    def __init__(self, agent, services):
        self.agent, self.services = agent, services
        self.store = agent.business

    def operation_result(self, project_id, run_id, operation_id, *, require_terminal=False):
        with self.store.transaction() as db:
            operation = db.execute("SELECT * FROM agent_operations WHERE id=? AND project_id=? AND run_id=?", (operation_id, project_id, run_id)).fetchone()
            if not operation:
                raise PolicyDenied("scope_denied", "操作不属于当前运行")
            jobs, results, reviews, blocked, budget_waits, references = [], [], [], [], [], []
            pages = {"requested": [], "completed": [], "failed": [], "pending": [], "uncovered": []}
            for link in db.execute("SELECT * FROM agent_job_links WHERE operation_id=? ORDER BY job_kind,job_id", (operation_id,)).fetchall():
                if link["job_kind"] == "pdf_stage":
                    record = db.execute("""SELECT s.status,s.output,p.id page_id,p.page_number,p.document_id FROM document_stages s
                        JOIN pages p ON p.id=s.page_id JOIN documents d ON d.id=p.document_id
                        WHERE s.id=? AND d.project_id=?""", (link["job_id"], project_id)).fetchone()
                    result_ids = []
                    if record:
                        output = json.loads(record["output"] or "{}")
                        wait = output.get('agent_budget_wait')
                        if record['status'] == 'paused' and wait and wait['run_id'] == run_id:
                            budget_waits.append({**wait, 'stage_id': link['job_id']})
                        if output.get("result_id"):
                            result_ids.append(output["result_id"])
                elif link['job_kind'] == 'visual_review':
                    record = db.execute("""SELECT t.status,NULL result_id,NULL page_id,NULL page_number,NULL document_id,m.raw_response,m.summary
                        FROM tasks t JOIN multimodal_requests m ON m.task_id=t.id WHERE t.id=? AND t.project_id=? AND t.kind='multimodal'""",
                        (link['job_id'], project_id)).fetchone()
                    result_ids = []
                    if record:
                        evidence = db.execute("""SELECT e.reference FROM agent_evidence e JOIN agent_runs r ON r.id=e.run_id
                            JOIN agent_sessions s ON s.id=r.session_id WHERE s.project_id=?
                            AND json_extract(e.reference,'$.target_id')=? ORDER BY e.created DESC LIMIT 1""",
                            (project_id, 'visual-job:' + link['job_id'])).fetchone()
                        if evidence and len(references) < 10:
                            references.append(json.loads(evidence[0]))
                        reviews.append({'job_id': link['job_id'], 'summary': (record['summary'] or '')[:1000],
                            'evidence_ref_id': json.loads(evidence[0])['ref_id'] if evidence else None,
                            'response_saved': record['raw_response'] is not None,
                            'proposals': [dict(r) for r in db.execute('SELECT id,status,state FROM multimodal_proposals WHERE task_id=? LIMIT 15', (link['job_id'],))],
                            'automatic_adoption': False})
                else:
                    record = db.execute("""SELECT t.status,t.result_id,p.id page_id,p.page_number,p.document_id FROM tasks t
                        LEFT JOIN pages p ON p.image_id=t.image_id WHERE t.id=? AND t.project_id=? AND t.kind=?""",
                        (link["job_id"], project_id, 'fusion' if link['job_kind'] == 'fusion' else 'ocr')).fetchone()
                    result_ids = [record["result_id"]] if record and record["result_id"] else []
                if not record:
                    raise PolicyDenied("not_found", "关联后台任务已不存在")
                state = record["status"]
                if state == "waiting_unlock":
                    state = "paused"
                if state in {'paused', 'interrupted'}:
                    blocked.append(link['job_id'])
                elif state == 'waiting_gpu' and link['job_kind'] == 'pdf_stage':
                    children = db.execute('SELECT t.status FROM page_ocr_inputs i JOIN tasks t ON t.id=i.task_id WHERE i.stage_id=?', (link['job_id'],)).fetchall()
                    if children and any(t[0] in {'paused', 'interrupted'} for t in children) and not any(t[0] in {'queued', 'running'} for t in children):
                        blocked.append(link['job_id'])
                jobs.append({"operation_id": operation_id, "kind": link["job_kind"], "job_id": link["job_id"],
                             "ownership": link["ownership"], "input_revision": link["input_revision"], "state": state})
                if state != link["last_state"]:
                    db.execute("UPDATE agent_job_links SET last_state=? WHERE operation_id=? AND job_kind=? AND job_id=?", (state, operation_id, link["job_kind"], link["job_id"]))
                page_ref = {"page_id": record["page_id"], "document_id": record["document_id"], "page_number": record["page_number"]}
                usable, incomplete = [], False
                for result_id in dict.fromkeys(result_ids):
                    result = db.execute("SELECT r.id result_id,r.revision,t.version_id,r.edited,r.original FROM results r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND t.project_id=?", (result_id, project_id)).fetchone()
                    if result:
                        data = json.loads(result["edited"])
                        metadata = data.get("document", json.loads(result["original"]).get("document", {}))
                        incomplete = incomplete or bool(metadata.get("native_only_incomplete") or metadata.get("unprocessed_regions"))
                        usable.append({k: result[k] for k in ("result_id", "revision", "version_id")})
                results.extend(usable)
                if page_ref["page_id"]:
                    pages["requested"].append(page_ref)
                    bucket = "pending" if state not in TERMINAL else "failed" if state != "succeeded" else "uncovered" if not usable or incomplete else "completed"
                    pages[bucket].append(page_ref)
            # Multiple engines may refer to one page. Count it once, conservatively:
            # pending/failed/uncovered takes precedence over completed.
            assigned = set()
            for name in ("pending", "failed", "uncovered", "completed"):
                unique = {p["page_id"]: p for p in pages[name] if p["page_id"] not in assigned}
                pages[name] = list(unique.values())
                assigned.update(unique)
            pages["requested"] = list({p["page_id"]: p for p in pages["requested"]}.values())
            results = list({r["result_id"]: r for r in results}.values())
            pending = any(j["state"] not in TERMINAL for j in jobs)
            runnable = any(j['state'] not in TERMINAL and j['job_id'] not in blocked for j in jobs)
            failures = any(j["state"] != "succeeded" for j in jobs) or bool(pages["uncovered"]) or any(not r['response_saved'] for r in reviews)
            result = ToolResult(status="partial" if pending or failures else "success",
                                summary=f"{len(jobs)} 个任务，完整覆盖 {len(pages['completed'])} 页，未覆盖 {len(pages['uncovered'])} 页，失败/取消 {sum(j['state'] in {'failed','cancelled'} for j in jobs)}",
                                data={"operation_id": operation_id, "waiting_jobs": pending, 'blocked_job_ids': blocked, 'budget_waits': budget_waits,
                                      "coverage": {name + "_count": len(items) for name, items in pages.items()},
                                      "results": results[:20], "reviews": reviews[:10], "job_count": len(jobs)}, job_refs=jobs[:20], evidence_refs=references,
                                truncated=len(jobs) > 20 or len(results) > 20).model_dump(mode="json")
            # Coverage is a durable projection, independent of graph position.
            projection = canonical({"operation_id": operation_id, "coverage": pages, "results": results, "tool_result": result})
            if operation["result"] != projection:
                db.execute("UPDATE agent_operations SET state=?,result=?,updated=? WHERE id=?", ("waiting" if pending else "finished", projection, now(), operation_id))
                run = self.agent._run(db, project_id, run_id)
                combined = {name: set() for name in pages}
                for item in db.execute("SELECT result FROM agent_operations WHERE run_id=? AND result IS NOT NULL", (run_id,)):
                    coverage = json.loads(item[0]).get("coverage", {})
                    for name in combined:
                        combined[name].update(p["page_id"] for p in coverage.get(name, []))
                settled = set()
                for name in ("pending", "failed", "uncovered", "completed"):
                    combined[name] -= settled
                    settled.update(combined[name])
                db.execute("UPDATE agent_runs SET coverage=? WHERE id=?", (canonical({name + "_count": len(ids) for name, ids in combined.items()}), run_id))
                self.agent._event(db, run["session_id"], run_id, run["generation"], operation_id + ":progress:" + digest(projection),
                                  "job_progress", {"operation_id": operation_id, "summary": result["summary"], "coverage": result["data"]["coverage"], "jobs": jobs[:20]})
            return None if require_terminal and runnable else result

    def no_progress(self, state):
        call = state['calls'][state['cursor']]
        rows = self.store.rows("""SELECT o.id,o.updated FROM agent_calls c JOIN agent_operations o ON o.id=c.operation_id
            WHERE c.run_id=? AND c.step=? AND c.provider_call_id=? AND o.state='waiting'""", (state['run_id'], state['step'], call['call_id']))
        if not rows:
            return None
        run = self.agent.run(state['project_id'], state['run_id'])
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(rows[0]['updated'])).total_seconds()
        if elapsed < run['limits'].get('no_job_progress_seconds', 1800):
            return None
        return {'operation_id': rows[0]['id'], 'last_progress': rows[0]['updated']}

    def acknowledge_wait(self, state):
        with self.store.transaction() as db:
            run = self.agent._run(db, state['project_id'], state['run_id'])
            self.agent.require_generation(run, state['generation'])
            self.agent.require_executor(run)
            call = state['calls'][state['cursor']]
            db.execute("""UPDATE agent_operations SET updated=? WHERE id=(SELECT operation_id FROM agent_calls
                WHERE run_id=? AND step=? AND provider_call_id=?)""", (now(), run['id'], state['step'], call['call_id']))

    def result_for_call(self, state, *, require_terminal=True):
        call = state["calls"][state["cursor"]]
        rows = self.store.rows("SELECT operation_id FROM agent_calls WHERE run_id=? AND step=? AND provider_call_id=?", (state["run_id"], state["step"], call["call_id"]))
        if not rows or not rows[0]["operation_id"]:
            raise PolicyDenied("not_found", "调用缺少持久操作关联")
        return self.operation_result(state["project_id"], state["run_id"], rows[0]["operation_id"], require_terminal=require_terminal)

    def cancel_owned(self, project_id, run_id):
        # Restrict to direct created links. Reused work is never cancelled.
        with self.store.file_lock, self.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            links = db.execute("""SELECT j.* FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
                WHERE o.run_id=? AND o.project_id=? AND j.ownership='created'""", (run_id, project_id)).fetchall()
            affected = []
            for link in links:
                if link["job_kind"] == "pdf_stage":
                    changed = db.execute("UPDATE document_stages SET status='cancelled',phase='已请求取消' WHERE id=? AND status IN ('queued','running','paused','interrupted','waiting_unlock','waiting_gpu')", (link["job_id"],)).rowcount
                    # Child tasks exist only for this created stage. Preserve unrelated page work.
                    db.execute("""UPDATE tasks SET status='cancelled',phase='已请求取消' WHERE id IN
                        (SELECT task_id FROM page_ocr_inputs WHERE stage_id=?) AND status IN ('queued','running','paused','interrupted')""", (link["job_id"],))
                else:
                    changed = db.execute("UPDATE tasks SET status='cancelled',phase='已请求取消' WHERE id=? AND project_id=? AND status IN ('queued','running','paused','interrupted')", (link["job_id"], project_id)).rowcount
                if changed:
                    affected.append({"kind": link["job_kind"], "job_id": link["job_id"]})
        return affected

    def resume_budget(self, state):
        from .budgets import check
        with self.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self.agent._run(db, state['project_id'], state['run_id'])
            self.agent.require_generation(run, state['generation'])
            self.agent.require_executor(run)
            call = state['calls'][state['cursor']]
            stages = db.execute("""SELECT DISTINCT s.id,s.output FROM agent_calls c JOIN agent_job_links j ON j.operation_id=c.operation_id
                JOIN document_stages s ON s.id=j.job_id WHERE c.run_id=? AND c.step=? AND c.provider_call_id=?
                AND j.job_kind='pdf_stage' AND j.ownership='created' AND s.status='paused'""", (run['id'], state['step'], call['call_id'])).fetchall()
            for stage in stages:
                output = json.loads(stage['output'] or '{}')
                wait = output.get('agent_budget_wait')
                if not wait or wait['run_id'] != run['id']:
                    continue
                check(run, wait['key'], wait['required'])
                del output['agent_budget_wait']
                db.execute("UPDATE document_stages SET status='queued',phase='预算已增加，等待区域处理',output=?,error=NULL WHERE id=?", (canonical(output), stage['id']))
        self.services.documents.wake.set()

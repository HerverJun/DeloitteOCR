"""Durable budget accounting in the same transaction as each charged effect."""
import json

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from ocr_workbench.store import Conflict, now
from .store import canonical, digest

DEFAULTS = {"model_requests_per_run": 12, "tool_calls_per_run": 40,
            "pages_per_run": 1000, "engine_jobs_per_run": 400, "tokens_per_run": 64000}


class BudgetExceeded(Exception):
    code = "budget_exceeded"

    def __init__(self, key, required):
        self.key, self.required = key, required
        super().__init__(f"本轮 {key} 预算不足，需要至少 {required}；请增加预算或停止。")


class IncreaseBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=1)
    limits: dict[str, StrictInt]


def check(run, key, required):
    if required > run["limits"].get(key, DEFAULTS[key]):
        raise BudgetExceeded(key, required)


def save_usage(db, run, usage):
    db.execute("UPDATE agent_runs SET usage=?,updated=? WHERE id=?", (canonical(usage), now(), run["id"]))


def reserve_model(db, run, estimate):
    usage = dict(run["usage"])
    count = db.execute("SELECT COUNT(*) FROM agent_model_requests WHERE run_id=?", (run["id"],)).fetchone()[0]
    check(run, "model_requests_per_run", count + 1)
    check(run, "tokens_per_run", usage.get("tokens_charged", 0) + estimate)
    usage.update(model_requests=count + 1, tokens_charged=usage.get("tokens_charged", 0) + estimate,
                 tokens_estimated=usage.get("tokens_estimated", 0) + estimate,
                 tokens_unconfirmed=usage.get("tokens_unconfirmed", 0) + estimate,
                 model_requests_unconfirmed=usage.get("model_requests_unconfirmed", 0) + 1)
    save_usage(db, run, usage)


def reconcile_model(db, run, reservation, actual):
    usage = dict(run["usage"])
    estimate = reservation.get("reserved_tokens", 0)
    # Missing usage retains the conservative reservation, never implies free use.
    if all(type(actual.get(k)) is int and actual[k] >= 0 for k in ("input_tokens", "output_tokens")):
        total = actual["input_tokens"] + actual["output_tokens"]
        usage["tokens_charged"] = usage.get("tokens_charged", 0) - estimate + total
        usage["tokens_actual"] = usage.get("tokens_actual", 0) + total
        usage["tokens_unconfirmed"] = usage.get("tokens_unconfirmed", 0) - estimate
        usage["model_requests_unconfirmed"] = max(0, usage.get("model_requests_unconfirmed", 0) - 1)
    save_usage(db, run, usage)


def reserve_operation(db, run, pages, jobs):
    usage = dict(run["usage"])
    # Page visits accumulate across distinct operations; batching cannot reset them.
    page_count = usage.get("pages", 0) + pages
    job_count = usage.get("engine_jobs", 0) + jobs
    check(run, "pages_per_run", page_count)
    check(run, "engine_jobs_per_run", job_count)
    usage.update(pages=page_count, engine_jobs=job_count)
    save_usage(db, run, usage)


def reserve_stage_children(db, stage_id, count):
    """Called inside the transaction that creates region tasks; replay is free.

    The stage itself was charged at submission. Each actual OCR child is another
    engine job. Existing UI-only stages have no agent owner and keep their budget.
    """
    from .store import AgentStore
    owner = db.execute("""SELECT o.run_id,o.project_id FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
        WHERE j.job_kind='pdf_stage' AND j.job_id=? AND j.ownership='created' ORDER BY o.created,o.id LIMIT 1""", (stage_id,)).fetchone()
    if owner is None:
        return True
    run = AgentStore._run(db, owner['project_id'], owner['run_id'])
    try:
        reserve_operation(db, run, 0, count)
    except BudgetExceeded as error:
        row = db.execute('SELECT output FROM document_stages WHERE id=?', (stage_id,)).fetchone()
        output = json.loads(row[0] or '{}')
        output['agent_budget_wait'] = {'run_id': run['id'], 'key': error.key, 'required': error.required, 'children': count}
        db.execute("UPDATE document_stages SET status='paused',phase='等待增加任务预算',output=?,error=? WHERE id=? AND status='running'",
                   (canonical(output), str(error), stage_id))
        return False
    return True


def increase(agent, project_id, run_id, body):
    request = IncreaseBudget.model_validate(body)
    if not request.limits or set(request.limits) - DEFAULTS.keys():
        raise ValueError("仅允许增加本轮请求、工具、页数、任务和 token 预算")
    if any(v <= 0 or v > 10000000 for v in request.limits.values()):
        raise ValueError("预算必须为 1 到 10000000 的整数")
    with agent.business.transaction() as db:
        db.execute("BEGIN IMMEDIATE")
        run = agent._run(db, project_id, run_id)
        event_key = "budget:" + digest({"run": run_id, "request": request.client_request_id})
        payload = {"limits": request.limits, "generation": request.generation}
        old = db.execute("SELECT payload FROM agent_events WHERE session_id=? AND event_key=?", (run["session_id"], event_key)).fetchone()
        if old:
            if json.loads(old[0]) != payload:
                raise Conflict("预算请求编号对应不同内容")
            return run
        agent.require_generation(run, request.generation)
        if any(v <= run["limits"].get(k, DEFAULTS[k]) for k, v in request.limits.items()):
            raise ValueError("新预算必须大于当前上限")
        db.execute("UPDATE agent_runs SET limits=?,updated=? WHERE id=?", (canonical({**run["limits"], **request.limits}), now(), run_id))
        agent._event(db, run["session_id"], run_id, run["generation"], event_key, "budget_updated", payload)
        return agent._run(db, project_id, run_id)

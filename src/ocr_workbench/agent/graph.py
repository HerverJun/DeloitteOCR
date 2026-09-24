"""StateGraph owns node scheduling; business records own effects and authority."""
from __future__ import annotations

import json

from .checkpoints import disable_tracing
disable_tracing()
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.errors import GraphInterrupt

from .contracts import ProviderCall, ToolResult, validate_result_pairing
from .policy import PolicyDenied
from .providers import ProviderFault
from .state import AgentState
from .store import digest, InboxPending
from .budgets import BudgetExceeded


def tool_error(code, message):
    return ToolResult(status="error", summary=message[:1000], error={"code": code, "message": message[:1000],
                      "retryable": False, "required_action": "decide" if code == "authorization_required" else "correct_arguments"}).model_dump(mode="json")


class GraphServices:
    def __init__(self, agent, registry, model, *, jobs=None, inbox=None, prepare_context=None):
        self.agent, self.registry, self.model, self.jobs, self.inbox = agent, registry, model, jobs, inbox
        self.prepare_context = prepare_context

    def current(self, state):
        if state["graph_version"] != "ocr-agent-graph-v1" or state["state_version"] != 1:
            raise PolicyDenied("config_changed", "图状态版本不兼容，不能自动恢复")
        run = self.agent.run(state["project_id"], state["run_id"])
        self.agent.require_generation(run, state["generation"])
        self.agent.require_executor(run)
        return run

    def project_status(self, state, status, suffix, outcome=None, final_message=None):
        run = self.current(state)
        if run["status"] != status:
            self.agent.transition(run["project_id"], run["id"], state["generation"], status,
                                  event_key=f"{run['id']}:{state['generation']}:{state['step']}:{state.get('cursor', 0)}:{suffix}", outcome=outcome, final_message=final_message)

    def decision(self, state, question, reason, *, budget=None, job_wait=None):
        run = self.current(state)
        payload = {"question": question, "options": [{"id": "continue", "label": "明确继续（将发起新请求）"}, {"id": "stop", "label": "停止助手"}]}
        scope = {"step": state["step"], "cursor": state.get("cursor", 0)}
        if reason.startswith('summary_'):
            scope['summary_attempt'] = state.get('context_summary_attempt', 0)
        if job_wait:
            scope['job_wait'] = job_wait
            payload['options'][0]['label'] = '继续等待（不会重试任务）'
        if budget:
            payload["budget"] = {"key": budget.key, "required": budget.required,
                                 "current": run["limits"].get(budget.key)}
            scope["limits"] = run["limits"]
            payload["options"][0]["label"] = "增加预算后继续"
        if reason == 'context_limit':
            # Increasing a run's token budget does not enlarge the provider's
            # context window. Do not offer an ineffective retry that loops.
            payload['options'] = [{'id': 'stop', 'label': '保留记录并停止；调整目标后新建会话'}]
        decision = self.agent.create_decision(state["project_id"], state["run_id"], state["generation"], {
            **payload}, kind=reason, scope=scope)
        return {"decision_id": decision["id"], "wait_reason": reason}


def build_graph(services, saver):
    def repeated_error(run, tool):
        limit = max(1, run['limits'].get('same_tool_error_limit', 2))
        prior = services.agent.business.rows("SELECT result_ref FROM agent_calls WHERE run_id=? AND tool=? AND state='finished' ORDER BY step DESC,rowid DESC LIMIT ?",
                                             (run['id'], tool, limit))
        errors = [json.loads(row['result_ref']).get('error') for row in prior]
        if len(errors) == limit and all(error and error['code'] != 'cancelled' and error['code'] == errors[0]['code'] for error in errors):
            return f"{tool} 连续 {limit} 次遇到相同问题：{errors[0]['message']}。已停止后续调用；请调整范围或参数后重新开始。已有结果和导出保留。"
        return None

    def has_inbox(state):
        return services.inbox is not None and services.inbox.pending(state['run_id'])

    async def receive_inbox(state):
        services.current(state)
        return {'messages': services.inbox.receive(state)} if services.inbox else {}

    async def prepare_context(state):
        run = services.current(state)
        services.project_status(state, 'running', 'preparing-context')
        try:
            updates = await services.prepare_context(state, run)
        except BudgetExceeded as error:
            return services.decision(state, str(error), 'summary_budget', budget=error)
        except ProviderFault as error:
            if error.code in {'summary_response_unknown', 'context_limit'}:
                return services.decision(state, str(error), error.code)
            raise
        fault = updates.pop('_context_fault', None)
        if fault:
            updates.update(services.decision(state, fault, 'context_limit'))
        return updates

    async def model(state):
        run = services.current(state)
        if services.prepare_context and state.get('context_prepared') != {'run_id': run['id'], 'step': state['step']}:
            # Older compatible checkpoints can point directly at model. Bring
            # them through the new checkpointed preparation node before a POST.
            return {'wait_reason': 'prepare_context', 'decision_id': None}
        services.project_status(state, "running", "running")
        try:
            reply = await services.model(state, run)
        except BudgetExceeded as error:
            return services.decision(state, str(error), "budget", budget=error)
        except ProviderFault as error:
            if error.code == "response_unknown":
                return services.decision(state, str(error), "response_unknown")
            if error.code == 'context_limit':
                return services.decision(state, str(error), 'context_limit')
            if error.code == 'invalid_arguments':
                rejected = services.agent.business.rows("""SELECT COUNT(*) count FROM agent_model_requests WHERE run_id=?
                    AND state='rejected' AND json_extract(normalized_response,'$.error.code')='invalid_arguments'""", (run['id'],))[0]['count']
                limit = run['limits'].get('argument_corrections', 2)
                if rejected and rejected <= limit:
                    text = '上一响应的工具参数未通过整批校验，未执行任何工具。请按工具 schema 修正参数后重新生成完整响应。'
                    key = f"{run['id']}:{state['step']}:argument-correction"
                    services.agent.append_event(run['project_id'], run['id'], state['generation'], key, 'error', {'code': 'invalid_arguments', 'message': text, 'correction': rejected, 'limit': limit})
                    return {'messages': [{'id': key, 'role': 'user', 'content': '[工作台校验] ' + text}], 'step': state['step'] + 1,
                            'calls': [], 'results': [], 'cursor': 0, 'wait_reason': 'retry_arguments', 'decision_id': None}
                text = '工具参数修正次数已用完，未执行无效调用。请调整目标后重新开始。'
                key = f"{run['id']}:{state['step']}:argument-limit"
                services.agent.append_event(run['project_id'], run['id'], state['generation'], key, 'message', {'role': 'assistant', 'content': text})
                return {'messages': [{'id': key, 'role': 'assistant', 'content': text, 'calls': []}], 'calls': [],
                        'wait_reason': 'error_limit', 'decision_id': None, 'final_text': text}
            raise
        services.current(state)
        reply = dict(reply)
        context_updates = reply.pop('_context_updates', [])
        context_summary = reply.pop('_context_summary', state.get('context_summary'))
        # Tool-step commentary may be shown immediately. A final answer is only
        # published after authoritative task/coverage/artifact settlement.
        if reply['calls']:
            services.agent.append_event(run["project_id"], run["id"], state["generation"], reply["id"] + ":message", "message",
                                        {"role": "assistant", "content": reply["content"]})
        return {"messages": context_updates + [reply], "context_summary": context_summary,
                "calls": reply["calls"], "results": [], "cursor": 0,
                "batch_error": None, "error_stop": None, "decision_id": None, "wait_reason": None, "final_text": reply["content"]}

    def after_model(state):
        if state.get('wait_reason') == 'prepare_context':
            return 'prepare_context'
        if state.get('wait_reason') == 'retry_arguments':
            return 'receive_inbox'
        return "await_user" if state.get("decision_id") else "validate_batch" if state["calls"] else "finalize"

    async def validate_batch(state):
        run = services.current(state)
        error = None
        try:
            services.registry.validate_batch(run, state["generation"], state["calls"])
        except PolicyDenied as caught:
            error = tool_error(caught.code, str(caught))
        # Provider normalization already guarantees the complete schema batch.
        try:
            services.agent.record_calls(run["project_id"], run["id"], state["generation"], state["step"], state["calls"])
        except BudgetExceeded as error:
            return services.decision(state, str(error), "tool_budget", budget=error)
        return {"batch_error": error}

    async def execute_next(state):
        run = services.current(state)
        call = state["calls"][state["cursor"]]
        old = services.agent.call_result(run["id"], state["step"], call["call_id"])
        if old is not None:
            result = old
        elif state.get('error_stop'):
            result = tool_error('cancelled', '连续同类错误已达到上限，此项尚未提交的调用未执行')
        elif has_inbox(state) and not services.agent.business.rows("SELECT 1 FROM agent_calls WHERE run_id=? AND step=? AND provider_call_id=? AND operation_id IS NOT NULL",
                                                                  (run['id'], state['step'], call['call_id'])):
            result = tool_error('cancelled', '用户已追加新指令，此项尚未提交的旧调用已跳过')
            result['data']['superseded_by_inbox'] = True
        elif state.get("batch_error"):
            result = state["batch_error"]
        else:
            started_key = digest({"run": run["id"], "step": state["step"], "call": call["call_id"]})
            services.agent.append_event(run["project_id"], run["id"], state["generation"], started_key + ":started", "tool_started",
                                        {"call_id": call["call_id"], "tool": call["tool"], "step": state["step"]})
            try:
                result = await services.registry.execute(run, state["generation"], ProviderCall.model_validate(call),
                    {"run": run, "generation": state["generation"], "step": state["step"], "call_id": call["call_id"]})
            except GraphInterrupt:
                raise
            except BudgetExceeded as error:
                return services.decision(state, str(error), "operation_budget", budget=error)
            except PolicyDenied as error:
                result = tool_error(error.code, str(error))
                if error.code == 'cancelled' and has_inbox(state):
                    result['data']['superseded_by_inbox'] = True
        services.current(state)
        if result["status"] == "needs_user":
            return {"decision_id": result["data"]["decision_id"], "wait_reason": result['data'].get('decision_kind', 'tool')}
        if result["data"].get("waiting_jobs") or any(job["state"] not in {"succeeded", "failed", "cancelled"} for job in result["job_refs"]):
            # Submission handler must have persisted its operation/job links.
            return {"wait_reason": "jobs"}
        services.agent.finish_call(run["project_id"], run["id"], state["generation"], state["step"], call, result)
        return {"results": state["results"] + [{"call_id": call["call_id"], "result": result}], "cursor": state["cursor"] + 1,
                'error_stop': state.get('error_stop') or repeated_error(run, call['tool'])}

    def after_execute(state):
        if state.get("decision_id"):
            return "await_user"
        if state.get("wait_reason") == "jobs":
            return "await_jobs"
        return "execute_next" if state["cursor"] < len(state["calls"]) else "collect_results"

    async def await_jobs(state):
        services.current(state)
        if services.jobs is None:
            raise PolicyDenied("unsupported_capability", "任务桥接器不可用")
        result = services.jobs.result_for_call(state)
        if result is None and not has_inbox(state):
            stalled = services.jobs.no_progress(state)
            if stalled:
                return services.decision(state, '后台任务长时间没有状态进展。请核对工作台任务队列和诊断；已有 OCR 任务仍保留。', 'no_job_progress', job_wait=stalled)
            services.project_status(state, "waiting_jobs", "waiting-jobs")
            interrupt({"kind": "jobs", "run_id": state["run_id"], "generation": state["generation"]})
        services.current(state)
        result = services.jobs.result_for_call(state)
        if result is None and has_inbox(state):
            # The submitted operation keeps running and remains linked. Deliver a
            # truthful pending result so the new instruction can reach a boundary.
            result = services.jobs.result_for_call(state, require_terminal=False)
        if result is None:
            raise PolicyDenied("business_failed", "后台任务尚未完成，恢复通知无效")
        if result['data'].get('budget_waits'):
            wait = result['data']['budget_waits'][0]
            error = BudgetExceeded(wait['key'], wait['required'])
            return services.decision(state, str(error), 'child_job_budget', budget=error)
        services.project_status(state, "running", "jobs-resumed")
        call = state["calls"][state["cursor"]]
        services.agent.finish_call(state["project_id"], state["run_id"], state["generation"], state["step"], call, result)
        return {"results": state["results"] + [{"call_id": call["call_id"], "result": result}], "cursor": state["cursor"] + 1, "wait_reason": None,
                'error_stop': state.get('error_stop') or repeated_error(services.current(state), call['tool'])}

    async def await_user(state):
        run = services.current(state)
        rows = services.agent.business.rows("SELECT * FROM agent_decisions WHERE id=? AND run_id=?", (state["decision_id"], run["id"]))
        if not rows:
            raise PolicyDenied("not_found", "待回复问题不存在")
        decision = rows[0]
        if state['wait_reason'] == 'tool' and has_inbox(state):
            with services.agent.business.transaction() as db:
                db.execute("UPDATE agent_decisions SET status='obsolete' WHERE id=?", (decision['id'],))
            if run['status'] == 'waiting_user':
                services.project_status(state, 'queued', 'inbox-queued:' + decision['id'])
                services.project_status(state, 'running', 'inbox-resumed:' + decision['id'])
            call = state['calls'][state['cursor']]
            result = tool_error('cancelled', '旧澄清问题已被追加指令替代；追加消息未视为授权回复')
            result['data']['superseded_by_inbox'] = True
            services.agent.finish_call(state['project_id'], run['id'], state['generation'], state['step'], call, result)
            return {'results': state['results'] + [{'call_id': call['call_id'], 'result': result}], 'cursor': state['cursor'] + 1,
                    'decision_id': None, 'wait_reason': None}
        if decision["status"] != "resolved":
            services.project_status(state, "waiting_user", "waiting-user:" + state["decision_id"])
            interrupt({"kind": "user", "decision_id": decision["id"], "run_id": run["id"], "generation": state["generation"]})
        # Query again: the resume payload is only a wakeup, never authorization.
        services.current(state)
        decision = services.agent.business.rows("SELECT * FROM agent_decisions WHERE id=?", (decision["id"],))[0]
        if decision["status"] != "resolved":
            raise PolicyDenied("authorization_required", "必须先回复对应问题")
        if services.current(state)["status"] == "waiting_user":
            services.project_status(state, "queued", "decision-queued:" + decision["id"])
        if services.current(state)["status"] == "queued":
            services.project_status(state, "running", "decision-resumed:" + decision["id"])
        if state['wait_reason'] == 'partial_export':
            if decision['reply'] == 'allow':
                return {'decision_id': None, 'wait_reason': 'retry_operation'}
            call = state['calls'][state['cursor']]
            result = ToolResult(status='partial', summary='用户暂不导出，已有结果和缺失页清单保留', data={'decision_id': decision['id']}).model_dump(mode='json')
            services.agent.finish_call(state['project_id'], run['id'], state['generation'], state['step'], call, result)
            return {'results': state['results'] + [{'call_id': call['call_id'], 'result': result}], 'cursor': state['cursor'] + 1,
                    'decision_id': None, 'wait_reason': None}
        if state["wait_reason"] == "tool":
            call = state["calls"][state["cursor"]]
            result = ToolResult(status="success", summary="已收到用户回复", data={"option_id": decision["reply"], "decision_id": decision["id"]}).model_dump(mode="json")
            services.agent.finish_call(state["project_id"], run["id"], state["generation"], state["step"], call, result)
            return {"results": state["results"] + [{"call_id": call["call_id"], "result": result}], "cursor": state["cursor"] + 1,
                    "decision_id": None, "wait_reason": None}
        if decision["reply"] == "stop":
            services.project_status(state, "cancelled", "user-stopped")
            return {"wait_reason": "stopped", "decision_id": None}
        if state['wait_reason'] == 'child_job_budget':
            services.jobs.resume_budget(state)
            return {'decision_id': None, 'wait_reason': 'jobs'}
        if state['wait_reason'] == 'no_job_progress':
            services.jobs.acknowledge_wait(state)
            return {'decision_id': None, 'wait_reason': 'jobs'}
        if state['wait_reason'] in {'summary_response_unknown', 'summary_budget'}:
            return {'context_summary_attempt': state.get('context_summary_attempt', 0) +
                    (1 if state['wait_reason'] == 'summary_response_unknown' else 0),
                    'decision_id': None, 'wait_reason': 'retry_summary'}
        return {"step": state["step"] + (1 if state["wait_reason"] == "response_unknown" else 0), "decision_id": None,
                "wait_reason": "retry_operation" if state["wait_reason"] == "operation_budget" else "retry_batch" if state["wait_reason"] == "tool_budget" else "retry_model"}

    def after_user(state):
        if state.get("wait_reason") == "stopped":
            return END
        if state.get("wait_reason") == "retry_model":
            return "receive_inbox"
        if state.get('wait_reason') == 'retry_summary':
            # Preserve the same checkpointed summary source even if new inbox
            # text arrived while its outbound response was unknown.
            return 'prepare_context'
        if state.get("wait_reason") == "retry_batch":
            return "validate_batch"
        if state.get("wait_reason") == "retry_operation":
            return "execute_next"
        return after_execute(state)

    async def collect_results(state):
        run = services.current(state)
        validate_result_pairing([ProviderCall.model_validate(call) for call in state["calls"]], state["results"])
        messages = [{"id": f"{state['run_id']}:{state['step']}:{r['call_id']}:result", "role": "tool", **r} for r in state["results"]]
        for call in state['calls']:
            text = state.get('error_stop') or repeated_error(run, call['tool'])
            if text:
                key = f"{run['id']}:{state['step']}:error-limit"
                services.agent.append_event(run['project_id'], run['id'], state['generation'], key, 'message', {'role': 'assistant', 'content': text})
                messages.append({'id': key, 'role': 'assistant', 'content': text, 'calls': []})
                return {'messages': messages, 'step': state['step'] + 1, 'wait_reason': 'error_limit', 'decision_id': None, 'final_text': text}
        return {"messages": messages, "step": state["step"] + 1, "wait_reason": None, "decision_id": None}

    async def finalize(state):
        run = services.agent.run(state["project_id"], state["run_id"])
        if run["status"] == "completed" and run["generation"] == state["generation"]:
            return {"outcome": run["outcome"]}
        run = services.current(state)
        if has_inbox(state):
            return {'step': state['step'] + 1, 'wait_reason': 'inbox'}
        if services.jobs:
            for row in services.agent.business.rows("""SELECT DISTINCT o.id FROM agent_operations o JOIN agent_job_links j ON j.operation_id=o.id WHERE o.run_id=?""", (run['id'],)):
                services.jobs.operation_result(run['project_id'], run['id'], row['id'])
            run = services.current(state)
        pending = services.agent.business.rows("SELECT state FROM agent_calls WHERE run_id=? AND state!='finished'", (run["id"],))
        if pending:
            raise PolicyDenied("business_failed", "尚有未结算的工具调用，不能标记完成")
        # Writes later supply authoritative page coverage. Read-only answers have
        # outcome=answered and never claim successful processing of a document.
        recorded = services.agent.business.rows("SELECT tool,result_ref FROM agent_calls WHERE run_id=?", (run["id"],))
        outcome = "partial" if state.get('wait_reason') == 'error_limit' or any(json.loads(r["result_ref"])["status"] in {"partial", "error"} and not json.loads(r['result_ref']).get('data', {}).get('superseded_by_inbox') for r in recorded if r["result_ref"]) else "answered"
        if run["coverage"].get("requested_count"):
            ancillary_failure = any(r['tool'] in {'export_results', 'inspect_table', 'request_visual_review'} and r['result_ref'] and json.loads(r['result_ref'])['status'] in {'partial', 'error'} for r in recorded)
            outcome = "success" if not ancillary_failure and run["coverage"].get("completed_count") == run["coverage"]["requested_count"] else "partial"
        if state.get('wait_reason') == 'error_limit':
            outcome = 'partial'
        artifact_states = {row['state']: row['count'] for row in services.agent.business.rows(
            'SELECT state,COUNT(*) count FROM agent_artifacts WHERE run_id=? GROUP BY state', (run['id'],))}
        unavailable_artifacts = sum(count for status, count in artifact_states.items() if status != 'ready')
        if unavailable_artifacts:
            outcome = 'partial'
        final_text = state.get('final_text', '')
        if outcome == 'partial':
            facts = ['本轮未完整完成。']
            coverage = run['coverage']
            if coverage.get('requested_count'):
                facts.append(f"本轮范围 {coverage['requested_count']} 页：完整覆盖 {coverage.get('completed_count', 0)} 页，"
                             f"失败或取消 {coverage.get('failed_count', 0)} 页，等待处理 {coverage.get('pending_count', 0)} 页，"
                             f"未覆盖 {coverage.get('uncovered_count', 0)} 页。")
            failures = [json.loads(row['result_ref']) for row in recorded if row['result_ref'] and
                        json.loads(row['result_ref'])['status'] in {'partial', 'error'} and
                        not json.loads(row['result_ref']).get('data', {}).get('superseded_by_inbox')]
            reasons = list(dict.fromkeys(result['summary'][:350] for result in failures if result.get('summary')))
            if reasons:
                facts.append('工具过程记录：' + '；'.join(reasons[:3]) + ('；更多情况见工具记录。' if len(reasons) > 3 else '。'))
            if state.get('wait_reason') == 'error_limit' and final_text:
                facts.append(final_text)
            if artifact_states:
                facts.append(f"导出产物：已保存 {artifact_states.get('ready', 0)} 份，失败或不可用 {unavailable_artifacts} 份。")
            final_text = '\n'.join(facts)
        try:
            services.project_status(state, "completed", "completed", outcome=outcome, final_message=final_text)
        except InboxPending:
            return {'step': state['step'] + 1, 'wait_reason': 'inbox'}
        return {"outcome": outcome, 'final_text': final_text}

    builder = StateGraph(AgentState)
    for name, node in (("receive_inbox", receive_inbox), ("prepare_context", prepare_context), ("model", model), ("validate_batch", validate_batch), ("execute_next", execute_next),
                       ("await_jobs", await_jobs), ("await_user", await_user), ("collect_results", collect_results), ("finalize", finalize)):
        builder.add_node(name, node)
    builder.add_edge(START, "receive_inbox")
    builder.add_edge('receive_inbox', 'prepare_context' if services.prepare_context else 'model')
    builder.add_conditional_edges('prepare_context', lambda state: 'await_user' if state.get('decision_id') else 'model')
    builder.add_conditional_edges("model", after_model)
    builder.add_conditional_edges("validate_batch", lambda s: "await_user" if s.get("decision_id") else "execute_next")
    builder.add_conditional_edges("execute_next", after_execute)
    builder.add_conditional_edges("await_jobs", after_execute)
    builder.add_conditional_edges("await_user", after_user)
    builder.add_conditional_edges("collect_results", lambda state: 'finalize' if state.get('wait_reason') == 'error_limit' else 'receive_inbox')
    builder.add_conditional_edges("finalize", lambda state: 'receive_inbox' if state.get('wait_reason') == 'inbox' else END)
    return builder.compile(checkpointer=saver)

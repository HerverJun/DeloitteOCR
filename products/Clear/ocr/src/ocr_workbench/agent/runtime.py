"""Thin lifecycle adapter: leases, fencing and invoke/resume, no step scheduler."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from .checkpoints import Checkpoints, THREAD_CHECKPOINT_LIMIT, THREAD_PAYLOAD_LIMIT
from .connection import ControllerConnection
from .context import compact_messages, SUMMARY_SYSTEM, summary_sources, summary_message
from .graph import GraphServices, build_graph, tool_error
from .policy import AgentPolicy, PolicyDenied
from .providers import ProviderFault, logged_request, payload_for
from .registry import ToolRegistry
from .state import initial_state, merge_messages
from .store import AgentStore, GRAPH_VERSION, canonical
from .tools import register_read_tools, register_processing_tools
from .jobs import JobBridge
from .artifacts import Artifacts
from ocr_workbench.store import Conflict, now
from langgraph.types import Command
from langgraph.errors import GraphRecursionError


class AgentRuntime:
    def __init__(self, services, *, policy, connection=None, model_override=None):
        self.services = services
        self.agent = AgentStore(services.store)
        self.policy_config = policy
        self.connection = connection or ControllerConnection(services.store)
        self.policy = AgentPolicy(self.agent, review_only=bool(getattr(services.documents, "review_only", False)))
        from .inbox import Inbox
        self.inbox = Inbox(self.agent, self.policy, services)
        self.registry = ToolRegistry(self.policy)
        self.views = register_read_tools(self.registry, services, self.capabilities)
        self.jobs = JobBridge(self.agent, services)
        self.operations = register_processing_tools(self.registry, services, self.jobs)
        from .review_tools import register_review_tools
        register_review_tools(self.registry, services, self.operations, self.views, self.jobs)
        from .retry import register_retry_tool
        register_retry_tool(self.registry, services, self.operations, self.jobs)
        self.artifacts = Artifacts(self.agent, services, self.operations)
        async def export_tool(args, context):
            return await asyncio.to_thread(self.artifacts.export, args, context)
        self.registry.register("export_results", export_tool)
        self.checkpoints = Checkpoints(services.store)
        self.http_slots = asyncio.Semaphore(policy.get("concurrency", {}).get("model_http_global", 2))
        self.tasks = {}
        self.owner = uuid.uuid4().hex
        self.agent.executor = self.owner
        self.closed = True
        self.model_override = model_override
        self.observer = None
        self.observer_error = None
        self.control_lock = asyncio.Lock()
        self.last_artifact_cleanup = 0
        self.maintenance_projects = set()

    def capabilities(self):
        return {"tools": sorted(self.registry.handlers), "review_only": self.policy.review_only,
                "controller_images": False, "automatic_adoption": False}

    async def open(self):
        self.agent.interrupt_on_startup()
        await self.checkpoints.open()
        self.graph = build_graph(GraphServices(self.agent, self.registry, self.model_override or self._model,
            jobs=self.jobs, inbox=self.inbox, prepare_context=None if self.model_override else self._prepare_context), self.checkpoints.saver)
        self.closed = False
        self.observer = asyncio.create_task(self._observe_jobs(), name="ocr-agent-job-observer")
        return self

    async def _observe_jobs(self):
        while not self.closed:
            try:
                await self._observe_once()
                if time.monotonic() - self.last_artifact_cleanup > 60:
                    await asyncio.to_thread(self.artifacts.cleanup)
                    self.last_artifact_cleanup = time.monotonic()
                self.observer_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                # SQLite/IO can be temporarily unavailable. Keep the observer alive;
                # the next successful tick fences expired leases before doing work.
                self.observer_error = '后台核对暂时不可用，正在重试；租约过期的运行需显式继续。'
                logging.getLogger(__name__).exception('Agent observer tick failed')
            await asyncio.sleep(1)

    async def _observe_once(self):
        with self.services.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            expired = db.execute("SELECT * FROM agent_runs WHERE owner=? AND lease_expires<=? AND status IN ('queued','running','waiting_jobs','waiting_user')", (self.owner, time.time())).fetchall()
            for row in expired:
                generation = row['generation'] + 1
                db.execute("UPDATE agent_runs SET status='interrupted',generation=?,fencing_token=fencing_token+1,owner=NULL,lease_expires=NULL,updated=? WHERE id=?", (generation, now(), row['id']))
                db.execute("UPDATE agent_model_requests SET state='unknown' WHERE run_id=? AND state='sent'", (row['id'],))
                self.agent._event(db, row['session_id'], row['id'], generation, f"{row['id']}:lease-expired:{generation}", 'run_state', {'status': 'interrupted', 'reason': 'executor_lease_expired'})
            # Waiting runs retain ownership; checkpoints release their lock while idle.
            db.execute("UPDATE agent_runs SET lease_expires=? WHERE owner=? AND lease_expires>? AND status IN ('queued','running','waiting_jobs','waiting_user')",
                       (time.time() + 300, self.owner, time.time()))
        await self._reconcile_resume_intents()
        rows = self.services.store.rows("""SELECT r.id,s.project_id FROM agent_runs r JOIN agent_sessions s ON s.id=r.session_id
            WHERE r.owner=? AND (r.status='waiting_jobs' OR (r.status='waiting_user' AND (EXISTS
                (SELECT 1 FROM agent_inbox i WHERE i.run_id=r.id AND i.state='pending') OR EXISTS
                (SELECT 1 FROM agent_decisions d WHERE d.run_id=r.id AND d.generation=r.generation AND d.status='resolved'))))""", (self.owner,))
        for row in rows:
            if row['project_id'] in self.maintenance_projects:
                continue
            task = self.tasks.get(row["id"])
            if task and not task.done():
                continue
            run = self.agent.run(row["project_id"], row["id"])
            config = {"configurable": {"thread_id": run["graph_thread_id"]}}
            try:
                async with self.checkpoints.activity(run["graph_thread_id"]):
                    if run['project_id'] in self.maintenance_projects:
                        continue
                    snapshot = await self.graph.aget_state(config)
                    if not snapshot.values or snapshot.values.get("generation") != run["generation"]:
                        raise Conflict("等待检查点缺失或失效")
                    waiting = (snapshot.interrupts and snapshot.interrupts[0].value.get("kind") == "jobs") or (
                        snapshot.values.get("wait_reason") == "jobs" and "await_jobs" in snapshot.next)
                    if waiting and (self.jobs.result_for_call(snapshot.values) is not None or self.inbox.pending(run['id']) or self.jobs.no_progress(snapshot.values)):
                        self.schedule(run, resume=True)
                    elif run['status'] == 'waiting_user' and snapshot.values.get('wait_reason') == 'tool' and self.inbox.pending(run['id']):
                        self.schedule(run, resume=True)
                    elif run['status'] == 'waiting_user' and snapshot.values.get('decision_id'):
                        resolved = self.services.store.rows("SELECT 1 FROM agent_decisions WHERE id=? AND run_id=? AND generation=? AND status='resolved'",
                            (snapshot.values['decision_id'], run['id'], run['generation']))
                        if resolved:
                            self.schedule(run, resume=True)
            except Exception as error:
                if run['project_id'] in self.maintenance_projects:
                    continue
                current = self.agent.run(run["project_id"], run["id"])
                if current["generation"] == run["generation"] and current["status"] in {'waiting_jobs', 'waiting_user'}:
                    self.agent.append_event(run["project_id"], run["id"], run["generation"], f"{run['id']}:{run['generation']}:observer-error", "error",
                                            {"code": getattr(error, "code", "checkpoint_reconcile_failed"), "message": "后台任务核对未完成，请显式继续；已有任务记录已保留。"})
                    self.agent.transition(run["project_id"], run["id"], run["generation"], "interrupted", event_key=f"{run['id']}:{run['generation']}:reconcile-interrupted")

    async def _reconcile_resume_intents(self):
        # A wakeup receipt is separate from business effects and from saver writes.
        # Never replay Command merely because its receipt was left claimed.
        rows = self.services.store.rows("""SELECT i.*,r.status,r.generation current_generation,s.graph_thread_id,s.project_id
            FROM agent_resume_intents i JOIN agent_runs r ON r.id=i.run_id JOIN agent_sessions s ON s.id=r.session_id
            WHERE i.state IN ('pending','claimed') ORDER BY i.created,i.id LIMIT 100""")
        for row in rows:
            if row['project_id'] in self.maintenance_projects:
                continue
            task = self.tasks.get(row['run_id'])
            if task and not task.done():
                continue
            state = None
            if row['generation'] != row['current_generation'] or row['status'] in {'cancelled', 'failed'}:
                state = 'obsolete'
            else:
                config = {'configurable': {'thread_id': row['graph_thread_id']}}
                async with self.checkpoints.activity(row['graph_thread_id']):
                    if row['project_id'] in self.maintenance_projects:
                        continue
                    snapshot = await self.graph.aget_state(config)
                    if not snapshot.values or snapshot.values.get('generation') != row['generation']:
                        continue
                    original = await self.graph.aget_state({'configurable': {
                        'thread_id': row['graph_thread_id'], 'checkpoint_id': row['checkpoint_id']}})
                    if not original.values:
                        continue
                    # update_state can remove an interrupt without consuming it.
                    # Require persisted logical progress, not just a new checkpoint ID.
                    before = (original.values.get('step', 0), original.values.get('cursor', 0))
                    after = (snapshot.values.get('step', 0), snapshot.values.get('cursor', 0))
                    still_waiting = any(i.id == row['interrupt_id'] for i in snapshot.interrupts)
                    progressed = after > before or snapshot.values.get('decision_id') != original.values.get('decision_id')
                    finished = not snapshot.next and snapshot.values.get('outcome') in {'answered', 'partial', 'success'}
                    if not still_waiting and (progressed or finished):
                        state = 'applied'
            if state:
                with self.services.store.transaction() as db:
                    db.execute("UPDATE agent_resume_intents SET state=? WHERE id=? AND state IN ('pending','claimed')", (state, row['id']))

    def _model_connection(self, state, run):
        config, key = self.connection.resolve(run["config"]["revision"])
        if config != run["config"]:
            raise ProviderFault("config_changed", "主控快照已变化")
        documents, pages = set(run["context"].get("document_ids", [])), set(run["context"].get("page_ids", []))
        for message in state["messages"]:
            documents.update(message.get('context_scope', {}).get('document_ids', []))
            pages.update(message.get('context_scope', {}).get('page_ids', []))
            if message["role"] == "tool":
                for ref in message["result"].get("evidence_refs", []):
                    documents.add(ref["document_id"])
                    pages.add(ref["page_id"])
                for doc in message["result"].get("data", {}).get("documents", []):
                    documents.add(doc["id"])
        self.policy.authorize_outbound(run, role="controller", endpoint=config["base_url"], config_revision=config["revision"],
                                       document_ids=documents, page_ids=pages, data_kinds=["text", "metadata"])
        return config, key

    async def _prepare_context(self, state, run):
        """Checkpoint all context replacements before the next ordinary POST.

        Negative request steps belong exclusively to a run's one summary and
        explicit unknown-response retries. They use the same durable receipts
        and request/token budgets as every other controller request.
        """
        config, key = self._model_connection(state, run)
        prepared = {'run_id': run['id'], 'step': state['step']}
        if self.services.store.rows('SELECT 1 FROM agent_model_requests WHERE run_id=? AND step=?',
                                    (run['id'], state['step'])):
            return {'context_prepared': prepared, 'decision_id': None, 'wait_reason': None}
        recovery = self.views.recovery_message(run)
        history = [m for m in state['messages'] if not m.get('server_context_summary')]
        max_bytes = (config['context_cap'] - config['max_output_tokens']) * 2
        measure = lambda values: len(json.dumps(payload_for(config, values), ensure_ascii=False).encode('utf-8'))
        messages = compact_messages(history + [recovery], max_bytes, measure=measure, enforce_limit=False)
        attempt = state.get('context_summary_attempt', 0)
        summary_step = -1 - attempt
        old_summary = self.services.store.rows('SELECT 1 FROM agent_model_requests WHERE run_id=? AND step=?',
                                              (run['id'], summary_step))
        sources = summary_sources(history)
        done = state.get('context_summary_done', False)
        prior = state.get('context_summary') or {}
        semantic = prior.get('semantic') if prior.get('run_id') == run['id'] else None
        summary_error = None
        deletions = []
        if not done and sources and (old_summary or measure(messages) > max_bytes * 7 // 10):
            summary_config = {**config, 'max_output_tokens': min(config['max_output_tokens'], 1024)}
            summary_limit = (config['context_cap'] - summary_config['max_output_tokens']) * 2
            # Use only checkpointed sources here. Live jobs/grants/versions stay
            # in the separate business projection and cannot change retry input.
            source_data = compact_messages(sources, summary_limit // 2, enforce_limit=False)
            # This is data for a new summary request, not provider continuation.
            # Avoid duplicating text in native blocks or sending old opaque
            # reasoning. Retained live rounds keep those blocks byte-for-byte.
            source_data = [{name: value for name, value in message.items()
                            if name in {'id', 'role', 'content', 'calls', 'call_id', 'result', 'context_compaction'}}
                           for message in source_data]
            prompt = [{'id': run['id'] + ':summary-input', 'role': 'user',
                       'content': canonical({'untrusted_history': source_data})}]
            summary_size = len(json.dumps(payload_for(summary_config, prompt, schemas={}, system=SUMMARY_SYSTEM),
                                          ensure_ascii=False).encode('utf-8'))
            if summary_size <= summary_limit:
                try:
                    async with self.http_slots:
                        response = await logged_request(self.agent, run, state['generation'], summary_step,
                            summary_config, key, prompt, schemas={}, system=SUMMARY_SYSTEM, purpose='summary',
                            check_current=lambda: self.connection.assert_current(config['revision']))
                except ProviderFault as error:
                    if error.code == 'response_unknown':
                        raise ProviderFault('summary_response_unknown', '历史摘要请求结果未知；不会自动重发。明确继续会计费发起一次新的摘要请求。') from None
                    # A known unusable summary falls back to traceable tool
                    # reduction, then pauses if the original history cannot fit.
                    summary_error = {'code': error.code, 'message': str(error)}
                    done = True
                else:
                    replacement, semantic = summary_message(run, sources, response)
                    deletions = [{'id': m['id'], '_delete': True} for m in sources[1:]]
                    history = merge_messages(history, [replacement] + deletions)
                    messages = compact_messages(history + [recovery], max_bytes, measure=measure, enforce_limit=False)
                    done = True
        metadata = {'version': 2, 'run_id': run['id'], 'step': state['step'], 'wire_bytes': measure(messages),
                    'threshold': 0.7, 'estimator': 'utf8_bytes/2', 'recovery_message_id': recovery['id'],
                    'compacted_messages': [m['context_compaction'] for m in messages if m.get('context_compaction')],
                    'semantic': semantic, 'summary_error': summary_error}
        updates = {'messages': messages + deletions, 'context_summary': metadata,
                   'context_summary_done': done, 'context_prepared': prepared, 'decision_id': None, 'wait_reason': None}
        if measure(messages) > max_bytes:
            updates['_context_fault'] = '已保留全部用户约束、完整工具配对和证据，并尝试可用的历史压缩，但仍超过主控上下文容量。请停止本轮后缩小目标或新建会话；已有任务、产物和原始记录保留。'
        return updates

    async def _model(self, state, run):
        config, key = self._model_connection(state, run)
        # The business projection below can legitimately change while jobs run.
        # Replay the immutable received/rejected/unknown request before rebuilding
        # a prompt; never reinterpret a new projection as a reason to POST again.
        old = self.services.store.rows('SELECT state,normalized_response FROM agent_model_requests WHERE run_id=? AND step=?',
                                       (run['id'], state['step']))
        if old:
            if old[0]['state'] == 'received':
                return json.loads(old[0]['normalized_response'])
            if old[0]['state'] == 'rejected':
                error = json.loads(old[0]['normalized_response'])['error']
                raise ProviderFault(error['code'], error['message'])
            raise ProviderFault('response_unknown', '已有发送记录但没有可靠回包；禁止自动重发')
        if state.get('context_prepared') != {'run_id': run['id'], 'step': state['step']}:
            raise ProviderFault('context_limit', '该历史图尚未准备上下文，请从正式图准备节点继续；未发送主控请求。')
        async with self.http_slots:
            reply = await logged_request(self.agent, run, state["generation"], state["step"], config, key, state['messages'],
                                         check_current=lambda: self.connection.assert_current(config["revision"]))
        return reply

    def _lease(self, run):
        with self.services.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.agent._run(db, run["project_id"], run["id"])
            self.agent.require_generation(current, run["generation"])
            if current["owner"] and current["owner"] != self.owner and (current["lease_expires"] or 0) > time.time():
                raise Conflict("运行正在由另一个执行者处理")
            db.execute("UPDATE agent_runs SET owner=?,fencing_token=fencing_token+1,lease_expires=? WHERE id=?", (self.owner, time.time() + 300, run["id"]))

    def schedule(self, run, *, resume=False):
        if self.closed:
            raise ValueError("助手未启动")
        if run['project_id'] in self.maintenance_projects:
            raise Conflict('项目正在清理，不能启动新的助手运行')
        old = self.tasks.get(run["id"])
        if old and not old.done():
            return
        self._lease(run)
        task = asyncio.create_task(self._invoke(run, resume=resume), name="ocr-agent-" + run["id"])
        self.tasks[run["id"]] = task
        task.add_done_callback(lambda done: self.tasks.pop(run["id"], None) if self.tasks.get(run["id"]) is done else None)

    def _continuation(self, run, *, before_seq=None):
        """Bound a new graph segment independently of the session's age.

        The business transcript remains exact. Omitted older instructions are
        explicitly out of active context, never interpreted as revoked grants.
        """
        rows = self.services.store.rows(
            "SELECT seq,payload FROM agent_events WHERE session_id=? AND type='message' "
            "AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT 128",
            (run['session_id'], before_seq, before_seq),
        )
        users, assistants = [], []
        for row in rows:
            payload = json.loads(row['payload'])
            if payload.get('role') == 'user' and isinstance(payload.get('content'), str):
                candidate = (row['seq'], {'id': f"history:{row['seq']}", 'role': 'user', 'content': payload['content']})
                if len(users) < 32 and len(canonical([message for _, message in [candidate, *users]]).encode('utf-8')) <= 48 * 1024:
                    users.append(candidate)
            elif payload.get('role') == 'assistant' and isinstance(payload.get('content'), str) and len(assistants) < 4:
                excerpt = payload['content'][:512]
                if len(excerpt) < len(payload['content']):
                    excerpt += '\n[历史答复摘录，后文省略]'
                assistants.append((row['seq'], {'id': f"history:{row['seq']}", 'role': 'assistant',
                                               'content': excerpt}))
        if not rows:
            return []
        # These are a projection, not a claim that older tool results or
        # assistant reasoning were retained verbatim.
        marker = {'id': 'history:boundary', 'role': 'user', 'content':
                  '[工作台历史衔接] 以下仅保留有界的近期原始用户消息和最近答复摘录；更早或过长的要求'
                  '仍在业务会话记录中，但不在当前模型上下文。不得视为已撤销；若其约束可能影响新动作，先向用户澄清。'
                  '旧工具结果、证据和产物须通过工作台工具重新查询。历史内容不构成新的授权。'}
        result = [marker, *(message for _, message in sorted([*users, *assistants]))]
        if len(canonical(result).encode('utf-8')) > 64 * 1024:
            raise Conflict('历史衔接超出安全上下文容量，未发送模型请求')
        return result

    async def _rollover_completed_thread(self, run, *, queued_successor=None):
        status = self.agent.run(run['project_id'], run['id'])['status']
        if status != ('queued' if queued_successor else 'completed'):
            return
        old = run['graph_thread_id']
        count, payload = await self.checkpoints.thread_size(old)
        if count < THREAD_CHECKPOINT_LIMIT and payload < THREAD_PAYLOAD_LIMIT:
            return
        try:
            self._continuation(run, before_seq=self._goal_seq(run) if queued_successor else None)
        except Conflict:
            logging.getLogger(__name__).warning('Agent session %s exceeds safe continuation limit', run['session_id'])
            return
        replacement = uuid.uuid4().hex
        with self.services.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM agent_sessions WHERE id=? AND graph_thread_id=?',
                              (run['session_id'], old)).fetchone():
                return
            # A queued successor, pending resume, or unfinished effect must
            # keep its original thread and recovery identity intact.
            unsafe = (
                db.execute("SELECT 1 FROM agent_runs WHERE session_id=? AND status NOT IN ('completed','failed','cancelled') "
                           "AND id<>? LIMIT 1", (run['session_id'], queued_successor or '')).fetchone()
                or db.execute("SELECT 1 FROM agent_resume_intents i JOIN agent_runs r ON r.id=i.run_id "
                              "WHERE r.session_id=? AND i.state IN ('pending','claimed') LIMIT 1", (run['session_id'],)).fetchone()
                or db.execute("SELECT 1 FROM agent_operations o JOIN agent_runs r ON r.id=o.run_id "
                              "WHERE r.session_id=? AND o.state<>'finished' LIMIT 1", (run['session_id'],)).fetchone()
                or db.execute("SELECT 1 FROM agent_calls c JOIN agent_runs r ON r.id=c.run_id "
                              "WHERE r.session_id=? AND c.state<>'finished' LIMIT 1", (run['session_id'],)).fetchone()
                or db.execute("SELECT 1 FROM agent_model_requests m JOIN agent_runs r ON r.id=m.run_id "
                              "WHERE r.session_id=? AND m.state IN ('sent','unknown') LIMIT 1", (run['session_id'],)).fetchone()
                or db.execute("SELECT 1 FROM agent_decisions d JOIN agent_runs r ON r.id=d.run_id "
                              "WHERE r.session_id=? AND d.status='pending' LIMIT 1", (run['session_id'],)).fetchone()
                or db.execute("SELECT 1 FROM agent_inbox i JOIN agent_runs r ON r.id=i.run_id "
                              "WHERE r.session_id=? AND i.state='pending' LIMIT 1", (run['session_id'],)).fetchone()
            )
            if unsafe:
                return
            if queued_successor and (
                db.execute("SELECT 1 FROM agent_runs WHERE id=? AND status<>'queued' LIMIT 1", (queued_successor,)).fetchone()
                or db.execute('SELECT 1 FROM agent_model_requests WHERE run_id=? LIMIT 1', (queued_successor,)).fetchone()
                or db.execute('SELECT 1 FROM agent_operations WHERE run_id=? LIMIT 1', (queued_successor,)).fetchone()
            ):
                return
            db.execute('UPDATE agent_sessions SET graph_thread_id=?,updated=? WHERE id=? AND graph_thread_id=?',
                       (replacement, now(), run['session_id'], old))
            db.execute('INSERT OR IGNORE INTO agent_checkpoint_cleanup VALUES(?,?,?)',
                       (old, run['project_id'], now()))
        try:
            await self.checkpoints.drain_retired_thread(old, run['project_id'])
        except Exception:
            logging.getLogger(__name__).exception('Agent retired checkpoint cleanup pending for %s', old)
        else:
            logging.getLogger(__name__).info(
                'Agent checkpoint rollover session=%s rows=%d payload_bytes=%d old_thread=%s',
                run['session_id'], count, payload, old)

    def _goal_seq(self, run):
        return self.services.store.rows('SELECT seq FROM agent_events WHERE session_id=? AND event_key=?',
                                        (run['session_id'], run['id'] + ':goal'))[0]['seq']

    async def _invoke(self, run, *, resume=False):
        config = {"configurable": {"thread_id": run["graph_thread_id"]}, "recursion_limit": 500}
        try:
            async with self.checkpoints.activity(run["graph_thread_id"]):
                current_thread = self.agent.run(run['project_id'], run['id'])
                if current_thread['graph_thread_id'] != run['graph_thread_id']:
                    return await self._invoke(current_thread, resume=resume)
                if resume:
                    snapshot = await self.graph.aget_state(config)
                    if not snapshot.values or snapshot.values.get("run_id") != run["id"] or snapshot.values.get("generation") != run["generation"]:
                        raise Conflict("检查点缺失或运行代次不匹配；请核对已有任务")
                    current = self.agent.run(run['project_id'], run['id'])
                    if current['status'] == 'queued':
                        self.agent.transition(run['project_id'], run['id'], run['generation'], 'running',
                                              event_key=f"{run['id']}:{run['generation']}:invoke-resumed")
                    if not snapshot.interrupts:
                        # A duplicate notification after graph completion is an acknowledgment.
                        if not snapshot.next:
                            return
                        await self.graph.ainvoke(None, config, durability="sync")
                        return
                    item = snapshot.interrupts[0]
                    intent_id = f"{run['id']}:{run['generation']}:{item.id}"
                    with self.services.store.transaction() as db:
                        db.execute("INSERT OR IGNORE INTO agent_resume_intents VALUES(?,?,?,?,?,?,'pending',?)",
                                   (intent_id, run["id"], run["generation"], snapshot.config["configurable"]["checkpoint_id"], item.id, item.value["kind"], now()))
                        db.execute("UPDATE agent_resume_intents SET state='claimed' WHERE id=?", (intent_id,))
                    await self.graph.ainvoke(Command(resume={item.id: {"notification": True}}), config, durability="sync")
                    with self.services.store.transaction() as db:
                        db.execute("UPDATE agent_resume_intents SET state='applied' WHERE id=?", (intent_id,))
                else:
                    state = initial_state(run, run["goal"])
                    snapshot = await self.graph.aget_state(config)
                    if snapshot.values:
                        count, payload = await self.checkpoints.thread_size(run['graph_thread_id'])
                        if count >= THREAD_CHECKPOINT_LIMIT or payload >= THREAD_PAYLOAD_LIMIT:
                            await self._rollover_completed_thread(run, queued_successor=run['id'])
                            current_thread = self.agent.run(run['project_id'], run['id'])
                            if current_thread['graph_thread_id'] != run['graph_thread_id']:
                                return await self._invoke(current_thread)
                            self._continuation(run, before_seq=self._goal_seq(run))
                            raise Conflict('旧检查点有待恢复或未结副作用，暂不能安全换段；请核对旧任务后新建会话')
                    if not snapshot.values:
                        history = self._continuation(run, before_seq=self._goal_seq(run))
                        state['messages'] = [*history, *state['messages']]
                    await self.graph.ainvoke(state, config, durability="sync")
                await self._rollover_completed_thread(run)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isinstance(error, (PolicyDenied, ProviderFault, Conflict)):
                logging.getLogger(__name__).exception('Agent invocation failed for run %s', run['id'])
            current = self.agent.run(run["project_id"], run["id"])
            if current["generation"] == run["generation"] and current["status"] not in {"completed", "cancelled", "failed"}:
                code = getattr(error, "code", "internal_error")
                message = str(error) if isinstance(error, (PolicyDenied, ProviderFault, Conflict)) else "助手执行未完成；已保留任务记录，请查看诊断"
                self.agent.append_event(run["project_id"], run["id"], run["generation"], f"{run['id']}:{run['generation']}:error", "error", {"code": code, "message": message[:1000]})
                if current["status"] in {"waiting_user", "queued", "interrupted"}:
                    with self.services.store.transaction() as db:
                        db.execute("UPDATE agent_runs SET status='failed',updated=? WHERE id=?", (now(), run["id"]))
                        self.agent.discard_inbox(db, run['id'])
                        self.agent._event(db, run["session_id"], run["id"], run["generation"], f"{run['id']}:{run['generation']}:failed", "run_state", {"status": "failed"})
                else:
                    self.agent.transition(run["project_id"], run["id"], run["generation"], "failed", event_key=f"{run['id']}:{run['generation']}:failed")
        finally:
            with self.services.store.transaction() as db:
                db.execute("UPDATE agent_runs SET owner=NULL,lease_expires=NULL WHERE id=? AND generation=? AND owner=? AND status IN ('completed','failed','cancelled','interrupted')",
                           (run["id"], run["generation"], self.owner))

    async def cancel(self, project_id, run_id, generation, mode, *, request_id=None):
        async with self.control_lock:
            return await self._cancel(project_id, run_id, generation, mode, request_id=request_id)

    async def delete_project(self, project_id, confirmation):
        """Fence and drain only this project's graph before existing file cleanup."""
        async with self.control_lock:
            self.maintenance_projects.add(project_id)
            try:
                runs = self.services.maintenance.stop_agent_runs(project_id, confirmation)
                tasks = [self.tasks[row['id']] for row in runs if row['id'] in self.tasks]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                # An observer may already have read this project before the
                # barrier. Drain its per-thread saver scope without stopping the
                # global observer or waiting on another project's model HTTP.
                for row in runs:
                    async with self.checkpoints.activity(row['graph_thread_id']):
                        pass
                result = await asyncio.to_thread(self.services.maintenance.delete, project_id, confirmation)
                try:
                    await self.checkpoints.cleanup_project_threads(project_id)
                except Exception:
                    # The cascade's durable tombstone survives for restart retry.
                    logging.getLogger(__name__).exception('Agent checkpoint cleanup pending for project %s', project_id)
                result['checkpoint_cleanup_pending'] = bool(self.services.store.rows(
                    'SELECT 1 FROM agent_checkpoint_cleanup WHERE project_id=? LIMIT 1', (project_id,)))
                return result
            finally:
                self.maintenance_projects.discard(project_id)

    async def _cancel(self, project_id, run_id, generation, mode, *, request_id=None):
        if mode not in {"stop_agent", "cancel_owned_jobs"}:
            raise ValueError("停止模式无效")
        with self.services.store.transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            run = self.agent._run(db, project_id, run_id)
            old = self.agent.mutation(db, run['session_id'], request_id, 'cancel', {'generation': generation, 'mode': mode}, run_id) if request_id else None
            if old and old['state'] == 'applied':
                return json.loads(old['response'])
            if run['status'] in {'completed', 'failed'}:
                raise Conflict('运行已经结束，不能再改变结束状态；请在工作台核对关联任务')
            if run['status'] == 'cancelled' and run['generation'] != generation + 1:
                raise Conflict('运行已经取消，不能再次递增代次')
            if run['status'] != 'cancelled' or run['generation'] != generation + 1:
                self.agent.require_generation(run, generation)
                db.execute("UPDATE agent_runs SET status='cancelled',generation=generation+1,owner=NULL,lease_expires=NULL,updated=? WHERE id=?", (now(), run_id))
                self.agent.discard_inbox(db, run_id)
                # A cancelled ask_user has no business operation to reconcile.
                # Settle its validated call and retire its question atomically
                # with the generation fence; other unfinished effects remain
                # visible to checkpoint rollover's conservative safety gate.
                db.execute("UPDATE agent_decisions SET status='obsolete' WHERE run_id=? AND status='pending'", (run_id,))
                cancelled = tool_error('cancelled', '助手已停止，尚未回复的澄清调用已取消')
                cancelled['error']['required_action'] = 'none'
                for call in db.execute("SELECT * FROM agent_calls WHERE run_id=? AND tool='ask_user' AND state='validated' "
                                       "AND operation_id IS NULL AND result_ref IS NULL", (run_id,)).fetchall():
                    db.execute("UPDATE agent_calls SET state='finished',result_ref=? WHERE id=?", (canonical(cancelled), call['id']))
                    self.agent._event(db, run['session_id'], run_id, generation + 1, call['id'] + ':finished', 'tool_finished',
                                      {'call_id': call['provider_call_id'], 'tool': call['tool'], 'step': call['step'], 'result': cancelled})
                self.agent._event(db, run["session_id"], run_id, generation + 1, f"{run_id}:{generation}:cancelled", "run_state", {"status": "cancelled", "mode": mode})
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if mode == "cancel_owned_jobs":
            await asyncio.to_thread(self.jobs.cancel_owned, project_id, run_id)
        result = self.agent.run(project_id, run_id)
        if request_id:
            with self.services.store.transaction() as db:
                self.agent.finish_mutation(db, run['session_id'], request_id, result)
        return result

    async def resume_interrupted(self, project_id, run_id, generation, *, request_id=None):
        async with self.control_lock:
            if request_id:
                with self.services.store.transaction() as db:
                    db.execute('BEGIN IMMEDIATE')
                    run = self.agent._run(db, project_id, run_id)
                    old = self.agent.mutation(db, run['session_id'], request_id, 'resume', {'generation': generation}, run_id)
                    if old and old['state'] == 'applied':
                        return
                # A crash after the transition but before the acknowledgment may
                # leave an already resumed run. Never advance a different generation.
                if old and run['generation'] == generation and run['status'] != 'interrupted':
                    if run['status'] == 'queued' and run['owner'] is None:
                        self.schedule(run, resume=True)
                else:
                    await self._resume_interrupted(project_id, run_id, generation)
                with self.services.store.transaction() as db:
                    self.agent.finish_mutation(db, run['session_id'], request_id, {'run_id': run_id, 'generation': generation})
            else:
                await self._resume_interrupted(project_id, run_id, generation)

    async def _resume_interrupted(self, project_id, run_id, generation):
        run = self.agent.run(project_id, run_id)
        if run["status"] != "interrupted" or run["generation"] != generation:
            raise Conflict("只有重启中断的运行可显式继续")
        self.connection.assert_current(run["config"]["revision"])
        if run["context"].get("selection"):
            from .selection import validate_snapshot
            with self.services.store.transaction() as db:
                validate_snapshot(db, project_id, run["context"]["selection"], allow_initial_render=True)
        config = {"configurable": {"thread_id": run["graph_thread_id"]}}
        async with self.checkpoints.activity(run["graph_thread_id"]):
            try:
                snapshot = await self.graph.aget_state(config)
            except Exception:
                raise Conflict("检查点无法读取；请保留文件并核对已有任务后新建会话") from None
            if not snapshot.values or snapshot.values.get("run_id") != run_id or snapshot.values.get("project_id") != project_id or snapshot.values.get("graph_version") != GRAPH_VERSION or snapshot.values.get("state_version") != 1:
                raise Conflict("检查点缺失或不兼容；请核对任务后新建会话")
            from .job_recovery import resume_owned
            await asyncio.to_thread(resume_owned, self.agent, self.services, project_id, run_id, generation)
            # Reconcile business effects before advancing the saver. A wakeup carries
            # no permission and cannot turn a pending decision into an approval.
            linked = self.services.store.rows("""SELECT DISTINCT o.id FROM agent_operations o JOIN agent_job_links j ON j.operation_id=o.id
                WHERE o.run_id=? AND o.project_id=?""", (run_id, project_id))
            for operation in linked:
                self.jobs.operation_result(project_id, run_id, operation["id"])
            update = {"generation": generation}
            decision_id = snapshot.values.get("decision_id")
            if decision_id:
                rows = self.services.store.rows("SELECT * FROM agent_decisions WHERE id=? AND run_id=?", (decision_id, run_id))
                if not rows:
                    raise Conflict("待回复记录缺失，请核对任务后新建会话")
                old = rows[0]
                decision = self.agent.create_decision(project_id, run_id, generation, json.loads(old["payload"]),
                    kind=old["kind"], scope=json.loads(old["scope"]), revision=old["revision"])
                update["decision_id"] = decision["id"]
                if decision["id"] != decision_id:
                    with self.services.store.transaction() as db:
                        db.execute("UPDATE agent_decisions SET status='obsolete' WHERE id=?", (decision_id,))
            await self.graph.aupdate_state(config, update)
            with self.services.store.transaction() as db:
                db.execute("UPDATE agent_resume_intents SET state='obsolete' WHERE run_id=? AND generation<>? AND state IN ('pending','claimed')", (run_id, generation))
        self.agent.transition(project_id, run_id, generation, "queued", event_key=f"{run_id}:{generation}:resume")
        current = self.agent.run(project_id, run_id)
        if decision_id or snapshot.interrupts and snapshot.interrupts[0].value.get("kind") == "jobs":
            self._lease(current)
            self.agent.transition(project_id, run_id, generation, "running", event_key=f"{run_id}:{generation}:resume-running")
            self.agent.transition(project_id, run_id, generation, "waiting_user" if decision_id else "waiting_jobs",
                                  event_key=f"{run_id}:{generation}:resume-waiting")
        elif not snapshot.next:
            self.agent.transition(project_id, run_id, generation, "running", event_key=f"{run_id}:{generation}:resume-running")
            if snapshot.values.get("outcome") not in {"answered", "partial", "success"}:
                self.agent.transition(project_id, run_id, generation, "interrupted", event_key=f"{run_id}:{generation}:missing-outcome")
                raise Conflict("完成检查点缺少结算结果，不能宣告成功")
            self.agent.transition(project_id, run_id, generation, "completed", event_key=f"{run_id}:{generation}:resume-completed", outcome=snapshot.values["outcome"])
        else:
            self.schedule(current, resume=True)

    async def close(self):
        self.closed = True
        if self.observer:
            self.observer.cancel()
            await asyncio.gather(self.observer, return_exceptions=True)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.agent.interrupt_on_startup()
        await self.checkpoints.close()

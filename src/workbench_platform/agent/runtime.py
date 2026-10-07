"""Persistent platform runtime around one LangGraph, with native job observers."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import uuid

from .connection import ControllerConnection
from .context import prepare_request, core_tools
from .environment import compose, environment
from .skills import SkillService, registration
from .dependencies import load_dependencies
from .graph import build_graph
from .providers import payload_for, request, ProviderFault, SYSTEM, SYSTEM_VERSION, SYSTEM_PROMPTS
from .registry import ToolContext, ToolRegistry, invoke, tool_activity
from .store import AgentStore, Conflict, RunSuperseded, canonical, digest, now
from .tools import context, data, ocr, classification, forensics, jobs, pdf, results, audit, classification_management, ocr_management, ocr_processing, native_catalog, platform_management, documents, skills, history, screen_batches
from workbench_platform.application_services.native import model_projection


class AgentRuntime:
    def __init__(self, root: Path, services, *, model_provider=None):
        self.store = AgentStore(root)
        self.services = services
        self.registry = ToolRegistry()
        context.register(self.registry)
        data.register(self.registry)
        ocr.register(self.registry)
        ocr_management.register(self.registry)
        ocr_processing.register(self.registry)
        screen_batches.register(self.registry)
        classification.register(self.registry)
        classification_management.register(self.registry)
        native_catalog.register(self.registry)
        forensics.register(self.registry)
        jobs.register(self.registry)
        pdf.register(self.registry)
        results.register(self.registry)
        documents.register(self.registry)
        audit.register(self.registry)
        platform_management.register(self.registry)
        skills.register(self.registry)
        history.register(self.registry)
        load_dependencies()
        self.skills = SkillService(self.store, registry=self.registry, **registration())
        self.registry.tool_guard = lambda call, ctx: call['tool'] in self.skills.allowed_tools(
            ctx.session_id, [call['tool']], core=core_tools(self.registry))
        self.connection = ControllerConnection(self.store, production_preflight=self.production_preflight)
        from .tasks import AuditTasks
        self.tasks = AuditTasks(self)
        self.model_provider = model_provider
        self.round_limit = int(os.environ.get('WORKBENCH_AGENT_ROUND_LIMIT', '20'))
        self.tool_limit = int(os.environ.get('WORKBENCH_AGENT_TOOL_LIMIT', '60'))
        if not 1 <= self.round_limit <= 200 or not 1 <= self.tool_limit <= 600:
            raise ValueError('Agent预算配置无效')
        self.stack = AsyncExitStack()
        self.graph = None
        self.running = {}
        self.reschedules = {}
        self.observer_task = None
        self.closing = False
        self.model_projection = model_projection

    async def start(self):
        _, Saver = load_dependencies()
        saver = await self.stack.enter_async_context(Saver.from_conn_string(str(self.store.root / 'checkpoints.sqlite3')))
        self.graph = build_graph(self, saver)
        # Business terminal status may commit immediately before the display
        # projection. Repair that cold-stop window without replaying a request,
        # graph node or native submission.
        for run in self.store.rows("""SELECT r.* FROM runs r
            WHERE r.status IN ('answered','succeeded','partial','incomplete','failed','cancelled','needs_verification')
            AND (EXISTS (SELECT 1 FROM public_responses p WHERE p.run_id=r.id)
                 OR EXISTS (SELECT 1 FROM events e WHERE e.run_id=r.id AND e.kind='agent_activity'))
            AND NOT EXISTS (SELECT 1 FROM events e WHERE
                e.identity=r.id||':public-terminal:'||r.generation||':'||r.status)"""):
            self.store.finish_public(run['id'], run['status'], run.get('error') or '', generation=run['generation'])
        self.observer_task = asyncio.create_task(self.observe(), name='platform-agent-native-observer')
        for run in self.store.rows("SELECT * FROM runs WHERE status IN ('running','waiting_job','waiting_user')"):
            snapshot = await self.graph.aget_state(self.options(run['id']))
            if snapshot.values:
                # Business status can commit just before the interrupt's
                # checkpoint. Re-enter that saved node after a cold stop; a
                # fully saved question stays suspended for its bound answer.
                if run['status'] != 'waiting_user' or not any(task.interrupts for task in snapshot.tasks):
                    self.schedule(run['id'], None)
            else:
                events = self.store.rows("SELECT payload FROM events WHERE run_id=? AND identity=?", (run['id'], run['request_id']))
                if events:
                    content = json.loads(events[0]['payload'])['content']
                    self.schedule(run['id'], self.initial_state(run, content))

    async def close(self):
        self.closing = True
        if self.observer_task:
            self.observer_task.cancel()
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *([self.observer_task] if self.observer_task else []), return_exceptions=True)
        await self.stack.aclose()

    def context(self, state):
        session = self.store.session(state['session_id'])
        return ToolContext(state['workspace_id'], state['session_id'], state['run_id'], state['generation'],
                           state.get('step', 0), '', tuple(session['selected_assets']), self.services, self.store)

    def current(self, state):
        return self.store.assert_current(state['run_id'], state['generation'])

    def status(self, state, status, error=None):
        self.store.status(state['run_id'], state['generation'], status, error)

    def event(self, state, identity, kind, payload):
        self.store.event(state['session_id'], state['run_id'], state['run_id'] + ':' + identity, kind,
                         {**payload, 'generation': state['generation']})

    def activity(self, state, phase, **fields):
        payload = {'phase': phase, **fields}
        self.store.public_event(state['run_id'], state['generation'],
            state['run_id'] + ':activity:' + digest([state.get('step', 0), payload]), 'agent_activity', payload)

    def generated_response(self, state, record, response):
        request_id = record['id']
        content = response.get('content')
        if isinstance(content, str) and content:
            self.store.response_state(state['run_id'], state['generation'], request_id,
                request_id + ':text', 'generated', content=content, source='text')
        # Rehydrate a received response without sending or dispatching again.
        for index, call in enumerate(response.get('calls', [])):
            if call['tool'] not in {'conversation_answer', 'delivery_publish'}:
                continue
            source, key = ('answer', 'content') if call['tool'] == 'conversation_answer' else ('delivery', 'summary')
            content = call['arguments'].get(key)
            if not isinstance(content, str):
                # Public projection must not preempt the normal whole-batch
                # schema rejection with an unrelated database binding error.
                continue
            rows = self.store.rows('SELECT display_id FROM public_responses WHERE request_id=? AND call_id=?',
                                   (request_id, call['call_id']))
            display_id = rows[0]['display_id'] if rows else request_id + ':call:' + str(index)
            self.store.response_state(state['run_id'], state['generation'], request_id, display_id, 'generated',
                content=content, source=source, call_id=call['call_id'], tool=call['tool'])
        for row in self.store.rows("SELECT * FROM public_responses WHERE request_id=? AND status='generating'", (request_id,)):
            self.store.response_state(state['run_id'], state['generation'], request_id, row['display_id'], 'generated')

    def confirm_response(self, state, call, result):
        if call['tool'] not in {'conversation_answer', 'delivery_publish'} or result['status'] != 'success':
            return None
        if call['tool'] == 'delivery_publish' and result.get('data', {}).get('verified') is not True:
            return None
        records = self.store.rows('SELECT id FROM model_requests WHERE run_id=? AND step=?', (state['run_id'], state['step']))
        if not records:
            return None
        request_id = records[0]['id']
        rows = self.store.rows('SELECT display_id FROM public_responses WHERE request_id=? AND call_id=?',
                               (request_id, call['call_id']))
        display_id = rows[0]['display_id'] if rows else request_id + ':call:' + str(
            next((index for index, item in enumerate(state.get('calls', [])) if item['call_id'] == call['call_id']), 0))
        source = 'answer' if call['tool'] == 'conversation_answer' else 'delivery'
        content = result['data']['conversation_answer'] if source == 'answer' else result['summary']
        self.store.response_state(state['run_id'], state['generation'], request_id, display_id, 'confirmed',
            content=content, source=source, call_id=call['call_id'], tool=call['tool'],
            references=[*result.get('result_refs', []), *result.get('artifact_refs', [])])
        return display_id

    def close_presentations(self, state, status, reason=''):
        self.store.finish_public(state['run_id'], status, reason, generation=state['generation'])

    def inbox(self, state):
        received = set(state.get('inbox_received', []))
        rows = self.store.rows('SELECT * FROM inbox WHERE run_id=? ORDER BY created,id LIMIT 100', (state['run_id'],))
        incoming = [{'id': value['id'], 'role': 'user', 'content': value['content']}
                    for value in rows if value['id'] not in received]
        return incoming, sorted(received | {value['id'] for value in rows})

    async def model(self, state):
        run = self.current(state)
        self.activity(state, 'preparing_request')
        if self.model_provider:
            # Deterministic engineering providers use the same request assembly,
            # budget, activation and request ledger as production.
            config = {'protocol': 'openai_chat_completions', 'model': 'engineering-local',
                      'revision': run['connection_revision'], 'context_cap': 16384,
                      'max_output_tokens': 2048, 'streaming': False}
            secret = ''
        else:
            config, secret = self.connection.resolve(run['connection_revision'])
            self.connection.assert_current(run['connection_revision'])
        previous = self.store.model_record(run['id'], state['generation'], state['step'])
        task = self.tasks.get(state['session_id'])
        facts = self.session_facts(run, task=task)
        session = self.store.session(state['session_id'])
        skill_snapshot = self.skills.request_snapshot(state['workspace_id'], state['session_id'],
                                                     scan_sources=previous is None)
        active_skills = skill_snapshot.active
        environment_values = environment(config, workspace_id=state['workspace_id'], session_id=state['session_id'],
            scope_revision=task['scope_revision'], manual=task['manual'],
            families=(name.split('_', 1)[0] for name in self.registry.tools))
        host_contract = {'system': SYSTEM,
            'environment': environment_values,
            'tool_schemas': self.registry.schemas(self.registry.tools),
            'tool_versions': {name: {'version': spec.version, 'effect': spec.effect,
                'input_kinds': spec.input_kinds, 'output_kinds': spec.output_kinds}
                for name, spec in self.registry.tools.items()}}
        # Recover the already-received response for this exact checkpoint.
        # A live catalog/source description may change after transmission;
        # it must not replace the recorded request or its selected contracts.
        # Changed goals, scope, rules, config or fixed Skill versions conflict.
        input_state = {key: value for key, value in state.items()
                       if key not in {'answer_display_id', 'delivery_display_id'}}
        def input_version(contract):
            return digest({'state': input_state, 'config': config,
                'session': session, 'facts': facts, 'host_contract': contract,
                'skills': [{'name': item['name'], 'version': item['active_version']} for item in active_skills]})
        input_state_version = input_version(host_contract)
        if previous:
            if SYSTEM_PROMPTS.get(SYSTEM_VERSION) != SYSTEM:
                raise Conflict('当前模型提示契约未登记，不能兼容恢复原请求')
            events = self.store.rows('SELECT payload FROM events WHERE run_id=? AND identity=? AND kind=?',
                (run['id'], run['id'] + ':' + previous['id'] + ':request', 'model_request'))
            saved = json.loads(events[0]['payload']) if events else {}
            snapshot = saved.get('request_snapshot')
            if snapshot is not None:
                if not isinstance(snapshot, dict):
                    raise Conflict('原模型请求的提示快照无法核实')
                system = SYSTEM_PROMPTS.get(snapshot.get('system_version'))
                if (snapshot.get('format') != 'clear-model-input-v1' or system is None or
                        snapshot.get('system_sha256') != digest(system)):
                    raise Conflict('原模型请求的提示版本无法核实')
                original_contract = {**host_contract, 'system': system}
                if snapshot.get('host_contract_version') != digest(original_contract):
                    raise Conflict('模型请求与原工具或环境契约不同')
                versions = [input_version(original_contract)]
            else:
                # Older requests retain an input hash but no prompt-version
                # field. Rebuild only explicitly known compatible prompts;
                # every goal, scope, Skill, config and tool contract still hashes.
                versions = [input_version({**host_contract, 'system': system})
                            for system in SYSTEM_PROMPTS.values()]
            if saved.get('input_state_version') is None or saved['input_state_version'] not in versions:
                raise Conflict('模型请求与原输入状态不同或原输入记录缺失')
            self.current(state)
            if previous['state'] == 'received':
                response = json.loads(previous['response'])
                self.generated_response(state, previous, response)
                return response
            raise ProviderFault('response_unknown', '此轮主控已有发送记录但回包未知；未自动重发')
        system, injection, components = compose(SYSTEM, config,
            environment_values=environment_values,
            skill_prompt='[登记技能精简名录；正文按需启用]\n' + canonical(
                skill_snapshot.catalog) + '\n' + skill_snapshot.instructions)
        injection.update(facts_version=digest(facts), skills=active_skills)
        # Rebuild the bounded server facts for each request. Original graph
        # history and operation/evidence records are never rewritten.
        messages = [message for message in state['messages']
                    if not message.get('id', '').endswith((':session-facts', ':current-facts'))]
        messages.insert(0, {'id': run['id'] + ':current-facts', 'role': 'user',
            'content': '[服务器工作事实；用户目标/规则是任务约束，操作状态为历史回执；继续前查询当前状态]\n' + canonical(facts)})
        allowed = skill_snapshot.allowed_tools(state['visible'], core=core_tools(self.registry))
        visible = [name for name in state['visible'] if name in allowed]
        screen_calls={call['call_id'] for message in state['messages'] if message['role']=='assistant'
            for call in message.get('calls',[]) if call['tool']=='ocr_screen_batch_run'}
        has_screen_job=bool(state.get('background_jobs')) or any(message['role']=='tool' and
            message.get('call_id') in screen_calls and message['result'].get('status') in {'success','pending'}
            for message in state['messages'])
        payload, metrics, selected = prepare_request(config, messages, self.registry, visible,
            system=system, protected=[name for name in state.get('protected_tools', ()) if name in allowed],
            system_components=components,
            reclaimable_core=tuple(name for name in core_tools(self.registry) if name not in {
                'conversation_answer','tools_list','tools_describe','ask_user','session_read_history',
                'delivery_publish','ocr_screen_batch_export'}) if has_screen_job else ())
        injection['tools'] = selected
        record, fresh = self.store.begin_model(run['id'], state['generation'], state['step'],
            digest({'payload': payload, 'connection_revision': config['revision']}))
        if not fresh:
            if record['state'] == 'received':
                response = json.loads(record['response'])
                self.generated_response(state, record, response)
                return {**response, 'selected_tools': response.get('selected_tools', selected)}
            raise ProviderFault('response_unknown', '此轮主控已有发送记录但回包未知；未自动重发')
        self.event(state, record['id'] + ':request', 'model_request', {'step': state['step'],
            'input_state_version': input_state_version,
            'request_snapshot': {'format': 'clear-model-input-v1', 'system_version': SYSTEM_VERSION,
                'system_sha256': digest(SYSTEM), 'host_contract_version': digest(host_contract)},
            'model': config['model'], 'configured_model': config['model'],
            'compressed': metrics['compressed'], 'token_estimate': metrics['input_tokens_estimate'],
            'is_estimate': True, 'context_metrics': metrics, 'injection': injection,
            'engineering_provider': bool(self.model_provider)})
        self.activity(state, 'model_wait', request_id=record['id'])
        receiving = False
        def persist_preview(frame):
            nonlocal receiving
            self.current(state)
            if not self.model_provider:
                self.connection.assert_current(run['connection_revision'])
            self.store.preview_response(run['id'], state['generation'], record['id'], frame)
            if not receiving:
                self.activity(state, 'receiving_response', request_id=record['id'])
                receiving = True
        async def preview(frame):
            # SQLite commits must not block the ASGI loop that delivers them.
            # Provider callbacks are awaited in order; the store owns the
            # atomic generation fence within each worker transaction.
            await asyncio.to_thread(persist_preview, frame)
        async def transport(info):
            self.store.public_event(run['id'], state['generation'], record['id'] + ':transport', 'model_transport',
                {'request_id': record['id'], **info})
        response = await self.model_provider(state, self.registry.schemas(selected)) if self.model_provider else \
            await request(config, secret, payload, record['id'], on_preview=preview, on_transport=transport)
        self.current(state)
        if not self.model_provider:
            self.connection.assert_current(run['connection_revision'])
        response = {**response, 'selected_tools': selected}
        self.store.finish_model(record['id'], response)
        self.generated_response(state, record, response)
        self.event(state, record['id'] + ':usage', 'usage', {**response['usage'],
            'configured_model': config['model'], 'returned_model': response.get('returned_model'),
            'engineering_provider': bool(self.model_provider)})
        return response

    def production_preflight(self, config):
        system, injection, components = compose(SYSTEM, config,
            workspace_id='production-preflight', session_id='production-preflight',
            families=(name.split('_', 1)[0] for name in self.registry.tools),
            skill_prompt='[登记技能精简名录;正文按需启用]\n' + canonical(
                self.skills.catalog_prompt('production-preflight')))
        _, metrics, selected = prepare_request(config,
            [{'role': 'user', 'content': '你好，请说明平台可调用的工具和模型。'}],
            self.registry, core_tools(self.registry), system=system, system_components=components)
        return {'version': 'production-first-request-v1', 'passed': True,
                'context_metrics': metrics, 'injection': injection, 'tools': selected,
                'scope': '首轮核心工具与可信环境本地预检；实际任务/技能按每次请求重新计量'}

    async def settle(self, result, state):
        from workbench_platform.application_services.job_lifecycle import adapter
        self.current(state)
        outputs = []
        for reference in result['job_refs']:
            self.current(state)
            lifecycle = adapter(reference)
            context = self.context(state)
            if lifecycle.prepare:
                await invoke(lifecycle.prepare, reference, context)
            self.current(state)
            value = await invoke(lifecycle.settle, reference, context)
            self.current(state)
            if value is None:
                return None
            outputs.append(value)
        if len(outputs) != 1:
            raise ValueError('Job bundle settlement is unsupported')
        self.current(state)
        return {**outputs[0], 'job_refs': result['job_refs']}

    def budget_decision(self, state):
        identifier = 'd' + digest([state['run_id'], 'budget', state['step']])[:40]
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO decisions(id,run_id,question,revision,kind) VALUES(?,?,?,1,'budget')",
                       (identifier, state['run_id'], '本轮已达到预算。可以增加20轮、60次工具调用，或结束本轮。已有成果保留。'))
        return identifier

    def options(self, run_id):
        # The server alone chooses checkpoint identity and generation. A browser
        # cannot select thread_id, a graph node or a resume command payload.
        return {'configurable': {'thread_id': run_id}, 'recursion_limit': 600}

    def schedule(self, run_id, input):
        if self.closing:
            return
        if run_id in self.running:
            if input is not None:
                self.reschedules[run_id] = input
            return
        task = asyncio.create_task(self.drive(run_id, input), name='platform-agent-' + run_id)
        self.running[run_id] = task
        def finished(_):
            self.running.pop(run_id, None)
            pending = self.reschedules.pop(run_id, None)
            if pending is not None:
                self.schedule(run_id, pending)
        task.add_done_callback(finished)

    async def drive(self, run_id, input):
        generation = self.store.run(run_id)['generation']
        try:
            await self.graph.ainvoke(input, self.options(run_id), durability='sync')
        except asyncio.CancelledError:
            raise
        except RunSuperseded:
            pass
        except Conflict:
            self.finish_error(run_id, generation, 'failed', 'execution_conflict',
                              '执行身份或配置版本已变化，请读取当前状态后继续')
        except ProviderFault as error:
            status = 'needs_verification' if error.code == 'response_unknown' else 'failed'
            self.finish_error(run_id, generation, status, error.code, str(error), context_metrics=error.context_metrics,
                response_diagnostics=error.response_diagnostics)
        except Exception:
            self.finish_error(run_id, generation, 'failed', 'runtime_failed',
                              '运行未完成；请查看步骤与实际任务状态，已有成果保留')

    def finish_error(self, run_id, generation, status, code, message, *, context_metrics=None,response_diagnostics=None):
        try:
            self.store.status(run_id, generation, status, message)
        except RunSuperseded:
            return
        run = self.store.run(run_id)
        self.store.event(run['session_id'], run_id, run_id + ':error:' + code + ':' + str(generation),
                         'error', {'code': code, 'message': message,
                                   **({'response_diagnostics': response_diagnostics} if response_diagnostics else {}),
                                   **({'context_metrics': context_metrics} if context_metrics else {})})
        self.store.finish_public(run_id, status, message, generation=generation)

    async def observe(self):
        from langgraph.types import Command
        while not self.closing:
            await asyncio.sleep(0.5)
            for run in self.store.rows("SELECT * FROM runs WHERE status='waiting_user'"):
                if run['id'] in self.running:
                    continue
                snapshot = await self.graph.aget_state(self.options(run['id']))
                questions = [item.value.get('decision_id') for task in snapshot.tasks for item in task.interrupts
                             if isinstance(item.value, dict) and item.value.get('kind') == 'question']
                for question in questions:
                    answered = self.store.rows('SELECT id FROM decisions WHERE id=? AND run_id=? AND reply IS NOT NULL',
                                               (question, run['id']))
                    if answered:
                        self.schedule(run['id'], Command(resume={'bound_decision': question}))
                        break
            for run in self.store.rows("SELECT * FROM runs WHERE status='waiting_job'"):
                if run['id'] in self.running:
                    continue
                snapshot = await self.graph.aget_state(self.options(run['id']))
                if not snapshot.values or not snapshot.next:
                    continue
                try:
                    state = snapshot.values
                    from .graph import screen_pending
                    background = state.get('background_jobs', []) if 'await_background' in snapshot.next else []
                    waiting = [item['result'] for item in background] or [state['results'][-1]]
                    from .progress import progress_for
                    for reference in [ref for value in waiting for ref in value.get('job_refs', [])]:
                        try:
                            progress = await invoke(progress_for, reference, self.context(state))
                            stamp = digest(progress)
                            self.store.public_event(run['id'], run['generation'],
                                run['id'] + ':progress:' + digest(reference) + ':' + stamp, 'job_progress',
                                {**progress, 'job_ref': reference, 'sampled_at': now()})
                        except RunSuperseded:
                            raise
                        except Exception:
                            # Display sampling and persistence are optional;
                            # their failure must not
                            # interrupt settling the original native operation.
                            pass
                    # Only screen-batch waits admit new user goals before
                    # native settlement. The original durable operation stays
                    # pending; other families preserve their blocking wait.
                    directed = bool(self.inbox(state)[0]) and all(screen_pending(value) for value in waiting)
                    ready = directed
                    if not ready:
                        for value in waiting:
                            if await self.settle(value, state) is not None:
                                ready = True
                                break
                    if ready:
                        self.schedule(run['id'], Command(resume={'observer': True}))
                except Conflict:
                    continue
                except Exception:
                    self.store.status(run['id'], run['generation'], 'needs_verification', '原生任务状态需要核实')
                    self.store.finish_public(run['id'], 'needs_verification', '原生任务状态需要核实', generation=run['generation'])

    async def reconcile(self, run_id):
        run = self.store.run(run_id)
        if run['status'] in {'running', 'waiting_job'}:
            self.schedule(run_id, None)
            return run
        if run['status'] != 'needs_verification' or run_id in self.running:
            raise Conflict('此运行当前不可核对')
        unknown = self.store.rows("SELECT id FROM model_requests WHERE run_id=? AND state!='received'", (run_id,))
        if unknown:
            raise Conflict('主控回包未知，无法从原生任务核实；请结束本轮后以新目标继续')
        snapshot = await self.graph.aget_state(self.options(run_id))
        state = snapshot.values
        if not state or not state.get('calls') or not state.get('results'):
            raise Conflict('没有可核实的原生操作')
        call = state['calls'][state['cursor']]
        specification = self.registry.tools[call['tool']]
        records = self.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?', (run_id, call['call_id']))
        if not records or not specification.lookup_operation:
            raise Conflict('此工具没有原生回执查询入口；原操作保留')
        from dataclasses import replace
        context = replace(self.context(state), operation_id=records[0]['id'])
        recovered = await invoke(specification.lookup_operation, specification.input_schema.model_validate(call['arguments']), context)
        self.current(state)
        if recovered is None:
            raise Conflict('原生系统尚无可核实回执，未重新提交操作')
        recovered = self.registry.validated_result(specification, recovered, context)
        self.store.finish_operation(records[0]['id'], recovered)
        await self.graph.aupdate_state(self.options(run_id), {'results': [*state['results'][:-1], recovered]}, as_node='dispatch')
        self.store.status(run_id, run['generation'], 'running')
        tool_activity(context, call, 'restored', recovered)
        self.schedule(run_id, None)
        return self.store.run(run_id)

    async def submit(self, session_id, content, request_id):
        session = self.store.session(session_id)
        if self.tasks.get(session_id)['manual']:
            raise ValueError('任务正在人工处理，请先返回智能审计')
        previous_request = self.store.rows('SELECT * FROM runs WHERE request_id=?', (request_id,))
        if previous_request:
            if previous_request[0]['fingerprint'] != digest([session_id, content]):
                raise Conflict('请求身份与原目标不同')
            return previous_request[0]
        previous_inbox = self.store.rows('SELECT i.*,r.session_id FROM inbox i JOIN runs r ON r.id=i.run_id WHERE i.id=?', (request_id,))
        if previous_inbox:
            if (previous_inbox[0]['session_id'], previous_inbox[0]['content']) != (session_id, content):
                raise Conflict('追加请求身份与原目标不同')
            return self.store.run(previous_inbox[0]['run_id'])
        view = self.connection.view()
        if not self.model_provider and not view.get('available'):
            raise ValueError('请先在主控设置中配置并测试模型连接')
        active = self.store.rows("SELECT * FROM runs WHERE session_id=? AND status IN ('running','waiting_job','waiting_user')",
                                 (session_id,))
        if active:
            run = active[0]
            with self.store.transaction() as db:
                old = db.execute('SELECT * FROM inbox WHERE id=?', (request_id,)).fetchone()
                if old and (old['run_id'], old['content']) != (run['id'], content):
                    raise Conflict('追加请求与原参数不同')
                if len(self.store.rows('SELECT id FROM inbox WHERE run_id=?', (run['id'],))) >= 100:
                    raise Conflict('本轮追加目标数量已达到上限')
                db.execute('INSERT OR IGNORE INTO inbox(id,run_id,content,created) VALUES(?,?,?,?)',
                           (request_id, run['id'], content, now()))
            self.store.event(session_id, run['id'], request_id, 'message', {'role': 'user', 'content': content, 'queued': True})
            return self.store.run(run['id'])
        run, fresh = self.store.start_run(session_id, content, request_id, view.get('revision', 0), self.round_limit, self.tool_limit)
        if fresh:
            self.tasks.record_goal(session_id, content)
            self.schedule(run['id'], self.initial_state(run, content))
        return run

    def initial_state(self, run, content):
        session = self.store.session(run['session_id'])
        previous = self.store.rows("SELECT payload FROM events WHERE session_id=? AND kind='delivery' AND run_id!=? ORDER BY seq DESC LIMIT 1", (session['id'], run['id']))
        inputs = [{'id': run['request_id'], 'role': 'user', 'content': content}]
        history = self.session_facts(run)
        if history:
            inputs.insert(0, {'id': run['id'] + ':session-facts', 'role': 'user',
                'content': '[此前服务器记录的会话目标和操作引用；状态是历史回执，继续前查询当前工具状态]\n' + canonical(history)})
        if previous:
            inputs.insert(0, {'id': run['id'] + ':prior-delivery', 'role': 'user',
                             'content': '[上一轮服务器记录的交付]\n' + previous[0]['payload']})
        from .scope import scope_summary
        inputs.insert(0, {'id': run['id'] + ':scope', 'role': 'user', 'content': '[服务器会话范围]\n' +
            canonical({**scope_summary(ToolContext(session['workspace_id'],session['id'],run['id'],run['generation'],0,'',tuple(session['selected_assets']),self.services,self.store)),
                       'initial_refs': session['initial_refs']})})
        return {'run_id': run['id'], 'session_id': session['id'], 'workspace_id': session['workspace_id'],
            'generation': run['generation'], 'messages': inputs, 'step': 0, 'tool_count': 0,
            'visible': core_tools(self.registry), 'protected_tools': [],
            'inbox_received': [], 'delivery': None, 'calls': [], 'results': [], 'correction_count': 0,
            'background_jobs': []}

    def session_facts(self, run, *, task=None):
        goals = self.store.rows("SELECT payload FROM events WHERE session_id=? AND (run_id IS NULL OR run_id!=?) AND kind='message' ORDER BY seq DESC LIMIT 30",
                                (run['session_id'], run['id']))
        discussion = [json.loads(row['payload']) for row in reversed(goals)]
        goals = [str(row.get('content', '')) for row in discussion if row.get('role') == 'user'][-8:]
        discussion = [{'role': row['role'], 'content': str(row.get('content', ''))[:1600],
                       'preview_only': len(str(row.get('content', ''))) > 1600} for row in discussion
                      if row.get('role') in {'user', 'assistant'} and row.get('content')][-8:]
        rows = self.store.rows('SELECT o.* FROM operations o JOIN runs r ON r.id=o.run_id '
            'WHERE r.session_id=? AND r.id!=? AND o.result IS NOT NULL ORDER BY o.created DESC LIMIT 20',
            (run['session_id'], run['id']))
        operations = []
        for row in reversed(rows):
            value = json.loads(row['result'])
            fields = ('kind', 'workspace_id', 'object_id', 'revision', 'sha256', 'application', 'native_id', 'parent_id', 'coverage')
            refs = {key: [{field: ref[field] for field in fields if field in ref} for ref in value.get(key, [])[-5:]]
                    for key in ('result_refs', 'job_refs', 'artifact_refs')}
            facts = {key: item for key, item in value.get('data', {}).items() if key in
                {'data_session_id', 'version', 'handoff_id', 'binding_id', 'batch_id', 'dataset_id', 'link_id', 'project_id',
                 'entity_id', 'model_id', 'active_model_id', 'engines', 'document_id', 'result_id'}}
            if isinstance(value.get('data', {}).get('models'), list):
                facts['models'] = [{key: model[key] for key in ('id', 'name') if key in model} for model in value['data']['models'][:10]]
            operations.append({'operation_id': row['id'], 'parent_operation_id': row['parent_id'], 'tool': row['tool'],
                'status': value['status'], 'summary': value['summary'][:300], 'data': facts, **refs})
        task = self.tasks.get(run['session_id']) if task is None else task
        profile = self.tasks.profile(run['workspace_id'])
        facts = {'goals': goals, 'operations': operations, 'discussion': discussion,
                 'audit_context': {key: str(profile.get(key, '')) for key in ('audit_object', 'period_start', 'period_end', 'objective', 'rules')},
                 'task': {key: task[key] for key in ('goal', 'scope_revision', 'manual', 'plan', 'plan_revision', 'revision')},
                 'profile_revision': profile.get('revision', 0),
                 'full_record_access': {'task': 'audit_get_task', 'project_rules': 'audit_get_project',
                                        'history': 'session_read_history'},
                 'omitted_history': {'discussion': 0, 'operations': 0, 'goals': 0}}
        while len(canonical(facts).encode('utf-8')) > 16000 and (operations or goals or discussion):
            if discussion:
                discussion.pop(0)
                facts['omitted_history']['discussion'] += 1
                continue
            if len(operations) > 1:
                operations.pop(0)
                facts['omitted_history']['operations'] += 1
            elif len(goals) > 1:
                goals.pop(0)
                facts['omitted_history']['goals'] += 1
            elif operations:
                operations.pop(0)
                facts['omitted_history']['operations'] += 1
            else:
                goals.pop(0)
                facts['omitted_history']['goals'] += 1
        return facts

    def cancel(self, run_id, cancel_owned_jobs=False):
        run = self.store.run(run_id)
        with self.store.transaction() as db:
            changed = self.store.cancel_in_transaction(db, run_id=run_id)
        if not changed:
            return self.store.run(run_id)
        native = []
        if cancel_owned_jobs:
            for record in self.store.rows("SELECT result FROM operations WHERE run_id=? AND state='pending'", (run_id,)):
                for reference in json.loads(record['result'])['job_refs']:
                    try:
                        # Cancellation uses only the persisted references owned
                        # by this run, never a browser-supplied native ID.
                        native.append(jobs.cancel_reference(reference, self.context({'session_id': run['session_id'],
                            'workspace_id': run['workspace_id'], 'run_id': run_id, 'generation': run['generation']})))
                    except (ValueError, KeyError):
                        native.append({'status': 'needs_verification', 'job_id': reference['object_id']})
        self.store.event(run['session_id'], run_id, run_id + ':cancel', 'cancelled',
                         {'message': '已停止后续动作', 'native_jobs': native, 'cancel_owned_jobs': cancel_owned_jobs})
        self.store.finish_public(run_id, 'cancelled', 'Agent 已停止后续动作；原生作业按其实际能力继续或取消',
                                 generation=changed[0]['generation'])
        return self.store.run(run_id)

    async def reply(self, decision_id, content, revision, request_id):
        from langgraph.types import Command
        with self.store.transaction() as db:
            decision = db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
            if not decision or decision['revision'] != revision:
                raise Conflict('问题已变化或不存在')
            if decision['kind'] == 'budget' and content not in {'继续（增加20轮、60次工具）', '结束本轮'}:
                raise Conflict('请选择明确的预算选项')
            run = self.store.run(decision['run_id'])
            if decision['reply'] and (decision['reply'], decision['request_id']) == (content, request_id):
                return run
            if run['status'] != 'waiting_user':
                raise Conflict('原运行未等待此问题')
            if decision['reply'] and (decision['reply'], decision['request_id']) != (content, request_id):
                raise Conflict('此问题已收到不同答复')
            db.execute('UPDATE decisions SET reply=?,request_id=? WHERE id=?', (content, request_id, decision_id))
            if decision['kind'] == 'budget' and content == '继续（增加20轮、60次工具）':
                db.execute('UPDATE runs SET round_limit=round_limit+20,tool_limit=tool_limit+60 WHERE id=?', (decision['run_id'],))
        self.store.event(run['session_id'], run['id'], request_id, 'message', {'role': 'user', 'content': content, 'decision_id': decision_id})
        self.store.event(run['session_id'], run['id'], decision_id + ':answered', 'question_answered',
                         {'decision_id': decision_id, 'content': content})
        self.schedule(run['id'], Command(resume={'bound_decision': decision_id}))
        return self.store.run(run['id'])

"""Clear's durable business supervisor around DeepSeek Harness."""
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
from .dependencies import load_support_dependencies
from .harness import HarnessEngine
from .harness_service import HarnessService
from .providers import payload_for, request, ProviderFault, SYSTEM, SYSTEM_VERSION, SYSTEM_PROMPTS
from .registry import ToolContext, ToolRegistry, invoke, tool_activity
from .store import AgentStore, Conflict, RunSuperseded, canonical, digest, now
from .tools import context, data, ocr, classification, forensics, jobs, pdf, results, audit, classification_management, ocr_management, ocr_processing, native_catalog, platform_management, documents, skills, history
from .tools import multimodal
from workbench_platform.application_services.native import model_projection

DIRECT_DRIVERS = {'image-text-v1': 'image_text', 'pdf-text-v1': 'pdf_text',
                  'data-action-v1': 'data_action', 'classification-predict-v1': 'classification_action'}


class AgentRuntime(HarnessService):
    def __init__(self, root: Path, services, *, model_provider=None):
        self.store = AgentStore(root)
        self.services = services
        from workbench_platform.application_services.direct_scope import resolve_harness_scope
        self.store.scope_resolver = lambda run: resolve_harness_scope(self, self.store.session(run['session_id']))
        self.registry = ToolRegistry()
        context.register(self.registry)
        data.register(self.registry)
        ocr.register(self.registry)
        ocr_management.register(self.registry)
        ocr_processing.register(self.registry)
        classification.register(self.registry)
        classification_management.register(self.registry)
        native_catalog.register(self.registry)
        forensics.register(self.registry)
        multimodal.register(self.registry)
        jobs.register(self.registry)
        pdf.register(self.registry)
        results.register(self.registry)
        documents.register(self.registry)
        audit.register(self.registry)
        platform_management.register(self.registry)
        skills.register(self.registry)
        history.register(self.registry)
        from .workspace_outputs import register as register_workspace_outputs
        register_workspace_outputs(self.registry)
        from workbench_platform.application_services.composed import register as register_composed
        register_composed(self.registry)
        from workbench_platform.application_services.ocr_tuning import register as register_tuning
        register_tuning(self.registry)
        self.agent_enabled = os.environ.get('WORKBENCH_AGENT_ENABLED', '1') != '0'
        self.agent_unavailable_reason = '' if self.agent_enabled else 'Agent 已停用；明确的本地操作仍可使用'
        if self.agent_enabled:
            try:
                load_support_dependencies()
            except (ImportError, FileNotFoundError, ValueError):
                self.agent_enabled = False
                self.agent_unavailable_reason = 'Agent 依赖未就绪；明确的本地操作仍可使用'
        self.skills = SkillService(self.store, registry=self.registry, scripts_supported=True, **registration())
        def tool_allowed(call, ctx):
            from .general import enabled as general_enabled
            graph = getattr(self, 'graph', None)
            if graph is not None and general_enabled(graph.load(ctx.run_id)):
                return True
            return call['tool'] in self.skills.allowed_tools(ctx.session_id, [call['tool']], core=core_tools(self.registry))
        self.registry.tool_guard = tool_allowed
        self.connection = ControllerConnection(self.store, production_preflight=self.production_preflight)
        from .tasks import AuditTasks
        self.tasks = AuditTasks(self)
        self.model_provider = model_provider
        self.round_limit = int(os.environ['WORKBENCH_AGENT_ROUND_LIMIT']) if os.environ.get('WORKBENCH_AGENT_ROUND_LIMIT') else None
        self.tool_limit = int(os.environ['WORKBENCH_AGENT_TOOL_LIMIT']) if os.environ.get('WORKBENCH_AGENT_TOOL_LIMIT') else None
        if any(value is not None and value < 1 for value in (self.round_limit, self.tool_limit)):
            raise ValueError('Agent预算配置无效')
        self.stack = AsyncExitStack()
        self.graph = None
        self.running = {}
        self.reschedules = {}
        self.resume_tasks = {}
        self.observer_task = None
        self.maintenance = {}
        self.result_collectors = {}
        self.closing = False
        self.model_projection = model_projection

    async def start(self):
        if self.agent_enabled:
            try:
                self.graph = HarnessEngine(self)
            except (ImportError, FileNotFoundError, ValueError):
                self.agent_enabled = False
                self.agent_unavailable_reason = 'Harness 依赖未就绪；明确的本地操作仍可使用'
        if self.graph is not None:
            from .harness_migration import migrate_legacy
            await migrate_legacy(self)
            from .harness_materialize import materialize_legacy
            await materialize_legacy(self)
        if self.graph is not None and self.store.rows('SELECT 1 FROM harness_events LIMIT 1'):
            from workbench_platform.resources.reader_features import require_features
            require_features(self.store.root.parent, 'agent-harness-v1')
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
            if run.get('driver') in DIRECT_DRIVERS:
                self.schedule(run['id'], None)
                continue
            if self.graph is None:
                continue
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
        tasks = [*self.running.values(), *self.maintenance.values(), *self.result_collectors.values(), *self.resume_tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *([self.observer_task] if self.observer_task else []), return_exceptions=True)
        if self.graph:
            await self.graph.close()
            self.graph.event_db.close()
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
        """Validate archived request receipts; Harness owns live model requests."""
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
        payload, metrics, selected = prepare_request(config, messages, self.registry, visible,
            system=system, protected=[name for name in state.get('protected_tools', ()) if name in allowed],
            system_components=components)
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

    async def settle(self, result, state, *, context=None):
        from workbench_platform.application_services.job_lifecycle import adapter
        self.current(state)
        outputs = []
        for reference in result['job_refs']:
            self.current(state)
            lifecycle = adapter(reference)
            context = context if context is not None else self.context(state)
            context.current()
            if lifecycle.prepare:
                await invoke(lifecycle.prepare, reference, context)
            context.current()
            self.current(state)
            value = await invoke(lifecycle.settle, reference, context)
            context.current()
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
        return {'configurable': {'thread_id': run_id}}

    def schedule(self, run_id, input):
        if self.closing:
            return
        if run_id in self.running:
            self.reschedules[run_id] = input
            return
        task = asyncio.create_task(self.drive(run_id, input), name='platform-agent-' + run_id)
        self.running[run_id] = task
        def finished(_):
            self.running.pop(run_id, None)
            if run_id in self.reschedules:
                pending = self.reschedules.pop(run_id)
                if isinstance(pending, dict) and pending.get('_resume_after_handoff') and not self.closing:
                    resumed = asyncio.create_task(self.resume_after_handoff(run_id))
                    self.resume_tasks[run_id] = resumed
                    resumed.add_done_callback(lambda _, identifier=run_id: self.resume_tasks.pop(identifier, None))
                    return
                if self.store.run(run_id)['status'] in {'running', 'waiting_job', 'waiting_user'}:
                    self.schedule(run_id, pending)
        task.add_done_callback(finished)

    async def resume_after_handoff(self, run_id):
        try:
            await self.reconcile(run_id)
        except (Conflict, ValueError) as error:
            run = self.store.run(run_id)
            self.store.event(run['session_id'], run_id, run_id + ':resume-deferred:' + now(),
                'continuation_deferred', {'reason': str(error)[:500], 'input_preserved': True,
                    'version': 1, 'generation': run['generation']})

    async def drive(self, run_id, input, *, _recovery_attempt=0):
        generation = self.store.run(run_id)['generation']
        try:
            driver = self.store.run(run_id).get('driver', 'harness')
            if driver in DIRECT_DRIVERS:
                import importlib
                module = importlib.import_module('workbench_platform.application_services.' + DIRECT_DRIVERS[driver])
                await module.drive(self, run_id)
                await self.continue_direct_followups(run_id)
            elif self.graph is not None:
                from .goal_interpreter import maybe_route
                route = json.loads(self.store.run(run_id).get('driver_state') or '{}')
                if route.get('local_goal_to_parse') and not self.graph.load(run_id) and isinstance(input, dict):
                    self.graph.save(input)
                if await maybe_route(self, run_id):
                    return
                saved_route = json.loads(self.store.run(run_id).get('driver_state') or '{}')
                if saved_route.get('interpretation_complete'):
                    parsed_state = input if isinstance(input, dict) else self.graph.load(run_id)
                    if parsed_state:
                        parsed_state['step'] = max(parsed_state.get('step', 0), self.store.run(run_id)['rounds'])
                        self.graph.save(parsed_state)
                await self.graph.ainvoke(input, self.options(run_id), durability='sync')
        except asyncio.CancelledError:
            raise
        except RunSuperseded:
            pass
        except Conflict as error:
            from workbench_platform.application_services.image_text import ReceiptUnknown
            from workbench_platform.application_services.errors import ResourceBusy
            if isinstance(error, ResourceBusy):
                self.finish_error(run_id, generation, 'paused', 'resource_busy', str(error) + ' 关联任务释放后点击继续原任务。')
            elif isinstance(error, ReceiptUnknown):
                self.finish_error(run_id, generation, 'needs_verification', 'response_unknown', str(error))
            else:
                self.finish_error(run_id, generation, 'failed', 'execution_conflict',
                                  '执行身份或配置版本已变化，请读取当前状态后继续')
        except ProviderFault as error:
            if self.store.run(run_id)['status'] == 'paused':
                return
            from .task_recovery import RECOVERABLE, AUTO_RETRY, recovery_message
            unknown = self.store.rows("SELECT id FROM model_requests WHERE run_id=? AND state='sent'", (run_id,))
            if error.code in AUTO_RETRY and _recovery_attempt < 1 and not unknown:
                self.store.public_event(run_id, generation, run_id + ':controller-retry:' + str(
                    self.store.rows('SELECT COUNT(*) AS n FROM model_requests WHERE run_id=?', (run_id,))[0]['n']),
                    'controller_retry', {'reason': error.code, 'attempt': 1})
                if self.graph is not None and driver not in DIRECT_DRIVERS:
                    state = self.graph.load(run_id)
                    state.pop('last_bridge_error', None)
                    self.graph.save(state)
                await self.collect_available_results(run_id)
                await asyncio.sleep(.2)
                current = self.store.run(run_id)
                if current['generation'] != generation or current['status'] in {'paused', 'cancelled'}:
                    return
                await self.drive(run_id, None, _recovery_attempt=1)
                return
            status = 'needs_verification' if error.code == 'response_unknown' else 'paused' if error.code in RECOVERABLE else 'failed'
            message = recovery_message(self, run_id, error.code, str(error)) if error.code in RECOVERABLE else str(error)
            self.finish_error(run_id, generation, status, error.code, message, context_metrics=error.context_metrics,
                              response_diagnostics=getattr(error, 'response_diagnostics', None))
        except ValueError as error:
            from workbench_platform.application_services.errors import DirectActionFault
            if isinstance(error, DirectActionFault):
                saved = json.loads(self.store.run(run_id).get('driver_state') or '{}')
                self.finish_error(run_id, generation, 'failed', error.code, str(error))
                self.store.event(self.store.run(run_id)['session_id'], run_id,
                    run_id + ':business-error:' + str(generation), 'business_error', {
                        'code': error.code, 'message': str(error), 'stage': saved.get('stage', 'validation'),
                        'accepted_write': bool(saved.get('receipts', {}).get('submitted')),
                        'retryable': False, 'user_action': error.user_action})
            else:
                self.finish_error(run_id, generation, 'failed', 'direct_action_failed' if
                    self.store.run(run_id).get('driver') in DIRECT_DRIVERS else 'runtime_failed',
                    '操作未完成；请读取当前任务与服务状态，已有回执保留')
        except Exception:
            self.finish_error(run_id, generation, 'failed', 'runtime_failed',
                              '运行未完成；请查看步骤与实际任务状态，已有成果保留')
        finally:
            if not self.closing:
                await self.collect_available_results(run_id)

    async def collect_available_results(self, run_id):
        # Completion observers and tool callbacks can race on one operation.
        # The event identity is durable; this guard also avoids duplicate work.
        collecting = getattr(self, '_collecting_results', None)
        if collecting is None:
            self._collecting_results = collecting = set()
        if run_id in collecting:
            return
        collecting.add(run_id)
        try:
            from .available_results import collect
            await collect(self, run_id)
        except RunSuperseded:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            run = self.store.run(run_id)
            self.store.event(run['session_id'], run_id,
                run_id + ':result-observer-error:' + digest(str(error))[:16], 'result_collection_error',
                {'message': str(error)[:500], 'generation': run['generation']})
        finally:
            collecting.discard(run_id)

    def finish_error(self, run_id, generation, status, code, message, *, context_metrics=None, response_diagnostics=None):
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
        observation = 0
        while not self.closing:
            await asyncio.sleep(.5)
            for run in self.store.rows("SELECT * FROM runs WHERE status IN ('waiting_job','waiting_user')"):
                if self.graph is None and run.get('driver') not in DIRECT_DRIVERS:
                    continue
                if run['id'] not in self.running:
                    self.schedule(run['id'], None)
            observation += 1
            if observation % 4:
                continue
            # Reading accepted jobs and exporting fixed results does not need
            # a live model, Harness process or a resumable analysis status.
            for run in self.store.rows("""SELECT r.* FROM runs r WHERE r.status!='cancelled'
                AND EXISTS (SELECT 1 FROM operations o WHERE o.run_id=r.id AND o.result IS NOT NULL)"""):
                if run['id'] not in self.result_collectors:
                    task = asyncio.create_task(self.collect_available_results(run['id']))
                    self.result_collectors[run['id']] = task
                    task.add_done_callback(lambda _, identifier=run['id']: self.result_collectors.pop(identifier, None))

    async def reconcile(self, run_id):
        run = self.store.run(run_id)
        from .task_recovery import RECOVERABLE, last_fault, prepare_resume, can_resume_run
        if run['status'] == 'cancelled':
            raise Conflict('用户已停止此任务，不会恢复后续步骤')
        recoverable_failure = run['status'] == 'failed' and last_fault(self, run_id) in RECOVERABLE
        if run.get('driver') in DIRECT_DRIVERS:
            if run['status'] not in {'paused', 'running', 'waiting_job', 'needs_verification'} and not recoverable_failure:
                raise Conflict('此运行当前不可继续')
            if run['status'] in {'paused', 'needs_verification'} or recoverable_failure:
                self.store.status(run_id, run['generation'], 'running')
                self.store.public_event(run_id, run['generation'], run_id + ':resumed:' + now(),
                    'run_resumed', {'reason': '继续原目标，保留已接受的作业与成果'})
            self.schedule(run_id, None)
            return self.store.run(run_id)
        if not self.agent_enabled or self.graph is None:
            raise Conflict('分析执行依赖尚未就绪，原运行和回执已保留；请恢复 Agent 后继续')
        if run['session_id'] in self.maintenance:
            raise Conflict('会话正在整理上下文，请稍后继续')
        if can_resume_run(self, run):
            if run_id in self.running:
                self.reschedules[run_id] = {'_resume_after_handoff': True}
                # Command acceptance remains valid after a turn becomes paused
                # and before its old session writer has finished closing.
                self.store.event(run['session_id'], run_id, run_id + ':resume-queued:' + now(),
                    'continuation_queued', {'reason': '追加要求已接收，当前操作保存后自动继续',
                        'version': 1, 'generation': run['generation']})
                return {**run, 'continuation_queued': True}
            await prepare_resume(self, run)
            self.store.resume_harness_run(run_id, run['generation'])
            self.store.public_event(run_id, run['generation'], run_id + ':resumed:' + now(),
                'run_resumed', {'reason': '继续原目标，保留已接受的作业与成果'})
            self.schedule(run_id, None)
            return self.store.run(run_id)
        if run['status'] in {'running', 'waiting_job'}:
            self.schedule(run_id, None)
            return run
        raise Conflict('此运行当前不可继续')

    async def submit(self, session_id, content, request_id):
        session = self.store.session(session_id)
        if session_id in self.maintenance:
            raise Conflict('会话正在整理上下文，请稍后发送')
        if self.tasks.get(session_id)['manual']:
            raise ValueError('任务正在人工处理，请先返回智能审计')
        previous_request = self.store.rows('SELECT * FROM runs WHERE request_id=?', (request_id,))
        if previous_request:
            saved = json.loads(previous_request[0].get('driver_state') or '{}')
            if previous_request[0].get('driver') in DIRECT_DRIVERS or saved.get('request', {}).get('source_text') is not None:
                original = saved.get('request', {}).get('source_text')
                if original != content or previous_request[0]['session_id'] != session_id:
                    raise Conflict('请求身份与原目标不同')
                return previous_request[0]
            if previous_request[0]['fingerprint'] != digest([session_id, content]):
                raise Conflict('请求身份与原目标不同')
            return previous_request[0]
        previous_inbox = self.store.rows('SELECT i.*,r.session_id FROM inbox i JOIN runs r ON r.id=i.run_id WHERE i.id=?', (request_id,))
        if previous_inbox:
            if (previous_inbox[0]['session_id'], previous_inbox[0]['content']) != (session_id, content):
                raise Conflict('追加请求身份与原目标不同')
            return self.store.run(previous_inbox[0]['run_id'])
        active = self.store.rows("SELECT * FROM runs WHERE session_id=? AND status IN ('running','waiting_job','waiting_user','paused')",
                                 (session_id,))
        if not active:
            from .task_recovery import can_resume_run
            latest = self.store.rows('SELECT * FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 1', (session_id,))
            if latest and latest[0]['status'] in {'failed', 'partial', 'needs_verification'} and can_resume_run(self, latest[0]):
                active = latest
        if active:
            run = active[0]
            if run.get('driver') in DIRECT_DRIVERS:
                import re
                if re.search(r'改用|换用|改成|重新识别|重新提取|重做|只识别|仅提取', content):
                    raise Conflict('本次修改会改变已接受操作的参数。可继续原任务并保留原结果；如需修订，请先“停止后续步骤”，'
                                   '再在本任务调整资料并发送完整修订要求，旧尝试与原生回执会保留。')
                from workbench_platform.resources.reader_features import require_features
                require_features(self.store.root.parent, 'same-goal-continuation-v1')
            with self.store.transaction() as db:
                old = db.execute('SELECT * FROM inbox WHERE id=?', (request_id,)).fetchone()
                if old and (old['run_id'], old['content']) != (run['id'], content):
                    raise Conflict('追加请求与原参数不同')
                if len(self.store.rows('SELECT id FROM inbox WHERE run_id=?', (run['id'],))) >= 100:
                    raise Conflict('本轮追加目标数量已达到上限')
                db.execute('INSERT OR IGNORE INTO inbox(id,run_id,content,created) VALUES(?,?,?,?)',
                           (request_id, run['id'], content, now()))
            self.store.event(session_id, run['id'], request_id, 'message', {'role': 'user', 'content': content, 'queued': True})
            self.tasks.record_goal(session_id, content, request_id)
            if run['status'] in {'paused', 'failed', 'partial', 'needs_verification'}:
                await self.reconcile(run['id'])
            return self.store.run(run['id'])
        # Typed buttons/APIs retain the zero-model action path. All new free
        # text belongs to the one DSH loop, including compound requirements.
        # An unavailable controller still permits the established exact local
        # extraction commands; accepted old drivers keep their original path.
        if not self.agent_enabled or (not self.model_provider and not self.connection.view().get('available')):
            from workbench_platform.application_services.goal_routing import submit_local_goal
            local = await submit_local_goal(self, session_id, content, request_id)
            if local is not None:
                return local
        if not self.agent_enabled:
            raise ValueError('分析依赖尚未就绪；明确的图片或指定 PDF 页文字提取仍可直接发送')
        view = self.connection.view()
        if not self.model_provider and not view.get('available'):
            raise ValueError('该目标需要模型分析，请在主控设置中修复连接后继续；明确的图片或指定 PDF 页文字提取可直接发送')
        from .general import CONTRACT
        from workbench_platform.resources.reader_features import require_features
        route_state = {'scope_claims': self.store.scope_resolver({'session_id': session_id}),
                       'execution_contract': CONTRACT}
        require_features(self.store.root.parent, 'agent-general-v1')
        run, fresh = self.store.start_run(session_id, content, request_id, view.get('revision', 0),
            self.round_limit or view.get('round_limit'), self.tool_limit or view.get('tool_limit'),
            driver_state=route_state)
        if fresh:
            self.tasks.record_goal(session_id, content, request_id)
            self.schedule(run['id'], self.initial_state(run, content))
        return run

    async def continue_direct_followups(self, run_id):
        """Keep the accepted operation immutable; reason over its fixed output.

        The inbox and receipts are durable before the handover. A lost model
        connection leaves the same run resumable at the completed local step.
        """
        run = self.store.run(run_id)
        if run.get('driver') not in DIRECT_DRIVERS or run['status'] not in {'succeeded', 'partial'}:
            return
        additions = self.store.rows('SELECT * FROM inbox WHERE run_id=? ORDER BY created,id', (run_id,))
        saved = json.loads(run.get('driver_state') or '{}')
        if not additions and not saved.get('analysis_goal'):
            return
        from workbench_platform.resources.reader_features import require_features
        require_features(self.store.root.parent, 'same-goal-continuation-v1')
        view = self.connection.view()
        if self.graph is None or not self.agent_enabled or (not self.model_provider and not view.get('available')):
            self.finish_error(run_id, run['generation'], 'paused', 'followup_connection_required',
                '原文或表格已完成，可在任务成果中使用；补充分析已保存。请修复模型连接后点击继续任务。')
            return
        original = self.store.rows("SELECT payload FROM events WHERE run_id=? AND identity=? AND kind='message'",
                                   (run_id, run['request_id']))
        content = json.loads(original[0]['payload'])['content']
        state = self.initial_state(run, content)
        delivery = saved.get('receipts', {}).get('delivery') or {}
        state['business_started'] = True
        state['messages'].append({'id': run_id + ':fixed-local-output', 'role': 'user',
            'content': '[服务器已完成的原请求及固定成果；原文可直接读取，不能重复提交已完成识别。'
                       '接续全部用户补充要求，最终交付须同时保留原成果与新增分析。]\n' + canonical(delivery)})
        # Saving first is safe: the direct driver ignores this prepared graph
        # state after a cold stop. Only the atomic driver transition activates it.
        self.graph.save(state)
        from workbench_platform.application_services.direct_scope import resolve_harness_scope
        claims = resolve_harness_scope(self, self.store.session(run['session_id']))
        with self.store.transaction() as db:
            current = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if current['status'] == 'cancelled' or current['generation'] != run['generation']:
                return
            # Accepted direct parameters/receipts remain byte-for-byte intact;
            # additional claims describe only the future analysis phase.
            saved['scope_claims'] = claims
            saved['direct_origin_driver'] = run['driver']
            db.execute("UPDATE runs SET driver='harness',status='running',error=NULL,connection_revision=?,driver_state=? WHERE id=?",
                       (view.get('revision', 0), canonical(saved), run_id))
        self.store.public_event(run_id, run['generation'], run_id + ':followup-started', 'run_resumed',
            {'message': '原成果已完成，正在继续补充要求', 'request_ids': [item['id'] for item in additions]})
        self.schedule(run_id, state)

    def initial_state(self, run, content):
        session = self.store.session(run['session_id'])
        execution_contract = json.loads(run.get('driver_state') or '{}').get('execution_contract')
        previous = self.store.rows("SELECT payload FROM events WHERE session_id=? AND kind='delivery' AND run_id!=? ORDER BY seq DESC LIMIT 1", (session['id'], run['id']))
        inputs = [{'id': run['request_id'], 'role': 'user', 'content': content}]
        history = self.session_facts(run)
        if history:
            inputs.insert(0, {'id': run['id'] + ':session-facts', 'role': 'user',
                'content': '[此前服务器记录的会话目标和操作引用；状态是历史回执，继续前查询当前工具状态]\n' + canonical(history)})
        if previous:
            inputs.insert(0, {'id': run['id'] + ':prior-delivery', 'role': 'user',
                             'content': '[上一轮服务器记录的交付]\n' + previous[0]['payload']})
        inputs.insert(0, {'id': run['id'] + ':scope', 'role': 'user', 'content': '[服务器会话范围]\n' +
            canonical({'workspace_id': session['workspace_id'], 'selected_assets': session['selected_assets'],
                       'initial_refs': session['initial_refs']})})
        return {'run_id': run['id'], 'session_id': session['id'], 'workspace_id': session['workspace_id'],
            'generation': run['generation'], 'messages': inputs,
            'execution_contract': execution_contract,
            'step': run.get('rounds', 0) if json.loads(run.get('driver_state') or '{}').get('interpretation_request_id') else 0,
            'tool_count': 0,
            'business_started': bool(history.get('task', {}).get('delivery_requirements', {}).get('stages')),
            'visible': core_tools(self.registry, contract=execution_contract), 'protected_tools': [],
            'inbox_received': [], 'delivery': None, 'calls': [], 'results': [], 'correction_count': 0}

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
        facts['task']['authorized_reference_ids'] = task.get('authorized_reference_ids', [])
        from .delivery_policy import effective_policy
        facts['task']['delivery_policy'] = effective_policy(task)
        requirements = task.get('delivery_requirements', {})
        facts['task']['delivery_requirements'] = {
            'version': requirements.get('version', 1), 'stages': requirements.get('stages', []),
            'history_count': len(requirements.get('history', [])), 'history_tool': 'audit_get_task'}
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
        if self.graph and run_id in self.graph.processes:
            asyncio.create_task(self.graph.processes[run_id].call('cancel', {}, timeout=10))
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

    async def pause(self, run_id):
        run = self.store.run(run_id)
        if run['status'] == 'paused':
            return run
        if run['status'] not in {'running', 'waiting_job', 'waiting_user'}:
            raise Conflict('此运行当前不能暂停')
        if run.get('driver') in DIRECT_DRIVERS:
            with self.store.transaction() as db:
                changed = db.execute("UPDATE runs SET status='paused',generation=generation+1,updated=? "
                    "WHERE id=? AND generation=? AND status IN ('running','waiting_job','waiting_user')",
                    (now(), run_id, run['generation'])).rowcount
                if not changed:
                    raise Conflict('运行状态已改变')
            state = {'run_id': run_id, 'generation': run['generation'] + 1}
            self.activity(state, 'paused', reason='用户暂停；原生作业保留，继续时核对回执')
            return self.store.run(run_id)
        process = self.graph.processes.get(run_id)
        state = dict(process.state) if process else self.graph.load(run_id)
        with self.store.transaction() as db:
            changed = db.execute("UPDATE runs SET status='paused',generation=generation+1,updated=? "
                "WHERE id=? AND generation=? AND status IN ('running','waiting_job','waiting_user')",
                (now(), run_id, run['generation'])).rowcount
            if not changed:
                raise Conflict('运行状态已改变')
            db.execute("UPDATE model_requests SET state='cancelled',response=? WHERE run_id=? AND state='sent'",
                (canonical({'reason': 'user_requested_pause', 'response_may_be_unknown': True}), run_id))
        state.update(generation=run['generation'] + 1, continuation_authorized=True)
        state.pop('pending_model_request', None)
        self.graph.save(state)
        self.activity(state, 'paused', reason='用户暂停；原生作业保留，继续时核对回执')
        if process:
            try:
                await process.call('pause', {}, timeout=10)
            except (ProviderFault, TimeoutError, ConnectionError):
                pass
        return self.store.run(run_id)

    async def compact(self, session_id):
        runs = self.store.rows("SELECT id FROM runs WHERE session_id=? AND status IN ('running','waiting_job','waiting_user','paused') "
                               'ORDER BY created DESC LIMIT 1', (session_id,))
        if runs and runs[0]['id'] in self.running:
            run_id = runs[0]['id']
            process = self.graph.processes.get(run_id)
            state = process.state if process else self.graph.load(run_id)
            state['compact_requested'] = True
            self.graph.save(state)
            self.store.event(session_id, run_id, session_id + ':compact-request:' + uuid.uuid4().hex,
                             'context_compaction', {'phase': 'queued', 'message': '已安排在下一次模型请求前整理上下文'})
            return {'queued': True, 'run_id': run_id}
        if session_id in self.maintenance:
            raise Conflict('此会话已经在整理上下文')
        idle = self.store.rows("SELECT * FROM runs WHERE session_id=? AND status!='cancelled' ORDER BY created DESC LIMIT 1", (session_id,))
        if not idle:
            raise Conflict('此会话还没有可整理的任务历史')
        run = idle[0]
        state = self.graph.load(run['id'])
        if not state:
            message = self.store.rows('SELECT payload FROM events WHERE identity=?', (run['request_id'],))
            state = self.initial_state(run, json.loads(message[0]['payload'])['content'])
        self.maintenance[session_id] = asyncio.current_task()
        from .harness import HarnessProcess
        process, reused = None, False
        try:
            previous_writer = self.running.get(run['id'])
            if previous_writer:
                await asyncio.shield(previous_writer)
            process = self.graph.idle_processes.pop(session_id, None)
            reused = bool(process and process.process and process.process.returncode is None)
            if process and not reused:
                await process.close()
            process = process if reused else HarnessProcess(self.graph, state)
            process.state = state
            if not reused:
                await process.start()
            result = await process.call('compact', {})
            return {'queued': False, 'run_id': run['id'], **result}
        finally:
            try:
                if reused and not self.closing and process.process.returncode is None:
                    process.resources = await process.call('idle', {}, timeout=10)
                    self.graph.idle_processes[session_id] = process
                elif process:
                    await process.close()
            except (Exception, asyncio.CancelledError):
                if process:
                    await process.close()
                raise
            finally:
                self.maintenance.pop(session_id, None)

    async def reply(self, decision_id, content, revision, request_id):
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
                db.execute('UPDATE runs SET round_limit=CASE WHEN round_limit=0 THEN 0 ELSE round_limit+20 END,'
                           'tool_limit=CASE WHEN tool_limit=0 THEN 0 ELSE tool_limit+60 END WHERE id=?', (decision['run_id'],))
        self.store.event(run['session_id'], run['id'], request_id, 'message', {'role': 'user', 'content': content, 'decision_id': decision_id})
        self.store.event(run['session_id'], run['id'], decision_id + ':answered', 'question_answered',
                         {'decision_id': decision_id, 'content': content})
        self.schedule(run['id'], None)
        return self.store.run(run['id'])

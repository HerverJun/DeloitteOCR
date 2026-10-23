"""Durable Clear supervision of an official DeepSeek Harness child process."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sqlite3
from types import SimpleNamespace

from workbench_platform.supervisor.processes import require_f_path
from .providers import ProviderFault
from .store import canonical, digest, now, Conflict, RunSuperseded

VERSION = '0.2.1-alpha.4'


def installation():
    root = require_f_path(Path(__file__).resolve().parents[3] / 'agent_harness')
    local = root / 'runtime-local.json'
    settings = json.loads(local.read_text(encoding='utf-8')) if local.is_file() else {}
    node = require_f_path(Path(os.environ.get('WORKBENCH_HARNESS_NODE') or settings.get('node') or root / 'node' / 'node.exe'))
    if not node.is_file() or not (root / 'host.mjs').is_file():
        raise FileNotFoundError('Harness Node 24.22.0 runtime is not installed')
    package = root / 'node_modules' / '@deepseek-ai' / 'dsh-agent-loop' / 'package.json'
    if not package.is_file() or json.loads(package.read_text(encoding='utf-8'))['version'] != VERSION:
        raise ValueError('Harness dependency closure is missing or has a different version')
    return root, node


class HarnessProcess:
    def __init__(self, engine, state):
        self.engine, self.runtime, self.state = engine, engine.runtime, state
        self.pending, self.handlers = {}, set()
        self.serial = 0
        self.process = self.reader = None
        self.log = None
        self.close_task = None

    async def start(self):
        source, node = installation()
        runtime_root = require_f_path(self.runtime.store.root / 'harness')
        for part in ('sessions', 'temp', 'cache', 'cache/appdata', 'cache/localappdata', 'logs'):
            (runtime_root / part).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(TEMP=str(runtime_root / 'temp'), TMP=str(runtime_root / 'temp'), TMPDIR=str(runtime_root / 'temp'),
                   APPDATA=str(runtime_root / 'cache/appdata'), LOCALAPPDATA=str(runtime_root / 'cache/localappdata'),
                   XDG_CACHE_HOME=str(runtime_root / 'cache'), DSH_HOME=str(runtime_root),
                   DSH_RESOURCE_CACHE=str(runtime_root / 'cache'), DSH_PRIMARY_RUNTIME='')
        from .general import enabled as general_enabled
        cwd = self.runtime.store.root
        if general_enabled(self.state):
            from .native_tools import environment as native_environment
            cwd = native_environment(self.runtime, self.state, env)
            self.engine.save(self.state)
        self.log = (runtime_root / 'logs' / (self.state['run_id'] + '.log')).open('ab')
        self.process = await asyncio.create_subprocess_exec(str(node), str(source / 'host.mjs'),
            cwd=str(source), env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.log, limit=16 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.reader = asyncio.create_task(self.read(), name='harness-rpc-' + self.state['run_id'])
        config = self.runtime.harness_config(self.state)
        seed = self.engine.seed(self.state)
        from .integrations import resolved
        integrations = resolved(self.runtime) if general_enabled(self.state) else {}
        self.state['integration_revision'] = integrations.get('revision', 0)
        result = await self.call('initialize', {'version': 1, 'root': str(runtime_root / 'sessions'),
            'cwd': str(cwd), 'session_id': self.state['session_id'],
            'execution_contract': self.state.get('execution_contract'),
            'integrations': integrations,
            'skill_dirs': [str(path) for path in [*self.runtime.skills.roots,
                *self.runtime.skills.workspace_roots.get(self.state['workspace_id'], ())]],
            'model': config['model'], 'protocol': config['protocol'], 'profile': config['model_profile'],
            'supports_images': config.get('supports_images', False),
            'system': self.runtime.harness_prompt(self.state)[0], 'history': seed}, timeout=45)
        # Seed and repair events are persisted before normal callbacks attach.
        # Refill the immutable SQL projection, also healing an ACK crash window.
        after = 0
        while True:
            page = await self.call('history_events', {'after': after, 'limit': 500}, timeout=45)
            for event in page['items']:
                self.engine.record_event(self.state, event)
            if not page['items'] or page['next_cursor'] >= page['total']:
                break
            after = page['next_cursor']
        with self.runtime.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO harness_sessions VALUES(?,?)', (self.state['session_id'], now()))
        from workbench_platform.resources.reader_features import require_features
        require_features(self.runtime.store.root.parent, 'agent-harness-v1')
        self.runtime.store.public_event(self.state['run_id'], self.state['generation'],
            self.state['run_id'] + ':engine:' + str(self.state['generation']), 'engine_ready', result)

    async def call(self, method, params, timeout=None):
        self.serial += 1
        identifier = 'p' + str(self.serial)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.write({'jsonrpc': '2.0', 'version': 1, 'id': identifier, 'method': method, 'params': params})
            return await asyncio.wait_for(asyncio.shield(future), timeout) if timeout else await future
        except asyncio.TimeoutError:
            raise ProviderFault('harness_exited', 'Harness 控制连接超时；原目标和已提交作业保留，可继续') from None
        finally:
            self.pending.pop(identifier, None)

    async def write(self, value):
        if not self.process or self.process.returncode is not None:
            raise ProviderFault('harness_exited', 'Harness 进程已退出；已保存的进度保留')
        try:
            self.process.stdin.write((canonical(value) + '\n').encode('utf-8'))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionError):
            raise ProviderFault('harness_exited', 'Harness 连接中断；原目标和已提交作业保留，可继续') from None

    async def read(self):
        error = ProviderFault('harness_exited', 'Harness 连接中断；恢复前会核对已提交操作')
        try:
            while raw := await self.process.stdout.readline():
                frame = json.loads(raw)
                if frame.get('version') != 1:
                    raise ValueError('Harness RPC version mismatch')
                if frame.get('method'):
                    task = asyncio.create_task(self.answer(frame))
                    self.handlers.add(task)
                    task.add_done_callback(self.handlers.discard)
                else:
                    future = self.pending.get(frame.get('id'))
                    if future and not future.done():
                        if frame.get('error'):
                            info = frame['error']
                            future.set_exception(ProviderFault(str(info.get('code', 'harness_failed')), str(info['message'])))
                        else:
                            future.set_result(frame.get('result'))
        except asyncio.CancelledError:
            return
        except Exception as caught:
            error = ProviderFault('harness_protocol', 'Harness 协议读取失败：' + type(caught).__name__)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)

    async def answer(self, frame):
        try:
            value = await self.engine.callback(self.state, frame['method'], frame.get('params', {}))
            response = {'result': value}
            if frame['method'] == 'clear/model' and self.state.pop('last_bridge_error', None) is not None:
                self.engine.save(self.state)
        except asyncio.CancelledError:
            return
        except Exception as error:
            code = getattr(error, 'code', 'clear_runtime_failed')
            if isinstance(error, (Conflict, RunSuperseded)):
                code = 'execution_conflict'
            response = {'error': {'code': code, 'message': str(error)}}
            if getattr(error, 'response_diagnostics', None):
                response['error']['response_diagnostics'] = error.response_diagnostics
            compacting = frame['method'] == 'clear/model' and frame.get('params', {}).get('purpose') == 'compaction'
            self.state['last_bridge_error'] = {'code': 'context_compaction_failed' if compacting else code,
                'message': ('上下文整理未完成：' if compacting else '') + str(error),
                'context_metrics': getattr(error, 'context_metrics', None),
                'response_diagnostics': getattr(error, 'response_diagnostics', None)}
            try:
                self.engine.save(self.state)
            except RunSuperseded:
                pass
        try:
            await self.write({'jsonrpc': '2.0', 'version': 1, 'id': frame['id'], **response})
        except (BrokenPipeError, ConnectionError, ProviderFault):
            pass

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self.close_task)
        except asyncio.CancelledError:
            await self.close_task
            raise

    async def _close(self):
        for task in list(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*self.handlers, return_exceptions=True)
        try:
            if self.process and self.process.returncode is None:
                try:
                    await self.call('shutdown', {}, timeout=5)
                except Exception:
                    if self.process.returncode is None:
                        self.process.kill()
                await self.process.wait()
        finally:
            if self.reader:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            remaining = list(self.handlers)
            for task in remaining:
                task.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
            if self.log:
                self.log.close()


class HarnessEngine:
    """Operational snapshots are projections; model history is Harness-owned."""
    def __init__(self, runtime):
        installation()
        self.runtime = runtime
        self.processes = {}
        self.idle_processes = {}
        with runtime.store.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS harness_runs(run_id TEXT PRIMARY KEY,state TEXT NOT NULL,updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS harness_events(session_id TEXT NOT NULL,seq INTEGER NOT NULL,
                    run_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(session_id,seq));
                CREATE TABLE IF NOT EXISTS harness_model_calls(request_id TEXT PRIMARY KEY,purpose TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS harness_model_payloads(request_id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,sha256 TEXT NOT NULL,metrics TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS harness_migrations(session_id TEXT PRIMARY KEY,history TEXT NOT NULL,
                    manifest TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS harness_sessions(session_id TEXT PRIMARY KEY,created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS harness_child_sessions(session_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                    model_calls INTEGER NOT NULL DEFAULT 0,updated TEXT NOT NULL);
            ''')
        # This reconstructible projection has one process-local writer. Keep
        # its connection open instead of reopening SQLite for every log event.
        self.event_db = sqlite3.connect(runtime.store.path, timeout=30)

    def save(self, state):
        with self.runtime.store.transaction() as db:
            run = db.execute('SELECT generation,status FROM runs WHERE id=?', (state['run_id'],)).fetchone()
            if not run or run['generation'] != state['generation'] or run['status'] == 'cancelled':
                raise RunSuperseded('运行已停止')
            db.execute('INSERT OR REPLACE INTO harness_runs VALUES(?,?,?)', (state['run_id'], canonical(state), now()))

    def load(self, run_id):
        rows = self.runtime.store.rows('SELECT state FROM harness_runs WHERE run_id=?', (run_id,))
        return json.loads(rows[0]['state']) if rows else {}

    def seed(self, state):
        rows = self.runtime.store.rows('SELECT history FROM harness_migrations WHERE session_id=?', (state['session_id'],))
        history = json.loads(rows[0]['history']) if rows else []
        # Fresh input is admitted by Harness, after the imported history.
        added = [m for m in state.get('messages', []) if m.get('id') != state.get('request_id')]
        known = {m.get('id') for m in history if m.get('id')}
        return [*history, *(m for m in added if m.get('id') not in known)]

    def record_event(self, state, event, session_id=None):
        session_id = session_id or state['session_id']
        stamp = digest(event)
        with self.event_db as db:
            existing = db.execute('SELECT sha256 FROM harness_events WHERE session_id=? AND seq=?',
                                  (session_id, event['seq'])).fetchone()
            if existing and existing[0] != stamp:
                raise Conflict('Harness 历史序号绑定的内容不同')
            db.execute('INSERT OR IGNORE INTO harness_events VALUES(?,?,?,?,?)',
                       (session_id, event['seq'], state['run_id'], canonical(event), stamp))

    async def aget_state(self, options):
        state = self.load(options['configurable']['thread_id'])
        return SimpleNamespace(values=state, next=('harness',) if state else (), tasks=())

    async def aupdate_state(self, options, values, **_):
        state = self.load(options['configurable']['thread_id'])
        state.update(values)
        self.save(state)

    async def ainvoke(self, input, options, **_):
        identifier = options['configurable']['thread_id']
        state = self.load(identifier)
        if isinstance(input, dict) and 'run_id' in input and not state:
            state = dict(input)
        if not state:
            run = self.runtime.store.run(identifier)
            message = self.runtime.store.rows('SELECT payload FROM events WHERE identity=?', (run['request_id'],))
            state = self.runtime.initial_state(run, json.loads(message[0]['payload'])['content'])
        run = self.runtime.current(state)
        state.setdefault('request_id', run['request_id'])
        state.setdefault('continuation_authorized', True)
        state.setdefault('started_active_seconds', 0.0)
        self.runtime.harness_config(state)
        self.save(state)
        # A stop fences the old run before its Windows writer lease closes.
        # Admit a new goal only after that same session's prior child releases
        # persistence ownership; it must not race into SessionBusyError.
        while any(other.state['session_id'] == state['session_id'] for other_id, other in self.processes.items()
                  if other_id != identifier):
            self.runtime.current(state)
            await asyncio.sleep(.1)
        process = self.idle_processes.pop(state['session_id'], None)
        reused = bool(process and process.process and process.process.returncode is None)
        if process and not reused:
            await process.close()
        process = process if reused else HarnessProcess(self, state)
        if reused:
            from .integrations import saved as saved_integrations
            if process.state.get('integration_revision', 0) != saved_integrations(self.runtime)['revision']:
                resources = await process.call('idle', {}, timeout=10)
                if not any(job['status'] in {'running', 'stopping'} for job in resources.get('jobs', [])):
                    await process.close()
                    process = HarnessProcess(self, state)
                    reused = False
        if reused:
            from .native_tools import workspace
            for key in ('python_executable', 'python_packages', 'execution_boundary'):
                if key in process.state:
                    state[key] = process.state[key]
            workspace(self.runtime, state)
        process.state = state
        self.processes[identifier] = process
        try:
            unknown = self.runtime.store.rows("SELECT id FROM model_requests WHERE run_id=? AND state='sent'", (identifier,))
            if unknown:
                raise ProviderFault('response_unknown', '此前模型请求回执未知；未重新发送，已有业务成果保留')
            if reused:
                config = self.runtime.harness_config(state)
                await process.call('activate', {'session_id': state['session_id'], 'model': config['model'],
                    'protocol': config['protocol'], 'profile': config['model_profile'],
                    'supports_images': config.get('supports_images', False),
                    'system': self.runtime.harness_prompt(state)[0], 'tools': []})
            else:
                await process.start()
            fresh = not state.get('entered')
            prompt = self.runtime.store.rows('SELECT payload FROM events WHERE identity=?', (run['request_id'],))
            content = json.loads(prompt[0]['payload'])['content'] if fresh else '[恢复此前已授权任务；先核对服务器回执，不重复已完成操作，继续未完成事项。]'
            state['entered'] = True
            state['resume_attempts'] = state.get('resume_attempts', 0) + (0 if fresh else 1)
            self.save(state)
            while True:
                try:
                    await process.call('prompt', {'id': run['request_id'] if fresh else identifier + ':continue:' + str(
                        state.get('continuations', 0)) + ':attempt:' + str(state['resume_attempts']), 'content': content})
                except ProviderFault as error:
                    details = state.get('last_bridge_error')
                    if details:
                        fault = ProviderFault(details['code'], details['message'], context_metrics=details.get('context_metrics'))
                        fault.response_diagnostics = details.get('response_diagnostics')
                        raise fault from error
                    raise
                self.runtime.current(state)
                if await self.runtime.harness_finalize(state):
                    break
                state['continuations'] = state.get('continuations', 0) + 1
                progress = digest(self.runtime.store.rows('SELECT id,state,result FROM operations WHERE run_id=? ORDER BY id', (identifier,)))
                state['idle_without_progress'] = state.get('idle_without_progress', 0) + 1 if progress == state.get('last_progress') else 0
                state['last_progress'] = progress
                if state['idle_without_progress'] >= 3:
                    self.runtime.store.status(identifier, state['generation'], 'paused', '连续三轮没有有效进展；已有进度保留')
                    self.runtime.activity(state, 'paused', reason='stalled')
                    break
                review = state.pop('completion_review_notice', None)
                content = ('[正式成果的固定版本复核已收到真实用户答复。确认时加载并使用适用的复核应用工具，'
                    '再生成核准后的成果；不确认时保留未复核标识并说明当前限制。不得代替用户确认。]\n' + canonical(review)) if review else (
                    '[继续原任务；已保存成果可直接读取下载，只补齐明确缺失项。复用原操作回执，勿重做成功项。完成后自然回复，确实缺少用户信息才提问。]\n' + canonical(state.get('remaining_delivery_work', {}))
                    if state.get('execution_contract') == 'clear-general-agent-v1' else
                    '[任务尚未通过交付检查。根据原目标、当前计划及真实回执继续执行；完成业务任务需调用 delivery_publish 核验交付，需要输入时调用 ask_user。]')
                fresh = False
                self.save(state)
            return state
        finally:
            try:
                self.save(state)
            except RunSuperseded:
                pass
            finally:
                from .general import enabled as general_enabled
                keep = False
                if (general_enabled(state) and not self.runtime.closing and process.process and process.process.returncode is None
                        and self.runtime.store.run(identifier)['status'] in {'answered', 'succeeded', 'paused', 'partial'}):
                    try:
                        resources = await process.call('idle', {}, timeout=10)
                        # Official jobs belong to their live Agent. Keep that
                        # session's host until it can collect its job records.
                        process.resources = resources
                        keep = True
                    except (Exception, asyncio.CancelledError):
                        pass
                if keep:
                    self.idle_processes[state['session_id']] = process
                    # Retain a few warm conversations without cancelling live
                    # background work to satisfy an arbitrary cache limit.
                    for session_id, host in list(self.idle_processes.items()):
                        if len(self.idle_processes) <= 8:
                            break
                        if session_id == state['session_id'] or any(job['status'] in {'running', 'stopping'}
                                for job in getattr(host, 'resources', {}).get('jobs', [])):
                            continue
                        self.idle_processes.pop(session_id)
                        await host.close()
                else:
                    await process.close()
                self.processes.pop(identifier, None)

    async def close(self):
        hosts = list({id(host): host for host in [*self.idle_processes.values(), *self.processes.values()]}.values())
        self.idle_processes.clear()
        self.processes.clear()
        await asyncio.gather(*(host.close() for host in hosts), return_exceptions=True)

    async def callback(self, state, method, params):
        if method == 'clear/event':
            event = params['event']
            agent_session = params.get('session_id', state['session_id'])
            self.record_event(state, event, agent_session)
            if agent_session != state['session_id']:
                self.runtime.store.public_event(state['run_id'], state['generation'],
                    'child:' + agent_session + ':' + str(event['seq']), 'subagent_activity', {
                        'agent_session_id': agent_session, 'event': event['type'], 'seq': event['seq']})
                return {'ack': event['seq']}
            if event['type'].startswith('compaction/'):
                self.runtime.store.public_event(state['run_id'], state['generation'],
                    state['session_id'] + ':harness:' + str(event['seq']), 'context_compaction',
                    {'phase': 'error' if event['data'].get('error') else event['type'].split('/')[1], 'seq': event['seq'],
                     'error': event['data'].get('error'), 'compaction_id': event['data'].get('compactionId')})
                if event['type'] == 'compaction/end' and not event['data'].get('error'):
                    state['compactions'] = self.runtime.store.rows("SELECT COUNT(*) AS n FROM harness_events WHERE session_id=? "
                        "AND json_extract(payload,'$.type')='compaction/end' AND json_extract(payload,'$.data.error') IS NULL",
                        (state['session_id'],))[0]['n']
                    self.save(state)
            if event['type'] == 'assistant/message':
                replay = event['data']['message'].get('source', {}).get('replayState', {}).get('response') or {}
                if replay.get('request_id') == state.get('pending_model_request'):
                    state.pop('pending_model_request', None)
                    self.save(state)
            if event['type'] == 'user/message':
                identifier = event['data']['id']
                if self.runtime.store.rows('SELECT id FROM inbox WHERE id=? AND run_id=?', (identifier, state['run_id'])):
                    state['inbox_received'] = list(dict.fromkeys([*state.get('inbox_received', []), identifier]))
                    with self.runtime.store.transaction() as db:
                        db.execute('UPDATE inbox SET consumed=1 WHERE id=? AND run_id=?', (identifier, state['run_id']))
                    self.save(state)
            return {'ack': event['seq']}
        if method == 'clear/prepare':
            return await self.runtime.harness_prepare(state, params)
        if method == 'clear/model':
            return await self.runtime.harness_model(state, params)
        if method == 'clear/tool':
            return await self.runtime.harness_tool(state, params)
        if method == 'clear/recovery':
            return await self.runtime.harness_recovery(state, params)
        if method in {'clear/native-begin', 'clear/native-result'}:
            from . import native_tools
            handler = native_tools.begin if method == 'clear/native-begin' else native_tools.finish
            return handler(self.runtime, state, params)
        raise ValueError('Unsupported private Harness method')

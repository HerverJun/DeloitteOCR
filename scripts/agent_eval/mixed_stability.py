"""Frozen-source real GLM / loopback visual / Agent / two-Edge load audit.

Run a short smoke before a 14400-second qualification run. All inputs and
credentials are synthetic; the existing portable model assets are read-only.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TOKEN = 'isolated-mixed-stability-local-token'


def utc():
    return datetime.now(timezone.utc).isoformat()


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def route_label(path):
    return re.sub(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])', '{id}', path)


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    temporary.replace(path)


def hardware_identity():
    import psutil
    details = {'platform': platform.platform(), 'logical_cpu_count': psutil.cpu_count(),
               'physical_cpu_count': psutil.cpu_count(logical=False),
               'ram_bytes': psutil.virtual_memory().total, 'gpu': None,
               'python': sys.version, 'packages': {}}
    for package in ['fastapi', 'uvicorn', 'httpx', 'psutil', 'Pillow', 'langgraph', 'langgraph-checkpoint-sqlite']:
        try:
            details['packages'][package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            details['packages'][package] = None
    node = shutil.which('node')
    if node:
        try:
            details['node'] = subprocess.run([node, '--version'], capture_output=True, text=True,
                                             timeout=10, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            details['node_error'] = f'{type(error).__name__}: {error}'
    executable = shutil.which('nvidia-smi')
    if executable:
        try:
            result = subprocess.run([executable, '--query-gpu=name,uuid,memory.total,driver_version',
                                     '--format=csv,noheader'], capture_output=True, text=True, timeout=10, check=True)
            details['gpu'] = result.stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError) as error:
            details['gpu_error'] = f'{type(error).__name__}: {error}'
    return details


class Metrics:
    """Exact millisecond histogram; bounded by duration, not request count."""
    def __init__(self):
        self.histograms = {}
        self.lock = threading.Lock()

    def add(self, name, seconds):
        with self.lock:
            self.histograms.setdefault(name, Counter())[math.ceil(seconds * 1000)] += 1

    def report(self):
        result = {}
        with self.lock:
            for name, hist in self.histograms.items():
                count = sum(hist.values())
                def percentile(q):
                    accumulated = 0
                    for value, n in sorted(hist.items()):
                        accumulated += n
                        if accumulated >= math.ceil(count * q):
                            return value
                result[name] = {'samples': count, 'median_ms': percentile(.5), 'p95_ms': percentile(.95),
                                'max_ms': max(hist), 'mean_ms': sum(k*v for k,v in hist.items()) / count}
        return result


def freeze(args):
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / 'source'
    for src, dst in [('src', 'src'), ('config', 'config'), ('frontend/dist', 'web')]:
        shutil.copytree(ROOT / src, source / dst,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'development-machine.json'))
    shutil.copytree(args.bundle / 'config', source / 'asset-config')
    for name in ['mixed_stability.py', 'mixed_stability.mjs', 'mixed_service_bridge.py', 'assess_mixed_stability.py']:
        (source / 'scripts/agent_eval').mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'scripts/agent_eval' / name, source / 'scripts/agent_eval' / name)
    (source / 'tests').mkdir()
    for name in ['test_agent_connection.py', 'test_external_review.py']:
        shutil.copy2(ROOT / 'tests' / name, source / 'tests' / name)
    manifest = {p.relative_to(source).as_posix(): checksum(p) for p in source.rglob('*') if p.is_file()}
    write_json(args.output / 'source-manifest.json', manifest)
    # Only these large immutable asset directories are shared with candidate-01.
    # Product source, policy and web files above are independent copies.
    for name in ['runtimes', 'models', 'fonts', 'tools']:
        subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(source / name), str(args.bundle / name)],
                       check=True, stdout=subprocess.DEVNULL)
    write_json(args.output / 'asset-links.json', {n: str(args.bundle / n) for n in ['runtimes', 'models', 'fonts', 'tools']})
    command = [sys.executable, '-X', 'utf8', '-B', str(source / 'scripts/agent_eval/mixed_stability.py'),
               '--worker', '--bundle', str(args.bundle), '--output', str(args.output),
               '--seconds', str(args.seconds), '--repeats', str(args.repeats), '--pages', str(args.pages),
               '--cycle-seconds', str(args.cycle_seconds), '--playwright', str(args.playwright),
               '--client-transport', args.client_transport]
    if args.service_process:
        command.append('--service-process')
    if args.trace_api_spans:
        command.append('--trace-api-spans')
    if args.paired_transport_probe:
        command.append('--paired-transport-probe')
    if args.trace_db_spans:
        command.append('--trace-db-spans')
    write_json(args.output / 'process.json', {'launcher_pid': os.getpid(), 'command': command, 'created_utc': utc()})
    return subprocess.call(command)


def forbid_remote_network():
    original = socket.getaddrinfo
    def local_only(host, *args, **kwargs):
        if host not in {'127.0.0.1', 'localhost', '::1', None}:
            raise RuntimeError('Audit blocks non-loopback name resolution: ' + str(host))
        return original(host, *args, **kwargs)
    socket.getaddrinfo = local_only
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'


class Audit:
    def __init__(self, args):
        self.args, self.out, self.source = args, args.output, args.output / 'source'
        self.metrics = Metrics()
        self.phase, self.app, self.base = 'setup', None, None
        self.stop, self.ui_enabled = False, False
        self.ui_process = None
        self.receipt_lock = threading.RLock()
        self.server_task, self.provider, self.open_patch = None, None, None
        self.service_process, self.bridge, self.service_stdout = None, None, None
        self.child_metrics = {}
        self.http_client = None
        self.span_lock = threading.Lock()
        self.ui_counts = Counter()
        self.run_counts = Counter()
        self.resource_samples = Counter()
        self.requests = Counter()
        self.worker_pids = set()
        self.report = {'status': 'running', 'started_utc': utc(), 'worker_pid': os.getpid(),
            'requested_mixed_seconds': args.seconds, 'repeats': args.repeats, 'pages_per_batch': args.pages,
            'source_sha256': checksum(self.out / 'source-manifest.json'),
            'asset_bundle': str(args.bundle), 'asset_manifest_sha256': checksum(args.bundle / 'manifest.json'),
            'hardware': hardware_identity(),
            'controller': 'explicit synthetic model_override; no controller HTTP',
            'client_transport': args.client_transport,
            'service_mode': 'independent_process' if args.service_process else 'in_process',
            'api_span_trace': 'api-spans.jsonl' if args.trace_api_spans else None,
            'visual': 'real local HTTP ProviderFixture, no cloud', 'private_or_sealed_inputs': False,
            'comparisons': [], 'errors': [], 'ui_clients': {},
            'limits': ['Single development GPU machine, not target-machine qualification.',
                       'Synthetic controller does not qualify provider behavior or semantic summary quality.',
                       'Latency histogram rounds up to 1ms; UI progress latency includes browser polling delay.',
                       'Baseline and enabled phases use the same process/project/images. The production queue unloads at idle; each measured batch therefore includes a first untimed warmup image followed by measured images in that worker.',
                       'Worker assets/config resolve from the unchanged physical portable bundle; a copy of asset-config is included in the source identity.']}
        prefix = 'service-' if args.service_child else ''
        self.resources = (self.out / (prefix + 'resources.jsonl')).open('w', encoding='utf-8')
        self.trace = (self.out / (prefix + 'activity.jsonl')).open('w', encoding='utf-8')
        self.api_spans = (self.out / (prefix + 'api-spans.jsonl')).open('w', encoding='utf-8') if args.trace_api_spans else None
        self.db_spans = (self.out / (prefix + 'db-spans.jsonl')).open('w', encoding='utf-8') if args.trace_db_spans else None
        self.db_context = ContextVar('audit_db_request', default=None)

    def log(self, kind, **value):
        self.trace.write(json.dumps({'utc': utc(), 'phase': self.phase, 'kind': kind, **value}, ensure_ascii=False) + '\n')
        self.trace.flush()

    def record_api_span(self, request_id, event, route, **value):
        if self.api_spans is None:
            return
        row = {'request_id': request_id, 'event': event, 'route': route,
               'phase': self.phase, 'perf_ns': time.perf_counter_ns(), 'utc': utc(), **value}
        with self.span_lock:
            self.api_spans.write(json.dumps(row, ensure_ascii=False) + '\n')
            self.api_spans.flush()

    def record_db_span(self, event, **value):
        current = self.db_context.get()
        if current is None or self.db_spans is None:
            return
        request_id, route = current
        row = {'request_id': request_id, 'route': route, 'phase': self.phase,
               'event': event, 'perf_ns': time.perf_counter_ns(),
               'thread_id': threading.get_ident(), **value}
        with self.span_lock:
            self.db_spans.write(json.dumps(row, ensure_ascii=False) + '\n')

    def install_db_trace(self, store):
        """Audit-only wrappers; the frozen product source remains untouched."""
        original_lock = store.lock
        audit = self

        class TimedLock:
            def __enter__(self):
                started = time.perf_counter_ns()
                result = original_lock.__enter__()
                audit.record_db_span('lock_acquired', wait_ns=time.perf_counter_ns() - started)
                return result

            def __exit__(self, *args):
                audit.record_db_span('lock_release')
                return original_lock.__exit__(*args)

            def __getattr__(self, name):
                return getattr(original_lock, name)

        class TimedDb:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, params=()):
                started = time.perf_counter_ns()
                try:
                    return self.db.execute(sql, params)
                finally:
                    audit.record_db_span('sql_execute', sql=str(sql).strip().splitlines()[0][:120],
                                         elapsed_ns=time.perf_counter_ns() - started)

            def executemany(self, sql, params):
                started = time.perf_counter_ns()
                try:
                    return self.db.executemany(sql, params)
                finally:
                    audit.record_db_span('sql_executemany', sql=str(sql).strip().splitlines()[0][:120],
                                         elapsed_ns=time.perf_counter_ns() - started)

            def __getattr__(self, name):
                return getattr(self.db, name)

        original_transaction = store.transaction
        original_rows = store.rows

        @contextmanager
        def traced_transaction():
            audit.record_db_span('transaction_start')
            try:
                with original_transaction() as db:
                    audit.record_db_span('transaction_ready')
                    yield TimedDb(db) if audit.db_context.get() else db
            finally:
                audit.record_db_span('transaction_end')

        def traced_rows(sql, params=()):
            started = time.perf_counter_ns()
            try:
                return original_rows(sql, params)
            finally:
                audit.record_db_span('rows', sql=str(sql).strip().splitlines()[0][:120],
                                     elapsed_ns=time.perf_counter_ns() - started)

        store.lock = TimedLock()
        store.transaction = traced_transaction
        store.rows = traced_rows

    def install_component_trace(self, app):
        services = app.state.application_services
        components = [('registry.engines', services.registry, 'engines'),
                      ('queue.status', app.state.queue, 'status'),
                      ('fusion_queue.status', app.state.fusion_queue, 'status'),
                      ('external_queue.status', app.state.external_queue, 'status'),
                      ('documents.status', services.documents, 'status'),
                      ('maintenance.disk_status', services.maintenance, 'disk_status')]
        for label, obj, name in components:
            original = getattr(obj, name)

            def traced(*args, _original=original, _label=label, **kwargs):
                started = time.perf_counter_ns()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self.record_db_span('component', component=_label,
                                        elapsed_ns=time.perf_counter_ns() - started)

            setattr(obj, name, traced)

    def install_route_trace(self, app):
        for route in app.routes:
            if getattr(route, 'path', None) not in {'/api/state', '/api/agent/sessions/{session_id}/messages'}:
                continue
            original = route.dependant.call
            if asyncio.iscoroutinefunction(original):
                async def traced_async(*args, _original=original, **kwargs):
                    self.record_db_span('endpoint_start')
                    try:
                        return await _original(*args, **kwargs)
                    finally:
                        self.record_db_span('endpoint_end')
                route.dependant.call = traced_async
            else:
                def traced_sync(*args, _original=original, **kwargs):
                    self.record_db_span('endpoint_start')
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        self.record_db_span('endpoint_end')
                route.dependant.call = traced_sync

    def save(self):
        with self.receipt_lock:
            if self.bridge and self.app:
                state = self.bridge.call('state')
                self.child_metrics = state['metrics']
                self.ui_counts = Counter(state['ui_counts'])
                self.report['ui_clients'] = state['ui_clients']
            metrics = self.metrics.report()
            # Keep both observations; a child key must never silently replace
            # a parent histogram (e.g. during warmup/setup).
            for name, histogram in self.child_metrics.items():
                key = name if name not in metrics else 'service:' + name
                if key in metrics:
                    raise ValueError('Metric namespace collision: ' + key)
                metrics[key] = histogram
            self.report.update(phase=self.phase, metrics=metrics, run_counts=dict(self.run_counts),
                               ui_counts=dict(self.ui_counts), updated_utc=utc())
            if self.app:
                store = self.app.state.store
                database_bytes = {}
                for path in store.root.glob('*.sqlite3*'):
                    try:
                        database_bytes[path.name] = path.stat().st_size
                    except OSError:
                        continue
                self.report['database_bytes'] = database_bytes
            write_json(self.out / 'mixed-stability.json', self.report)

    def publish_formal_receipts(self):
        if self.args.repeats >= 3 and self.args.seconds >= 14400:
            from assess_mixed_stability import publish
            publish(self.out, self.out.parent)

    async def api(self, path, method='GET', body=None, metric='api', *, transport=None, pair_id=None):
        import httpx
        started = time.perf_counter()
        request_id = uuid.uuid4().hex if self.api_spans is not None else None
        route = method + ' ' + route_label(path)
        transport = transport or self.args.client_transport
        headers = {'Authorization': 'Bearer ' + TOKEN}
        if request_id is not None:
            headers['X-Audit-Request-ID'] = request_id
            self.record_api_span(request_id, 'client_start', route, transport=transport, pair_id=pair_id)
        try:
            if transport == 'persistent':
                if self.http_client is None:
                    self.http_client = httpx.AsyncClient(timeout=30, trust_env=False)
                client = self.http_client
                response = await client.request(method, self.base + '/api' + path,
                    headers=headers, json=body)
            else:
                async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                    response = await client.request(method, self.base + '/api' + path,
                        headers=headers, json=body)
        except Exception as error:
            if request_id is not None:
                self.record_api_span(request_id, 'client_error', route, error=type(error).__name__)
            raise
        elapsed = time.perf_counter() - started
        if request_id is not None:
            self.record_api_span(request_id, 'client_end', route, status=response.status_code,
                                 elapsed_ms=math.ceil(elapsed * 1000))
        self.metrics.add(self.phase + ':' + metric, elapsed)
        self.metrics.add(self.phase + ':client_route ' + route, elapsed)
        if elapsed >= .75:
            self.log('slow_api', method=method, route=route_label(path), metric=metric,
                     elapsed_ms=math.ceil(elapsed * 1000), status=response.status_code)
        response.raise_for_status()
        return response.json()

    async def model(self, state, run):
        from ocr_workbench.agent.providers import normalize_response
        from ocr_workbench.agent.store import digest
        runtime = self.app.state.agent_runtime
        name, arguments = 'get_workspace_context', {}
        if run['goal'].startswith(('ASK-', 'CANCEL-')):
            name, arguments = 'ask_user', {'question': 'Synthetic stability choice', 'options': ['Continue', 'Stop']}
        elif run['goal'].startswith('OCR-'):
            name, arguments = 'run_ocr', {'version_ids': self.versions, 'engines': ['glm']}
        message = {'role': 'assistant', 'content': 'MIXED-DONE ' + run['goal']}
        if state['step'] == 0:
            message = {'role': 'assistant', 'tool_calls': [{'id': 'first', 'type': 'function',
                       'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
        request, fresh = runtime.agent.begin_model_request(self.project, run['id'], state['generation'], state['step'], digest(message))
        if not fresh:
            return json.loads(request['normalized_response'])
        self.requests[run['id']] += 1
        if state['step'] > 0 and run['goal'].startswith('OCR-'):
            links = self.app.state.store.rows('''SELECT t.status FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
                JOIN tasks t ON t.id=j.job_id WHERE o.run_id=?''', (run['id'],))
            assert len(links) == self.args.pages + 1 and all(r['status'] == 'succeeded' for r in links), links
        self.log('model', run_id=run['id'], step=state['step'], tool=name if state['step'] == 0 else None)
        reply = normalize_response('openai_chat_completions', {'choices': [{'message': message,
            'finish_reason': 'tool_calls' if state['step'] == 0 else 'stop'}]}, request['request_id'])
        runtime.agent.finish_model_request(self.project, run['id'], state['generation'], request['request_id'], reply)
        return reply

    async def start(self, enabled):
        if self.args.service_process and not self.args.service_child:
            from mixed_service_bridge import Bridge, RemoteApp, RemoteCounts
            if self.service_process is None:
                ready = self.out / 'service-ready.json'
                self.bridge_token = uuid.uuid4().hex
                command = [sys.executable, '-X', 'utf8', '-B', str(self.source / 'scripts/agent_eval/mixed_stability.py'),
                    '--service-child', '--bundle', str(self.args.bundle), '--output', str(self.out),
                    '--seconds', str(self.args.seconds), '--repeats', str(self.args.repeats),
                    '--pages', str(self.args.pages), '--cycle-seconds', str(self.args.cycle_seconds),
                    '--playwright', str(self.args.playwright), '--client-transport', self.args.client_transport,
                    '--bridge-token', self.bridge_token]
                if self.args.trace_api_spans:
                    command.append('--trace-api-spans')
                if self.args.trace_db_spans:
                    command.append('--trace-db-spans')
                self.service_stdout = (self.out / 'service.log').open('w', encoding='utf-8')
                self.service_process = subprocess.Popen(command, stdout=self.service_stdout,
                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(600):
                    if ready.exists():
                        break
                    assert self.service_process.poll() is None, 'Service child startup failed; see service.log'
                    await asyncio.sleep(.1)
                assert ready.exists(), 'Service child did not publish control address'
                detail = json.loads(ready.read_text(encoding='utf-8'))
                assert detail['pid'] == self.service_process.pid
                self.bridge = Bridge(detail['base'], self.bridge_token)
            detail = await asyncio.to_thread(self.bridge.call, 'start', enabled=enabled,
                                             phase='warmup_on' if enabled else 'warmup_off')
            assert detail['pid'] == self.service_process.pid
            self.base = detail['base']
            self.app = RemoteApp(self.bridge, self.out / 'workspace', enabled)
            self.requests = RemoteCounts(self.bridge)
            for name in ('project', 'photos', 'versions', 'measured_versions', 'visual_result',
                         'visual_version', 'visual_model', 'sessions'):
                setattr(self, name, detail[name])
            self.report.update(service_pid=detail['pid'], input_sha256=detail['input_sha256'],
                               project_id=self.project)
            self.log('server_started', enabled=enabled, base=self.base, service_pid=detail['pid'])
            return
        import httpx
        import uvicorn
        from fastapi.staticfiles import StaticFiles
        from starlette.routing import Mount
        from ocr_workbench.service import create_app
        from ocr_workbench.adapter import EngineAdapter
        from ocr_workbench.agent.runtime import AgentRuntime
        from ocr_workbench.external_review import CredentialVault
        from test_agent_connection import echo_probe
        from test_external_review import ProviderFixture, KEY
        from ocr_workbench import external_review as ext
        from PIL import Image
        self.app = create_app(self.source, self.out / 'workspace', TOKEN, start_queue=True, agent_enabled=enabled)
        app, store = self.app, self.app.state.store
        if self.args.trace_db_spans:
            self.install_db_trace(store)
            self.install_component_trace(app)
            self.install_route_trace(app)
        @app.middleware('http')
        async def measure_dispatch(request, call_next):
            started = time.perf_counter()
            request_id = request.headers.get('x-audit-request-id') if self.api_spans is not None else None
            route = request.method + ' ' + route_label(request.url.path[4:])
            db_token = None
            if request_id and (route == 'GET /state' or route == 'POST /agent/sessions/{id}/messages'):
                db_token = self.db_context.set((request_id, route))
            if request_id:
                self.record_api_span(request_id, 'server_entry', route)
            try:
                response = await call_next(request)
            except Exception as error:
                if request_id:
                    self.record_api_span(request_id, 'server_error', route, error=type(error).__name__)
                raise
            finally:
                if db_token is not None:
                    self.db_context.reset(db_token)
            if request.url.path.startswith('/api/'):
                elapsed = time.perf_counter() - started
                self.metrics.add(self.phase + ':server_route ' + route, elapsed)
                if request_id:
                    self.record_api_span(request_id, 'server_exit', route, status=response.status_code,
                                         elapsed_ms=math.ceil(elapsed * 1000))
            return response
        # create_app mounts an existing bundle/web directory at '/'. Keep that
        # catch-all after audit-only routes, otherwise it turns them into 404s.
        app.router.routes = [route for route in app.router.routes
                             if not (isinstance(route, Mount) and route.path == '')]
        app.state.application_services.registry.bundle = self.args.bundle
        # The runtime's DLL audit uses the physical portable root. Its Python
        # worker code still comes exclusively from this frozen source copy.
        app.state.queue.factory = lambda bundle, engine, session: EngineAdapter(self.args.bundle, engine, session, worker_source=self.source / 'src')
        if not hasattr(self, 'project'):
            self.seed()
        self.provider = ProviderFixture()
        # The fixture normally records whole image payloads for short tests.
        # Replace only that collector with an append-only counter for long load.
        class CountRequests:
            count = 0
            def append(collector, item):
                collector.count += 1
        self.provider.requests = CountRequests()
        app.state.external_connection.vault = CredentialVault(self.out / 'visual-credentials')
        probe = io.BytesIO(); Image.new('RGB', (200, 100), 'white').save(probe, format='PNG')
        with patch.object(ext, 'make_probe', return_value=(probe.getvalue(), self.provider.probe)):
            view = await asyncio.to_thread(app.state.external_connection.save,
                {'protocol': 'openai', 'base_url': self.provider.url, 'model': 'vision-a', 'api_key': KEY})
        self.visual_model = view['model_id']
        if enabled:
            app.state.agent_connection.vault = CredentialVault(self.out / 'controller-credentials')
            view = await app.state.agent_connection.save({'protocol': 'openai_chat_completions',
                'base_url': 'https://synthetic.invalid/v1', 'model': 'mixed-audit', 'api_key': 'synthetic-key'},
                transport=httpx.MockTransport(echo_probe))
            self.controller_revision = view['revision']
        @app.get('/api/audit/control')
        def control():
            return {'stop': self.stop, 'active': self.ui_enabled, 'phase': self.phase}
        @app.post('/api/audit/ui-receipt')
        def receipt(body: dict):
            client = str(body['client'])
            with self.receipt_lock:
                self.ui_counts[client] += 1
                self.metrics.add(self.phase + ':ui_completion', body['latency_ms'] / 1000)
                self.report['ui_clients'][client] = {'cycles': self.ui_counts[client], 'last_utc': utc(),
                    'sse_requests': body['sse_requests'], 'page_errors': body['page_errors'], 'session_id': body['session_id']}
            assert not body['page_errors']
            events = store.rows("SELECT created FROM agent_events WHERE type='message' AND json_extract(payload,'$.content')=? ORDER BY rowid DESC LIMIT 1",
                                ('MIXED-DONE ' + body['goal'],))
            assert events
            lag = time.time() - datetime.fromisoformat(events[0]['created']).timestamp()
            self.metrics.add(self.phase + ':ui_progress', max(0, lag))
            return {'recorded': True}
        app.mount('/', StaticFiles(directory=self.source / 'web', html=True))
        original_open = AgentRuntime.open
        async def instrumented_open(runtime):
            runtime.model_override = self.model
            result = await original_open(runtime)
            for method in ['aput', 'aput_writes']:
                original = getattr(runtime.checkpoints.saver, method)
                async def measured(*args, _original=original, _name=method, **kwargs):
                    began = time.perf_counter()
                    try:
                        return await _original(*args, **kwargs)
                    finally:
                        self.metrics.add(self.phase + ':saver_' + _name, time.perf_counter() - began)
                setattr(runtime.checkpoints.saver, method, measured)
            return result
        self.open_patch = patch.object(AgentRuntime, 'open', instrumented_open)
        self.open_patch.start()
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning', access_log=False))
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(300):
            if self.server.started:
                break
            if self.server_task.done():
                await self.server_task
                raise RuntimeError('Service failed startup')
            await asyncio.sleep(.05)
        assert self.server.started
        self.base = 'http://127.0.0.1:' + str(self.server.servers[0].sockets[0].getsockname()[1])
        if enabled:
            await self.api(f'/projects/{self.project}/agent/controller-authorization', 'PUT', {'revision': self.controller_revision, 'allow': True})
            self.sessions = {}
            for name in ['control', 'ocr', 'ui-1', 'ui-2']:
                current = await self.api(f'/projects/{self.project}/agent/sessions', 'POST',
                    {'client_request_id': name, 'title': 'Mixed ' + name})
                self.sessions[name] = current['id']
        self.log('server_started', enabled=enabled, base=self.base, queue=app.state.queue.status())

    def seed(self):
        from PIL import Image, ImageDraw, ImageFont
        from ocr_workbench.imaging import add_image
        store = self.app.state.store
        self.project = store.project('Public synthetic mixed stability')['id']
        self.photos, self.versions = [], []
        image = Image.new('RGB', (640, 500), 'white')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 28)
        draw.text((40, 22), 'Synthetic Invoice 001', fill='black', font=font)
        for r, row in enumerate([['Item', 'Amount'], ['Alpha', '10.00'], ['Beta', '20.00'], ['Total', '30.00']]):
            for col, value in enumerate(row):
                x, y = 40 + col * 280, 85 + r * 85
                draw.rectangle((x, y, x+280, y+85), outline='black', width=2)
                draw.text((x+18, y+26), value, fill='black', font=font)
        self.input = self.out / 'public-synthetic.png'; image.save(self.input)
        for n in range(self.args.pages):
            temporary = self.out / f'import-{n}.png'; shutil.copy2(self.input, temporary)
            photo = add_image(store, self.project, f'load-page-{n+1}.png', temporary)
            self.photos.append(photo['id']); self.versions.append(photo['active_version'])
        self.measured_versions = list(self.versions)
        temporary = self.out / 'import-warmup.png'; shutil.copy2(self.input, temporary)
        warmup = add_image(store, self.project, 'warmup.png', temporary)
        self.photos.insert(0, warmup['id']); self.versions.insert(0, warmup['active_version'])
        temporary = self.out / 'import-visual.png'; shutil.copy2(self.input, temporary)
        visual = add_image(store, self.project, 'visual-synthetic.png', temporary)
        task = store.enqueue(self.project, [visual['active_version']], ['ppocr'])[0]
        store.claim()
        store.complete(task, {'engine': 'ppocr', 'project_image_version': visual['active_version'], 'text': 'Amount 001.00',
            'tables': [], 'blocks': [], 'image': {'width': 640, 'height': 500}})
        self.visual_result = store.one('tasks', task)['result_id']; self.visual_version = visual['active_version']
        self.report['input_sha256'] = checksum(self.input)
        self.report['project_id'] = self.project

    async def stop_server(self):
        if self.bridge and not self.args.service_child:
            result = await asyncio.to_thread(self.bridge.call, 'stop')
            assert result['closed'], 'Service child did not close'
            self.report['visual_requests_count'] = result['visual_requests_count']
            self.log('server_closed', service_pid=self.service_process.pid)
            return
        if self.server_task and not self.server_task.done():
            self.server.should_exit = True
            await asyncio.wait_for(self.server_task, 60)
            self.log('server_closed', queue=self.app.state.queue.status())
        if self.open_patch:
            self.open_patch.stop(); self.open_patch = None
        if self.provider:
            self.report['visual_requests_count'] = self.report.get('visual_requests_count', 0) + self.provider.requests.count
            await asyncio.to_thread(self.provider.close); self.provider = None

    async def wait_jobs(self, ids, timeout=300):
        start = time.perf_counter()
        previous = None
        while time.perf_counter() - start < timeout:
            adapter = self.app.state.queue.adapter
            if self.app.state.queue.current in ids and adapter and adapter.ready and adapter.process:
                self.worker_pids.add(adapter.process.pid)
            rows = [self.app.state.store.one('tasks', key) for key in ids]
            states = [r['status'] for r in rows]
            if states != previous:
                self.log('jobs', ids=ids, states=states); previous = states
            assert not any(s in {'failed', 'cancelled', 'interrupted'} for s in states), rows
            if all(s == 'succeeded' for s in states):
                return rows
            await asyncio.sleep(.1)
        raise TimeoutError('Real tasks did not finish: ' + repr(ids))

    async def manual_ocr(self, identity):
        start = time.perf_counter()
        body = {'version_ids': self.versions, 'engines': ['glm'], 'request_id': identity}
        result = await self.api(f'/projects/{self.project}/tasks', 'POST', body)
        replay = await self.api(f'/projects/{self.project}/tasks', 'POST', body)
        assert replay['task_ids'] == result['task_ids'] and len(result['task_ids']) == self.args.pages + 1
        rows = await self.wait_jobs(result['task_ids'])
        elapsed = time.perf_counter() - start
        self.log('ocr_batch', task_ids=result['task_ids'], elapsed_seconds=elapsed, source='manual')
        return self.batch_timing(rows, elapsed)

    def batch_timing(self, rows, elapsed):
        # Queue ordering may tie on sub-millisecond creation timestamps. All
        # input images are identical: exclude the actual first completed task.
        measured = sorted(rows, key=lambda row: (row['started'], row['finished']))[1:]
        assert len(measured) == self.args.pages
        seconds = (max(datetime.fromisoformat(row['finished']) for row in measured) -
                   min(datetime.fromisoformat(row['started']) for row in measured)).total_seconds()
        assert seconds > 0
        return {'end_to_end_seconds': elapsed, 'measured_processing_seconds': seconds,
                'measured_task_ids': [row['id'] for row in measured]}

    async def send(self, goal, session='control', processing=False):
        selected = await self.api(f'/projects/{self.project}/agent/selection', 'POST',
            {'image_ids': self.photos, 'engines': ['glm'], 'allow_processing': processing, 'allow_reprocess': processing})
        body = {'client_request_id': goal, 'content': goal, 'selection_token': selected['selection_token']}
        accepted = await self.api('/agent/sessions/' + self.sessions[session] + '/messages', 'POST', body)
        replay = await self.api('/agent/sessions/' + self.sessions[session] + '/messages', 'POST', body)
        assert replay['run']['id'] == accepted['run']['id']
        return accepted['run']

    async def wait_run(self, run, waiting=False):
        start = time.perf_counter()
        while time.perf_counter() - start < 300:
            adapter = self.app.state.queue.adapter
            current_task = self.app.state.queue.current
            belongs = current_task and self.app.state.store.rows('''SELECT 1 FROM agent_job_links j
                JOIN agent_operations o ON o.id=j.operation_id WHERE o.run_id=? AND j.job_id=?''', (run['id'], current_task))
            if belongs and adapter and adapter.ready and adapter.process:
                self.worker_pids.add(adapter.process.pid)
            current = await self.api('/agent/runs/' + run['id'], metric='progress_api')
            assert current['status'] not in {'failed', 'interrupted'}, current
            if current['status'] in {'completed', 'cancelled'} or waiting and current['status'] == 'waiting_user':
                return current
            await asyncio.sleep(.1)
        raise TimeoutError('Run did not finish: ' + run['id'])

    async def control_cycle(self, index, mode=None):
        mode = mode or ['READ', 'ASK', 'CANCEL'][index % 3]
        run = await self.send(f'{mode}-{self.phase}-{index}')
        await self.wait_run(run, waiting=mode != 'READ')
        if mode != 'READ':
            count = self.requests[run['id']]
            await asyncio.sleep(.2)
            assert self.requests[run['id']] == count == 1
            if mode == 'CANCEL':
                result = await self.api('/agent/runs/' + run['id'] + '/cancel', 'POST',
                    {'client_request_id': 'cancel-' + run['id'], 'generation': run['generation'], 'mode': 'stop_agent'}, metric='cancel_api')
                assert result['status'] == 'cancelled'
            else:
                decision = self.app.state.store.rows("SELECT * FROM agent_decisions WHERE run_id=? AND status='pending'", (run['id'],))[0]
                await self.api('/agent/decisions/' + decision['id'] + '/reply', 'POST',
                    {'client_request_id': 'reply-' + run['id'], 'payload_hash': decision['payload_hash'], 'option_id': 'option-0'})
                await self.wait_run(run)
        assert self.requests[run['id']] == (1 if mode == 'CANCEL' else 2)
        self.run_counts[mode] += 1

    async def agent_ocr(self, identity):
        start = time.perf_counter()
        before = self.app.state.store.rows("SELECT COUNT(*) n FROM tasks WHERE engine='glm'")[0]['n']
        run = await self.send('OCR-' + identity, session='ocr', processing=True)
        final = await self.wait_run(run)
        assert final['status'] == 'completed' and final['outcome'] == 'success', final
        count = self.app.state.store.rows("SELECT COUNT(*) n FROM tasks WHERE engine='glm'")[0]['n']
        assert count - before == self.args.pages + 1
        assert self.requests[run['id']] == 2, 'Waiting real OCR jobs emitted extra model requests'
        self.run_counts['OCR'] += 1
        elapsed = time.perf_counter() - start
        self.log('ocr_batch', run_id=run['id'], elapsed_seconds=elapsed, source='agent')
        rows = self.app.state.store.rows('''SELECT t.* FROM tasks t JOIN agent_job_links j ON j.job_id=t.id
            JOIN agent_operations o ON o.id=j.operation_id WHERE o.run_id=? ORDER BY t.rowid''', (run['id'],))
        return self.batch_timing(rows, elapsed)

    async def visual(self, identity):
        body = {'request_id': identity, 'model_id': self.visual_model, 'revision': 0,
                'version_id': self.visual_version, 'scope': 'page'}
        result = await self.api('/results/' + self.visual_result + '/multimodal', 'POST', body)
        replay = await self.api('/results/' + self.visual_result + '/multimodal', 'POST', body)
        assert result['task_id'] == replay['task_id']
        await self.wait_jobs([result['task_id']])
        self.run_counts['visual'] += 1

    async def background_controls(self, done, prefix, all_modes=False):
        index = 0
        while not done.is_set():
            await self.control_cycle(prefix + index if isinstance(prefix, int) else index, None if all_modes else 'READ')
            index += 1
            try:
                await asyncio.wait_for(done.wait(), 1)
            except TimeoutError:
                pass

    async def sample(self):
        import psutil
        process = psutil.Process()
        while not self.stop:
            try:
                tree = [process] + process.children(recursive=True)
            except psutil.Error as error:
                self.log('resource_tree_retry', error=f'{type(error).__name__}: {error}')
                await asyncio.sleep(5)
                continue
            records = []
            for p in tree:
                try:
                    records.append({'pid': p.pid, 'name': p.name(), 'rss_bytes': p.memory_info().rss,
                                    'handles': p.num_handles(), 'threads': p.num_threads()})
                except psutil.Error:
                    pass
            database_bytes = {}
            for db_path in (self.out / 'workspace').glob('*.sqlite3*'):
                try:
                    database_bytes[db_path.name] = db_path.stat().st_size
                except OSError:
                    # SQLite can checkpoint and remove a WAL during sampling.
                    continue
            record = {'utc': utc(), 'phase': self.phase, 'processes': records,
                'tree_rss_bytes': sum(p['rss_bytes'] for p in records), 'tree_handles': sum(p['handles'] for p in records),
                'tree_threads': sum(p['threads'] for p in records), 'children': len(records)-1,
                'database_bytes': database_bytes}
            self.resources.write(json.dumps(record) + '\n'); self.resources.flush()
            self.resource_samples[self.phase] += 1
            self.report['latest_resources'] = record
            self.report['resource_samples'] = dict(self.resource_samples)
            self.save()
            await asyncio.sleep(5)

    async def api_load(self, done):
        while not done.is_set():
            await self.api('/state', metric='state_api')
            await asyncio.sleep(.5)

    async def paired_transport_probe(self, done):
        # Adjacent requests share the same live OCR/UI pressure and frozen app.
        # Alternate order so temporal drift does not always favor one transport.
        for index in range(8):
            if done.is_set():
                break
            pair_id = f'{self.phase}-{index}'
            order = ('per-request', 'persistent') if index % 2 == 0 else ('persistent', 'per-request')
            for transport in order:
                await self.api('/state', metric='paired_state_' + transport,
                               transport=transport, pair_id=pair_id)
            await asyncio.sleep(.2)

    async def start_ui(self):
        seed = {'base': self.base, 'token': TOKEN, 'sessions': [self.sessions['ui-1'], self.sessions['ui-2']],
                'playwright': str(self.args.playwright), 'output': str(self.out)}
        write_json(self.out / 'ui-seed.json', seed)
        self.ui_enabled = True
        if self.bridge:
            await asyncio.to_thread(self.bridge.call, 'configure', ui_enabled=True)
        self.ui_stdout = (self.out / 'ui.log').open('w', encoding='utf-8')
        self.ui_process = subprocess.Popen(['node', str(self.source / 'scripts/agent_eval/mixed_stability.mjs'), str(self.out / 'ui-seed.json')],
            stdout=self.ui_stdout, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(600):
            if (self.out / 'ui-ready.json').exists():
                return
            assert self.ui_process.poll() is None, 'UI startup failed; see ui.log'
            await asyncio.sleep(.1)
        raise TimeoutError('Two UI clients failed startup')

    async def batch(self, phase, index):
        self.phase = phase
        if self.bridge:
            await asyncio.to_thread(self.bridge.call, 'configure', phase=phase)
        self.worker_pids.clear()
        done = asyncio.Event()
        helpers = [asyncio.create_task(self.api_load(done))]
        if phase in {'agent_read', 'agent_ocr', 'mixed_two_ui', 'soak'}:
            helpers.append(asyncio.create_task(self.background_controls(done, index*1000, all_modes=phase in {'mixed_two_ui', 'soak'})))
        if phase in {'mixed_two_ui', 'soak'}:
            helpers.append(asyncio.create_task(self.visual(f'visual-{phase}-{index}')))
            if self.args.paired_transport_probe:
                helpers.append(asyncio.create_task(self.paired_transport_probe(done)))
        try:
            timing = await (self.agent_ocr(f'{phase}-{index}') if phase in {'agent_ocr', 'mixed_two_ui', 'soak'}
                             else self.manual_ocr(f'ocr-{phase}-{index}'))
        finally:
            done.set()
            await asyncio.gather(*helpers)
        assert len(self.worker_pids) == 1, 'Measured batch must stay in one warmed GPU worker: ' + repr(self.worker_pids)
        self.metrics.add(phase + ':ocr_batch', timing['end_to_end_seconds'])
        self.report['comparisons'].append({'phase': phase, 'repeat': index, **timing,
            'pages_per_second': self.args.pages / timing['measured_processing_seconds'], 'gpu_worker_pid': next(iter(self.worker_pids))})
        self.assert_healthy()
        self.save()
        print(json.dumps({'phase': phase, 'repeat': index, **timing}), flush=True)

    def assert_healthy(self):
        assert self.app.state.queue.status()['healthy']
        assert self.app.state.external_queue.status()['healthy']
        runtime = self.app.state.agent_runtime
        if runtime:
            assert not runtime.observer_error
            errors = self.app.state.store.rows("SELECT id,status FROM agent_runs WHERE status IN ('failed','interrupted')")
            assert not errors, errors
            stale = self.app.state.store.rows("SELECT id FROM agent_runs WHERE owner IS NOT NULL AND lease_expires<=? AND status NOT IN ('completed','cancelled','failed')", (time.time(),))
            assert not stale, stale
        if self.ui_process:
            assert self.ui_process.poll() is None, 'UI process exited early; see ui.log'

    async def reap_process(self, process, label):
        """Bounded fallback for a failed startup or graceful shutdown."""
        if process is None or process.poll() is not None:
            return
        descendants = []
        if getattr(process, 'pid', None) is not None:
            import psutil
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                pass
        for child in reversed(descendants):
            try:
                child.terminate()
            except Exception:
                pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
        except (TimeoutError, OSError):
            if process.poll() is None:
                process.kill()
            await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
        for child in descendants:
            try:
                await asyncio.wait_for(asyncio.to_thread(child.wait, timeout=2), 3)
            except Exception:
                try:
                    child.kill()
                    await asyncio.wait_for(asyncio.to_thread(child.wait, timeout=2), 3)
                except Exception:
                    pass

    async def run(self):
        sampler = asyncio.create_task(self.sample())
        try:
            await self.start(False)
            self.phase = 'warmup_off'; await self.manual_ocr('warmup-disabled')
            for index in range(self.args.repeats):
                await self.batch('agent_off', index)
            await self.stop_server()
            await self.start(True)
            self.phase = 'warmup_on'; await self.manual_ocr('warmup-enabled')
            for phase in ['agent_idle', 'agent_read', 'agent_ocr']:
                for index in range(self.args.repeats):
                    await self.batch(phase, index)
            self.phase = 'ui_start'; await self.start_ui()
            for index in range(self.args.repeats):
                await self.batch('mixed_two_ui', index)
            self.save()
            self.publish_formal_receipts()
            self.phase = 'soak'; started = time.monotonic(); self.report['mixed_started_utc'] = utc()
            if self.bridge:
                await asyncio.to_thread(self.bridge.call, 'configure', phase='soak')
            index = 0
            while time.monotonic() - started < self.args.seconds:
                batch_started = time.monotonic()
                await self.batch('soak', index)
                index += 1
                self.report['elapsed_mixed_seconds'] = time.monotonic() - started
                self.report['mixed_cycles'] = index
                self.save()
                await asyncio.sleep(max(0, min(self.args.cycle_seconds - (time.monotonic() - batch_started),
                                                self.args.seconds - (time.monotonic() - started))))
            self.report['elapsed_mixed_seconds'] = time.monotonic() - started
            assert all(self.ui_counts[str(i)] for i in [1, 2]), self.ui_counts
            assert self.run_counts['ASK'] and self.run_counts['CANCEL'] and self.run_counts['visual']
            assert not sampler.done(), 'Resource sampler stopped during mixed load'
            assert self.resource_samples['soak'] > 0, 'No resource samples during mixed load'
            self.assert_healthy()
            manifests = json.loads((self.out / 'source-manifest.json').read_text())
            assert all(checksum(self.source / name) == value for name, value in manifests.items()), 'Frozen source changed'
            assert checksum(self.args.bundle / 'manifest.json') == self.report['asset_manifest_sha256']
            baseline = [r['pages_per_second'] for r in self.report['comparisons'] if r['phase'] == 'agent_off']
            self.report['throughput_vs_baseline'] = {p: (sum(r['pages_per_second'] for r in self.report['comparisons'] if r['phase'] == p) /
                len([r for r in self.report['comparisons'] if r['phase'] == p])) / (sum(baseline)/len(baseline))
                for p in ['agent_idle', 'agent_read', 'agent_ocr', 'mixed_two_ui', 'soak'] if any(r['phase'] == p for r in self.report['comparisons'])}
            self.report['status'] = 'pass'
        except BaseException as error:
            self.report['status'] = 'failed'; self.report['errors'].append(f'{type(error).__name__}: {error}')
            raise
        finally:
            self.stop = True
            async def cleanup(label, action):
                try:
                    return await action()
                except BaseException as error:
                    self.report['status'] = 'failed'
                    self.report['errors'].append(label + ': ' + repr(error))
                    return None

            if self.bridge:
                await cleanup('Service configure', lambda: asyncio.to_thread(self.bridge.call, 'configure', stop=True))
            sampler.cancel()
            sampler_result = (await asyncio.gather(sampler, return_exceptions=True))[0]
            if isinstance(sampler_result, BaseException) and not isinstance(sampler_result, asyncio.CancelledError):
                self.report['status'] = 'failed'
                self.report['errors'].append('Resource sampler: ' + repr(sampler_result))
            if self.ui_process:
                result = await cleanup('UI shutdown', lambda: asyncio.wait_for(
                    asyncio.to_thread(self.ui_process.wait), 30))
                if result != 0:
                    self.report['status'] = 'failed'
                    self.report['errors'].append('UI returncode: ' + repr(result))
                await cleanup('UI process reap', lambda: self.reap_process(self.ui_process, 'ui'))
                if hasattr(self, 'ui_stdout'):
                    self.ui_stdout.close()
            if self.bridge:
                # Capture the final UI receipts and server-side latency histograms
                # while the child is still reachable.
                await cleanup('Final service metrics', lambda: asyncio.to_thread(self.save))
            service_stopped = False
            if self.bridge:
                async def stop_service():
                    await self.stop_server()
                    return True
                stop_result = await cleanup('Service stop', stop_service)
                shutdown_result = await cleanup('Service shutdown', lambda: asyncio.to_thread(self.bridge.call, 'shutdown'))
                if shutdown_result is not None:
                    returncode = await cleanup('Service wait', lambda: asyncio.wait_for(
                        asyncio.to_thread(self.service_process.wait), 90))
                    service_stopped = stop_result is True and returncode == 0
                await cleanup('Bridge close', lambda: asyncio.to_thread(self.bridge.close))
                self.bridge = None
            elif self.app and not self.service_process:
                await cleanup('Service stop', self.stop_server)
            if self.service_process:
                await cleanup('Service process reap', lambda: self.reap_process(self.service_process, 'service'))
                self.report['service_returncode'] = self.service_process.poll()
                if not service_stopped:
                    self.report['status'] = 'failed'
                    self.report['errors'].append('Service did not complete graceful stop and shutdown')
            if self.service_stdout:
                self.service_stdout.close()
            if self.http_client is not None:
                await cleanup('HTTP client close', self.http_client.aclose)
            queue_status = None
            if self.app and not self.service_process:
                queue_status = await cleanup('GPU queue status', lambda: asyncio.to_thread(self.app.state.queue.status))
            self.report['shutdown'] = {'service_closed': (service_stopped if self.service_process else
                                                         self.server_task is None or self.server_task.done()),
                'ui_returncode': self.ui_process.returncode if self.ui_process else None,
                'gpu_queue': queue_status}
            self.report['finished_utc'] = utc()
            # The cached child histogram remains in the receipt after bridge close.
            self.save()
            self.resources.close(); self.trace.close()
            if self.api_spans is not None:
                self.api_spans.close()
            if self.db_spans is not None:
                self.db_spans.close()
            try:
                self.publish_formal_receipts()
            except Exception as error:
                self.report['status'] = 'failed'
                self.report['errors'].append('F06 summary publishing: ' + repr(error))
                self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=14400)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--pages', type=int, default=3)
    parser.add_argument('--cycle-seconds', type=int, default=30)
    parser.add_argument('--playwright', type=Path, default=ROOT / 'frontend/node_modules/@playwright/test/index.mjs')
    parser.add_argument('--client-transport', choices=('per-request', 'persistent'), default='per-request',
                        help='Preserve the original fresh-client metric, or measure steady keep-alive in a separate run')
    parser.add_argument('--trace-api-spans', action='store_true',
                        help='Record paired client/server monotonic timestamps for audit-owned API requests')
    parser.add_argument('--paired-transport-probe', action='store_true',
                        help='During two-UI phases, interleave fresh/persistent GET /state pairs (diagnostic only)')
    parser.add_argument('--trace-db-spans', action='store_true',
                        help='Audit-only transaction/lock/SQL timing on traced state and message requests')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--service-process', action='store_true',
                        help='Run the real application in a separate local child process')
    parser.add_argument('--service-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--bridge-token', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.seconds, args.repeats, args.pages, args.cycle_seconds) < 1:
        parser.error('All duration/count values must be positive')
    if args.paired_transport_probe and not args.trace_api_spans:
        parser.error('--paired-transport-probe requires --trace-api-spans')
    if args.trace_db_spans and not args.trace_api_spans:
        parser.error('--trace-db-spans requires --trace-api-spans')
    if args.service_process and args.client_transport != 'per-request':
        parser.error('--service-process requires --client-transport per-request for the F06 metric')
    args.output, args.bundle, args.playwright = args.output.resolve(), args.bundle.resolve(), args.playwright.resolve()
    if not args.worker and not args.service_child:
        raise SystemExit(freeze(args))
    # The portable Python runs in isolated mode and omits the script directory
    # from sys.path; include the frozen assessor used by formal receipts.
    sys.path[:0] = [str(args.output / 'source/src'), str(args.output / 'source/tests'),
                    str(args.output / 'source/scripts/agent_eval')]
    forbid_remote_network()
    if args.service_child:
        from mixed_service_bridge import serve_control
        audit = Audit(args)
        audit.bridge_token = args.bridge_token
        try:
            asyncio.run(serve_control(audit, args.output / 'service-ready.json'))
        finally:
            for stream in (audit.resources, audit.trace, audit.api_spans, audit.db_spans):
                if stream is not None:
                    stream.close()
    else:
        write_json(args.output / 'worker.json', {'pid': os.getpid(), 'started_utc': utc(), 'argv': sys.argv})
        asyncio.run(Audit(args).run())


if __name__ == '__main__':
    main()

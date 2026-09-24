"""Narrow loopback control plane for the optional independent service process.

Workload requests still go through Audit.api with the original client transport.
Only setup, phase/UI control, and bounded app.state observations use this bridge.
"""
import asyncio
import os
from pathlib import Path
import httpx


QUERIES = {
    'linked_current': '''SELECT 1 FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id WHERE o.run_id=? AND j.job_id=?''',
    'pending_decision': "SELECT * FROM agent_decisions WHERE run_id=? AND status='pending'",
    'task_count': "SELECT COUNT(*) n FROM tasks WHERE engine='glm'",
    'agent_tasks': '''SELECT t.* FROM tasks t JOIN agent_job_links j ON j.job_id=t.id
            JOIN agent_operations o ON o.id=j.operation_id WHERE o.run_id=? ORDER BY t.rowid''',
    'links_status': '''SELECT t.status FROM agent_job_links j JOIN agent_operations o ON o.id=j.operation_id
                JOIN tasks t ON t.id=j.job_id WHERE o.run_id=?''',
    'failed_runs': "SELECT id,status FROM agent_runs WHERE status IN ('failed','interrupted')",
    'stale_runs': "SELECT id FROM agent_runs WHERE owner IS NOT NULL AND lease_expires<=? AND status NOT IN ('completed','cancelled','failed')",
}


def canonical(sql):
    return ' '.join(sql.split())


QUERY_IDS = {canonical(sql): key for key, sql in QUERIES.items()}


class Bridge:
    # Startup includes app initialization; shutdown can wait for queue workers.
    OPERATION_TIMEOUTS = {'start': 600, 'stop': 120, 'shutdown': 60}

    def __init__(self, base, token):
        self.client = httpx.Client(base_url=base, headers={'Authorization': 'Bearer ' + token},
                                   timeout=15, trust_env=False)

    def call(self, operation, **kwargs):
        response = self.client.post('/api/audit/bridge', json={'operation': operation, **kwargs},
                                    timeout=self.OPERATION_TIMEOUTS.get(operation, 15))
        response.raise_for_status()
        return response.json()

    def close(self):
        self.client.close()


class RemoteStore:
    def __init__(self, bridge, root):
        from pathlib import Path
        self.bridge, self.root = bridge, Path(root)

    def rows(self, sql, params=()):
        key = QUERY_IDS[canonical(sql)]
        return self.bridge.call('rows', query=key, params=list(params))['rows']

    def one(self, table, key):
        if table != 'tasks':
            raise ValueError('Only task observations may cross the service bridge')
        return self.bridge.call('task', id=key)['row']


class RemoteQueue:
    def __init__(self, bridge, name):
        self.bridge, self.name = bridge, name

    def _state(self):
        return self.bridge.call('state')['queues'][self.name]

    def status(self):
        return self._state()['status']

    @property
    def current(self):
        return self._state().get('current')

    @property
    def adapter(self):
        state = self._state().get('adapter')
        if not state:
            return None
        from types import SimpleNamespace
        return SimpleNamespace(ready=state['ready'],
                               process=SimpleNamespace(pid=state['pid']) if state['pid'] else None)


class RemoteRuntime:
    def __init__(self, bridge):
        self.bridge = bridge

    @property
    def observer_error(self):
        return self.bridge.call('state')['observer_error']


class RemoteApp:
    def __init__(self, bridge, root, enabled):
        from types import SimpleNamespace
        self.state = SimpleNamespace(store=RemoteStore(bridge, root),
            queue=RemoteQueue(bridge, 'queue'), external_queue=RemoteQueue(bridge, 'external_queue'),
            agent_runtime=RemoteRuntime(bridge) if enabled else None)


class RemoteCounts:
    def __init__(self, bridge):
        self.bridge = bridge

    def __getitem__(self, run_id):
        return self.bridge.call('counts')['requests'].get(run_id, 0)


def control_app(audit, finished):
    """No arbitrary SQL or filesystem operations cross the control boundary."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.middleware('http')
    async def authenticate(request, call_next):
        if request.headers.get('authorization') != 'Bearer ' + audit.bridge_token:
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        return await call_next(request)

    @app.post('/api/audit/bridge')
    async def call(body: dict):
        op = body.get('operation')
        if op == 'start':
            if audit.app is not None and audit.server_task and not audit.server_task.done():
                raise HTTPException(409, 'Service already started')
            audit.phase = str(body['phase'])
            await audit.start(bool(body['enabled']))
            return {'base': audit.base, 'pid': os.getpid(), 'project': audit.project,
                    'photos': audit.photos, 'versions': audit.versions,
                    'measured_versions': audit.measured_versions, 'input_sha256': audit.report['input_sha256'],
                    'visual_result': audit.visual_result, 'visual_version': audit.visual_version,
                    'visual_model': audit.visual_model, 'sessions': getattr(audit, 'sessions', {})}
        if op == 'stop':
            await audit.stop_server()
            return {'closed': audit.server_task is None or audit.server_task.done(),
                    'visual_requests_count': audit.report.get('visual_requests_count', 0)}
        if op == 'configure':
            if 'phase' in body:
                audit.phase = str(body['phase'])
            if 'ui_enabled' in body:
                audit.ui_enabled = bool(body['ui_enabled'])
            if 'stop' in body:
                audit.stop = bool(body['stop'])
            return {'ok': True}
        if op == 'state':
            queues = {}
            for name in ('queue', 'external_queue'):
                queue = getattr(audit.app.state, name)
                adapter = queue.adapter if name == 'queue' else None
                queues[name] = {'status': queue.status(), 'current': queue.current,
                                'adapter': {'ready': adapter.ready,
                                            'pid': adapter.process.pid if adapter.process else None}
                                if adapter else None}
            runtime = audit.app.state.agent_runtime
            return {'queues': queues, 'observer_error': runtime.observer_error if runtime else None,
                    'ui_counts': dict(audit.ui_counts), 'ui_clients': audit.report['ui_clients'],
                    'metrics': audit.metrics.report(), 'service_pid': os.getpid()}
        if op == 'counts':
            return {'requests': dict(audit.requests)}
        if op == 'rows':
            sql = QUERIES.get(body.get('query'))
            if sql is None:
                raise HTTPException(400, 'Unknown observation query')
            return {'rows': audit.app.state.store.rows(sql, tuple(body.get('params', [])))}
        if op == 'task':
            return {'row': audit.app.state.store.one('tasks', body['id'])}
        if op == 'shutdown':
            if audit.server_task and not audit.server_task.done():
                raise HTTPException(409, 'Stop the service before shutdown')
            finished.set()
            return {'ok': True}
        raise HTTPException(400, 'Unknown bridge operation')

    return app


async def serve_control(audit, ready_path: Path):
    import uvicorn
    from mixed_stability import utc, write_json

    finished = asyncio.Event()
    server = uvicorn.Server(uvicorn.Config(control_app(audit, finished), host='127.0.0.1', port=0,
                                           log_level='warning', access_log=False))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(300):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError('Control server failed startup')
            await asyncio.sleep(.05)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        write_json(ready_path, {'pid': os.getpid(), 'base': f'http://127.0.0.1:{port}', 'started_utc': utc()})
        await finished.wait()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 60)

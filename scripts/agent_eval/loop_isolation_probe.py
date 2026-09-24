"""Short, fixed-source event-loop isolation probe; never a formal F06 receipt.

Both arms issue identical scheduled loopback /state and local visual-fixture
requests. In `shared`, uvicorn, httpx, and the client workload share one loop.
In `split`, only uvicorn moves to a separate Python process; requests and the
fixture remain in the controller. The deliberate client CPU bursts expose
event-loop contention without claiming to reproduce the full OCR/UI matrix.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid


TOKEN = 'isolated-mixed-stability-local-token'
DEFAULT_SOURCE = Path('E:/OCR-api-latency-diagnostic-20260924/profile-route/source')
DEFAULT_BUNDLE = Path('E:/DeloitteOCR-Agent-Experimental-20260921/candidate-05')
DEFAULT_PYTHON = Path('C:/Users/A/Desktop/OCR/build/ocr-agent-20260921-langgraph/langgraph-probe/运行时 中文/service/python.exe')


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def now():
    return datetime.now(timezone.utc).isoformat()


def percentile(values, q):
    values = sorted(values)
    return values[math.ceil(len(values) * q) - 1] if values else None


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def app_for(source, bundle, workspace, spans):
    from ocr_workbench.service import create_app
    app = create_app(source, workspace, TOKEN, start_queue=False, agent_enabled=False)
    app.state.application_services.registry.bundle = bundle

    @app.middleware('http')
    async def trace(request, call_next):
        rid = request.headers.get('x-audit-request-id')
        entry = time.perf_counter_ns()
        response = await call_next(request)
        exit_ = time.perf_counter_ns()
        if rid:
            row = {'id': rid, 'entry_ns': entry, 'exit_ns': exit_,
                   'status': response.status_code, 'pid': os.getpid()}
            spans.append(row)
            with (workspace.parent / 'server-spans.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row) + '\n')
        return response
    return app


async def server(args):
    import uvicorn
    spans = []
    app = app_for(args.source, args.bundle, args.output / 'split-workspace', spans)
    lag_stop = asyncio.Event()
    lag_task = asyncio.create_task(server_lag_sampler(args.output / 'server-loop-lag.jsonl', lag_stop))
    ready = args.output / 'server-ready.json'
    config = uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning', access_log=False)
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve())
    try:
        while not instance.started:
            if task.done():
                await task
            await asyncio.sleep(.02)
        write_json(ready, {'pid': os.getpid(), 'port': args.port})
        await task
    finally:
        lag_stop.set()
        await lag_task


async def lag_sampler(rows, stop):
    period = .01
    deadline = asyncio.get_running_loop().time() + period
    while not stop.is_set():
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        rows.append(max(0, (asyncio.get_running_loop().time() - deadline) * 1000))
        deadline += period
        if deadline < asyncio.get_running_loop().time() - period:
            deadline = asyncio.get_running_loop().time() + period


async def server_lag_sampler(path, stop):
    period = .01
    deadline = asyncio.get_running_loop().time() + period
    pending = []
    while not stop.is_set():
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
        pending.append(max(0, (asyncio.get_running_loop().time() - deadline) * 1000))
        deadline += period
        if deadline < asyncio.get_running_loop().time() - period:
            deadline = asyncio.get_running_loop().time() + period
        if len(pending) >= 25:
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n'.join(json.dumps(value) for value in pending) + '\n')
            pending.clear()
    if pending:
        with path.open('a', encoding='utf-8') as stream:
            stream.write('\n'.join(json.dumps(value) for value in pending) + '\n')


async def resource_sampler(rows, stop, pids):
    import psutil
    while not stop.is_set():
        sample = {'utc': now(), 'processes': []}
        for pid in pids():
            try:
                process = psutil.Process(pid)
                cpu = process.cpu_times()
                seconds = cpu.user + cpu.system
                sample['processes'].append({'pid': pid, 'cpu_seconds': seconds,
                                            'rss_bytes': process.memory_info().rss})
            except psutil.Error:
                continue
        rows.append(sample)
        await asyncio.sleep(.25)


def visual_body():
    return {'model': 'vision-a', 'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'OCR_TARGET_DATA_JSON\n' + json.dumps(
            [{'target_id': 'probe', 'before': '001.00'}])}]}]}


async def workload(base, fixture, seconds, server_pid):
    import httpx
    events, lag, resources = [], [], []
    stop = asyncio.Event()
    start = asyncio.get_running_loop().time() + .2
    self_pid = os.getpid()
    sampler = asyncio.create_task(lag_sampler(lag, stop))
    resource = asyncio.create_task(resource_sampler(resources, stop, lambda: sorted({self_pid, server_pid})))

    async def state_stream(label, interval, offset, fresh):
        async with httpx.AsyncClient(timeout=10, trust_env=False) as kept:
            index = 0
            while True:
                target = start + offset + index * interval
                if target >= start + seconds:
                    break
                await asyncio.sleep(max(0, target - asyncio.get_running_loop().time()))
                rid = uuid.uuid4().hex
                sent = time.perf_counter_ns()
                try:
                    if fresh:
                        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                            response = await client.get(base + '/api/state', headers={
                                'Authorization': 'Bearer ' + TOKEN, 'X-Audit-Request-ID': rid})
                    else:
                        response = await kept.get(base + '/api/state', headers={
                            'Authorization': 'Bearer ' + TOKEN, 'X-Audit-Request-ID': rid})
                    status = response.status_code
                    response.raise_for_status()
                except Exception as error:
                    status = type(error).__name__
                events.append({'id': rid, 'label': label, 'index': index, 'planned_s': target - start,
                               'client_start_ns': sent, 'client_end_ns': time.perf_counter_ns(),
                               'status': status})
                index += 1

    async def visual_stream():
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            index = 0
            while start + .05 + index * .3 < start + seconds:
                target = start + .05 + index * .3
                await asyncio.sleep(max(0, target - asyncio.get_running_loop().time()))
                response = await client.post(fixture.url + '/chat/completions', json=visual_body())
                response.raise_for_status()
                index += 1
            return index

    async def cpu_stream():
        # Same fixed client-side 8 ms bursts in both arms, every 50 ms.
        index = 0
        while start + .025 + index * .05 < start + seconds:
            target = start + .025 + index * .05
            await asyncio.sleep(max(0, target - asyncio.get_running_loop().time()))
            until = time.perf_counter() + .008
            while time.perf_counter() < until:
                hashlib.sha256(b'fixed-client-work' * 256).digest()
            index += 1
        return index

    try:
        counts = await asyncio.gather(state_stream('audit-fresh', .25, 0, True),
            state_stream('ui-1-keepalive', .4, .07, False),
            state_stream('ui-2-keepalive', .4, .17, False), visual_stream(), cpu_stream())
    finally:
        stop.set()
        await asyncio.gather(sampler, resource)
    return events, lag, resources, {'visual_requests': counts[-2], 'cpu_bursts': counts[-1]}


async def arm(args, name):
    import uvicorn
    from test_external_review import ProviderFixture
    output = args.output / name
    output.mkdir()
    fixture = ProviderFixture()
    server_task = None
    server_lag_task = None
    server_lag_stop = None
    process = None
    spans = []
    p = port()
    try:
        if name == 'shared':
            app = app_for(args.source, args.bundle, output / 'workspace', spans)
            server_lag_stop = asyncio.Event()
            server_lag_task = asyncio.create_task(server_lag_sampler(output / 'server-loop-lag.jsonl', server_lag_stop))
            instance = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=p,
                                                      log_level='warning', access_log=False))
            server_task = asyncio.create_task(instance.serve())
            while not instance.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(.02)
            server_pid = os.getpid()
        else:
            command = [str(args.python), '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                '--server', '--source', str(args.source), '--bundle', str(args.bundle),
                '--output', str(output), '--port', str(p)]
            with (output / 'server.log').open('w', encoding='utf-8') as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(1500):
                if (output / 'server-ready.json').exists():
                    break
                if process.poll() is not None:
                    raise RuntimeError('Separate server exited; see server.log')
                await asyncio.sleep(.02)
            else:
                raise TimeoutError('Separate server startup')
            server_pid = process.pid
        events, lag, resources, counts = await workload('http://127.0.0.1:' + str(p), fixture,
                                                       args.seconds, server_pid)
    finally:
        if server_task:
            instance.should_exit = True
            await asyncio.wait_for(server_task, 20)
        if server_lag_task:
            server_lag_stop.set()
            await server_lag_task
        if process:
            process.terminate()
            await asyncio.to_thread(process.wait, 20)
        await asyncio.to_thread(fixture.close)
    if name == 'split':
        spans = [json.loads(line) for line in (output / 'server-spans.jsonl').read_text('utf-8').splitlines()]
    server_lag = [json.loads(line) for line in (output / 'server-loop-lag.jsonl').read_text('utf-8').splitlines()]
    by_id = {row['id']: row for row in spans}
    joined = []
    for event in events:
        server_span = by_id.get(event['id'])
        row = dict(event)
        if server_span:
            row.update(server_span)
            row['pre_ms'] = (server_span['entry_ns'] - event['client_start_ns']) / 1e6
            row['server_ms'] = (server_span['exit_ns'] - server_span['entry_ns']) / 1e6
            row['post_ms'] = (event['client_end_ns'] - server_span['exit_ns']) / 1e6
        row['total_ms'] = (event['client_end_ns'] - event['client_start_ns']) / 1e6
        joined.append(row)
    assert len(joined) == len(by_id) and all('server_ms' in row for row in joined), 'Missing request ID spans'
    assert all(row['status'] == 200 and row['pre_ms'] >= 0 and row['post_ms'] >= 0 for row in joined), 'Invalid request spans'
    write_json(output / 'requests.json', joined)
    write_json(output / 'resources.json', resources)
    write_json(output / 'loop-lag.json', lag)
    summary = {'pid': server_pid, **counts, 'loop_lag_ms': {'p95': percentile(lag, .95),
        'p99': percentile(lag, .99), 'max': max(lag)},
        'server_loop_lag_ms': {'p95': percentile(server_lag, .95), 'p99': percentile(server_lag, .99),
                               'max': max(server_lag)}, 'cpu_seconds': {}, 'requests': {}}
    for pid in {self_pid for sample in resources for self_pid in
                [process['pid'] for process in sample['processes']]}:
        series = [process['cpu_seconds'] for sample in resources for process in sample['processes']
                  if process['pid'] == pid]
        if len(series) > 1:
            summary['cpu_seconds'][str(pid)] = series[-1] - series[0]
    for label in ['audit-fresh', 'ui-1-keepalive', 'ui-2-keepalive']:
        group = [e for e in joined if e['label'] == label]
        summary['requests'][label] = {'n': len(group), **{part: {
            'median': percentile([e[part] for e in group], .5),
            'p95': percentile([e[part] for e in group], .95),
            'max': max(e[part] for e in group)} for part in ['pre_ms', 'server_ms', 'post_ms', 'total_ms']}}
    return summary


async def main(args):
    manifest = args.source.parent / 'source-manifest.json'
    assert manifest.exists() and args.bundle.joinpath('manifest.json').exists()
    args.output.mkdir(parents=True, exist_ok=False)
    identity = {'source_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                'asset_manifest_sha256': hashlib.sha256((args.bundle / 'manifest.json').read_bytes()).hexdigest(),
                'source': str(args.source), 'bundle': str(args.bundle), 'script_sha256':
                hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'seconds_per_arm': args.seconds,
                'started_utc': now(), 'client_pid': os.getpid(),
                'scope': 'GET /state and local visual fixture with two UI-like pollers; no browser or OCR jobs'}
    write_json(args.output / 'identity.json', identity)
    results = {}
    for name in ('shared', 'split'):
        results[name] = await arm(args, name)
        write_json(args.output / 'summary.json', {'identity': identity, 'arms': results})
    identity['finished_utc'] = now()
    write_json(args.output / 'summary.json', {'identity': identity, 'arms': results})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--bundle', type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument('--python', type=Path, default=DEFAULT_PYTHON)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--server', action='store_true')
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.bundle = args.bundle.resolve()
    args.output = args.output.resolve()
    sys.path[:0] = [str(args.source / 'src'), str(args.source / 'tests')]
    if args.server:
        asyncio.run(server(args))
    else:
        asyncio.run(main(args))

"""Frozen-source synthetic graph/event load and optional timed stability track.

This does not qualify OCR/GPU throughput, real providers or target hardware.
"""
import argparse
import asyncio
import ctypes
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def resource_sample():
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in (
            'peak_working', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile')]
    sample = Counters(); sample.cb = ctypes.sizeof(sample)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    process = kernel.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(sample), sample.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    handles = ctypes.c_ulong()
    if not kernel.GetProcessHandleCount(process, ctypes.byref(handles)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'working_bytes': sample.working, 'peak_working_bytes': sample.peak_working, 'handles': handles.value,
            'cpu_seconds': time.process_time(), 'utc': datetime.now(timezone.utc).isoformat()}


def distribution(values):
    values = sorted(values)
    return {'samples': len(values), 'median_ms': statistics.median(values) * 1000 if values else None,
            'p95_ms': values[min(len(values) - 1, int(len(values) * .95))] * 1000 if values else None,
            'max_ms': max(values) * 1000 if values else None}


async def run(out, soak):
    from ocr_workbench.store import Store
    from ocr_workbench.agent.runtime import AgentRuntime
    from ocr_workbench.agent.providers import normalize_response
    from ocr_workbench.agent.store import canonical, digest
    from ocr_workbench.application_services import ApplicationServices
    from ocr_workbench.agent.selection import create_selection
    from ocr_workbench.agent.context import AgentContext
    from ocr_workbench.agent.contracts import GetWorkspaceContext
    from ocr_workbench.documents import Documents
    store = Store(out / 'workspace')
    project = store.project('Synthetic scale')['id']
    timings = {'cycle': [], 'schedule_to_wait': [], 'reply_to_completion': [], 'cancel': [], 'saver_put': [], 'saver_writes': [], 'events_page': [], 'event_batch': []}
    sources = json.loads((out / 'source-manifest.json').read_text('utf-8'))
    report = {'status': 'running', 'source_snapshot': sources, 'platform': platform.platform(), 'python': sys.version,
              'real_model': 'not_tested', 'ocr_gpu_throughput': 'not_tested', 'target_machine': 'not_tested',
              'track': 'synthetic read / user interrupt+reply / cancel+fencing / persistent event reads',
              'requested_soak_seconds': soak, 'resources': [], 'cycles': 0, 'selection_scales': []}

    async def model(state, current):
        name = 'ask_user' if current['goal'] in {'wait', 'cancel'} else 'get_workspace_context'
        args = {'question': 'Synthetic choice', 'options': ['Continue', 'Stop']} if name == 'ask_user' else {}
        message = {'role': 'assistant', 'content': 'Synthetic completion'}
        if state['step'] == 0:
            message = {'role': 'assistant', 'tool_calls': [{'id': 'first', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
        request, fresh = runtime.agent.begin_model_request(project, current['id'], state['generation'], state['step'], digest(message))
        if not fresh:
            return json.loads(request['normalized_response'])
        reply = normalize_response('openai_chat_completions', {'choices': [{'message': message, 'finish_reason': 'tool_calls' if state['step'] == 0 else 'stop'}]}, request['request_id'])
        runtime.agent.finish_model_request(project, current['id'], state['generation'], request['request_id'], reply)
        return reply

    runtime = await AgentRuntime(ApplicationServices(store, None, SimpleNamespace(guard=store.file_lock)), policy={}, model_override=model).open()
    for method, metric in [('aput', 'saver_put'), ('aput_writes', 'saver_writes')]:
        original = getattr(runtime.checkpoints.saver, method)
        async def measured(*args, _original=original, _metric=metric, **kwargs):
            start = time.perf_counter()
            try:
                return await _original(*args, **kwargs)
            finally:
                timings[_metric].append(time.perf_counter() - start)
        setattr(runtime.checkpoints.saver, method, measured)
    from ocr_workbench.agent.graph import GraphServices, build_graph
    runtime.graph = build_graph(GraphServices(runtime.agent, runtime.registry, model, jobs=runtime.jobs, inbox=runtime.inbox), runtime.checkpoints.saver)

    def save():
        report['timings'] = {name: distribution(values) for name, values in timings.items()}
        report['database_bytes'] = {name: (store.root / name).stat().st_size for name in ['workbench.sqlite3', 'agent-checkpoints.sqlite3']}
        temporary = out / 'performance.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(out / 'performance.json')

    async def cycle(index):
        start = time.perf_counter()
        session = runtime.agent.create_session(project, {'client_request_id': 's-' + str(index), 'title': 'Scale ' + str(index)})
        mode = ['read', 'wait', 'cancel'][index % 3]
        current = runtime.agent.create_run(project, session['id'], {'client_request_id': 'run', 'content': mode}, context={}, config={}, limits={})
        runtime.schedule(current)
        task = runtime.tasks[current['id']]
        await asyncio.wait_for(task, 30)
        if mode in {'wait', 'cancel'}:
            assert runtime.agent.run(project, current['id'])['status'] == 'waiting_user'
            timings['schedule_to_wait'].append(time.perf_counter() - start)
            resumed = time.perf_counter()
            if mode == 'wait':
                decision = store.rows("SELECT * FROM agent_decisions WHERE run_id=? AND status='pending'", (current['id'],))[0]
                runtime.agent.reply_decision(project, current['id'], decision['id'], {'client_request_id': 'reply', 'payload_hash': decision['payload_hash'], 'option_id': 'option-0'})
                runtime.schedule(runtime.agent.run(project, current['id']), resume=True)
                await asyncio.wait_for(runtime.tasks[current['id']], 30)
                timings['reply_to_completion'].append(time.perf_counter() - resumed)
            else:
                await runtime.cancel(project, current['id'], current['generation'], 'stop_agent', request_id='cancel')
                timings['cancel'].append(time.perf_counter() - resumed)
        assert runtime.agent.run(project, current['id'])['status'] == ('cancelled' if mode == 'cancel' else 'completed')
        assert not runtime.observer_error
        timings['cycle'].append(time.perf_counter() - start)
        report['cycles'] = index + 1

    try:
        bulk = runtime.agent.create_session(project, {'client_request_id': 'bulk', 'title': '100k events'})
        for batch in range(100):
            start = time.perf_counter()
            with store.transaction() as db:
                db.execute('BEGIN IMMEDIATE')
                for i in range(batch * 1000, (batch + 1) * 1000):
                    runtime.agent._event(db, bulk['id'], None, None, 'event-' + str(i), 'message', {'role': 'assistant', 'content': 'synthetic ' + str(i)})
            timings['event_batch'].append(time.perf_counter() - start)
            await asyncio.sleep(0)
        report['event_count'] = store.rows('SELECT COUNT(*) n FROM agent_events WHERE session_id=?', (bulk['id'],))[0]['n']
        assert report['event_count'] == 100000
        for offset in [0, 49000, 99500] * 10:
            start = time.perf_counter()
            events = runtime.agent.events(project, bulk['id'], offset, 500)
            timings['events_page'].append(time.perf_counter() - start)
            assert len(events) == 500 and events[0]['seq'] == offset + 1
        for count in (100, 1000):
            with store.transaction() as db:
                doc = 'pdf-' + str(count)
                db.execute("INSERT INTO documents(id,project_id,name,kind,sha256,page_count,status,metadata,created,updated,original_path) VALUES(?,?,?,'pdf',?,?,'ready','{}','now','now','synthetic.pdf')", (doc, project, doc, doc, count))
                Documents._insert_pages(db, doc, [{'page_number': n, 'render_dpi': 72} for n in range(1, count + 1)])
            start = time.perf_counter()
            selected = create_selection(runtime.agent, project, {'document_ranges': [{'document_id': doc, 'first_page': 1, 'last_page': count}]}, available_engines=[])
            context = AgentContext(runtime.agent, None)
            current = {'project_id': project, 'context': {'selection': selected['selection']}}
            cursor, pages, max_wire = None, [], 0
            while True:
                result = context.workspace(current, GetWorkspaceContext(cursor=cursor), {})
                pages.extend(p['page_id'] for p in result['data']['selection']['pages'])
                max_wire = max(max_wire, len(canonical(result).encode('utf-8')))
                cursor = result['next_cursor']
                if cursor is None:
                    break
            assert len(pages) == len(set(pages)) == count and max_wire <= 16384
            report['selection_scales'].append({'pages': count, 'seconds': time.perf_counter() - start, 'max_wire_bytes': max_wire})
        report['resources'].append(resource_sample())
        for index in range(50):
            await cycle(index)
        report['resources'].append(resource_sample())
        report['initial_50_sessions_passed'] = True
        assert timings['saver_put'] and timings['saver_writes'], 'Saver instrumentation did not record real writes'
        started = time.monotonic()
        while time.monotonic() - started < soak:
            await cycle(report['cycles'])
            report['elapsed_soak_seconds'] = time.monotonic() - started
            report['resources'].append(resource_sample())
            save()
            await asyncio.sleep(min(30, max(0, soak - (time.monotonic() - started))))
        report['elapsed_soak_seconds'] = time.monotonic() - started
        report['status'] = 'pass'
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        await runtime.close()
        report['resources'].append(resource_sample())
        save()
    print(json.dumps({'status': report['status'], 'cycles': report['cycles'], 'events': report['event_count'], 'elapsed_soak_seconds': report['elapsed_soak_seconds']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--soak-seconds', type=int, default=0)
    args = parser.parse_args()
    if args.soak_seconds < 0:
        parser.error('soak seconds must be nonnegative')
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    shutil.copytree(ROOT / 'src', out / 'source/src', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    files = {p.relative_to(out / 'source').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (out / 'source/src').rglob('*') if p.is_file()}
    (out / 'source-manifest.json').write_text(json.dumps(files, indent=2), encoding='utf-8')
    sys.path.insert(0, str(out / 'source/src'))
    asyncio.run(run(out, args.soak_seconds))

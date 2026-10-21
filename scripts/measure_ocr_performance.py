"""Record real fixed-input OCR runs; inspect is read-only, collect submits tasks.

Uses existing sessions, operations and native jobs. Cold/warm labels come from
actual model session receipts, never from a warm HTTP server or elapsed time.
No model/device change, preload, process termination or receipt replay is used.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid


TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled'}


def f_path(value):
    requested = Path(value).absolute()
    resolved = requested.resolve()
    if requested.drive.upper() != 'F:' or resolved.drive.upper() != 'F:' or str(resolved).startswith('\\\\'):
        raise ValueError('All inputs, databases and output files must be on local F drive')
    return resolved


def read_rows(path, sql, values=()):
    path = f_path(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, values)]


def seconds(start, end):
    return (datetime.fromisoformat(end.replace('Z', '+00:00')) -
            datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds() if start and end else None


def extract(data, native_db, run_id):
    runs = read_rows(data / 'agent/operations.sqlite3', 'SELECT * FROM runs WHERE id=?', (run_id,))
    if len(runs) != 1:
        raise ValueError('The exact run is not present in the selected isolated DATA')
    run = runs[0]
    state = json.loads(run.get('driver_state') or '{}')
    operations = read_rows(data / 'agent/operations.sqlite3',
                          'SELECT * FROM operations WHERE run_id=? ORDER BY created,rowid', (run_id,))
    events = read_rows(data / 'agent/operations.sqlite3',
                      'SELECT seq,kind,payload,created FROM events WHERE run_id=? ORDER BY seq', (run_id,))
    jobs = {}
    for operation in operations:
        result = json.loads(operation['result']) if operation['result'] else {}
        for ref in result.get('job_refs', []):
            if ref.get('application') == 'ocr':
                rows = read_rows(data / 'jobs.sqlite3', 'SELECT * FROM projected_jobs WHERE platform_job_id=?', (ref['object_id'],))
                for row in rows:
                    jobs[row['platform_job_id']] = row
    native_ids = sorted({identifier for job in jobs.values() for identifier in json.loads(job['native_ids'])})
    samples = []
    for identifier in native_ids:
        task_rows = read_rows(native_db, 'SELECT * FROM tasks WHERE id=?', (identifier,))
        if not task_rows:
            continue
        task = task_rows[0]
        rows = read_rows(native_db, 'SELECT original FROM results WHERE task_id=?', (identifier,))
        original = json.loads(rows[0]['original']) if rows else {}
        session = original.get('engine_session') or {}
        loaded = original.get('load_seconds')
        temperature = ('cold' if isinstance(loaded, (int, float)) and loaded > 0 else
                       'warm' if loaded == 0 and session.get('id') else 'unverified')
        samples.append({'native_task_id': identifier, 'status': task['status'], 'stage': task['phase'],
            'engine': task['engine'], 'engine_package': task.get('engine_package'),
            'created': task['created'], 'started': task['started'], 'finished': task['finished'],
            'queue_wait_seconds': seconds(task['created'], task['started']),
            'native_execution_seconds': seconds(task['started'], task['finished']),
            'request_to_native_created_seconds': seconds(run['created'], task['created']),
            'temperature': temperature, 'engine_session': session,
            'native_load_seconds': loaded, 'worker_processing_seconds': original.get('elapsed_seconds'),
            'worker_stage_timings': original.get('timings'), 'model_revisions': original.get('model_revisions'),
            'image': {key: original.get('image', {}).get(key) for key in ('sha256', 'version', 'width', 'height')},
            'text_chars': len(original.get('text', '')), 'text_sha256': hashlib.sha256(original.get('text', '').encode('utf-8')).hexdigest() if rows else None,
            'quality_verified': False, 'error': task.get('error')})
    deliveries = []
    for event in events:
        payload = json.loads(event['payload'])
        body = payload.get('delivery') or payload
        readable = body.get('readable_text') or (body.get('data') or {}).get('readable_text')
        if readable:
            deliveries.append({'seq': event['seq'], 'created': event['created'], 'kind': event['kind'],
                               'readable_text': readable})
    for task in samples:
        task['native_finished_to_readable_event_seconds'] = (seconds(task['finished'], deliveries[0]['created'])
                                                           if deliveries else None)
    model_requests = read_rows(data / 'agent/operations.sqlite3', 'SELECT COUNT(*) AS n FROM model_requests WHERE run_id=?', (run_id,))[0]['n']
    return {'format': 'clear-ocr-performance-v1', 'run_id': run_id, 'session_id': run['session_id'],
        'workspace_id': run['workspace_id'], 'status': run['status'], 'driver': run.get('driver', 'harness'),
        'source_data': str(data), 'native_database': str(native_db),
        'created': run['created'], 'updated': run['updated'], 'inputs': state.get('inputs', []),
        'saved_stage_timestamps': state.get('timings', {}), 'model_requests': model_requests,
        'operation_count': len(operations), 'event_count': len(events), 'native_tasks': samples,
        'deliveries': deliveries, 'measurements_are_real': True,
        'notes': ['worker_processing_seconds includes input preparation, inference and postprocessing; it is not pure inference.',
                  'Cold/warm describes the actual native model attempt, independently of HTTP/server warmth.',
                  'Quality approval is separate and is never inferred from a successful native status.']}


def summary(records, *, cold_target=5, warm_target=10):
    tasks = [task for record in records for task in record['native_tasks']]
    engines = sorted({task['engine'] for task in tasks})
    source_hashes = sorted({item['blob_hash'] for record in records for item in record['inputs']})
    comparable = len(engines) <= 1 and len(source_hashes) <= 1
    groups = {}
    for temperature in ('cold', 'warm', 'unverified'):
        selected = [task for task in tasks if task['temperature'] == temperature and task['status'] == 'succeeded']
        metrics = {}
        for field in ('queue_wait_seconds', 'native_execution_seconds', 'native_load_seconds',
                      'worker_processing_seconds', 'request_to_native_created_seconds',
                      'native_finished_to_readable_event_seconds'):
            values = [task[field] for task in selected if isinstance(task.get(field), (int, float))]
            metrics[field] = {'n': len(values), 'median': statistics.median(values), 'min': min(values), 'max': max(values)} if values else {'n': 0}
        groups[temperature] = {'successful_samples': len(selected), 'metrics': metrics}
    return {'format': 'clear-ocr-performance-summary-v1', 'groups': groups,
        'targets': {'cold': cold_target, 'warm': warm_target},
        'engines': engines, 'source_hashes': source_hashes, 'same_input_engine_cohort': comparable,
        'sample_quota_met': comparable and groups['cold']['successful_samples'] >= cold_target and groups['warm']['successful_samples'] >= warm_target,
        'quality_accepted': bool(tasks) and all(task.get('quality_verified') for task in tasks),
        'failed_or_unfinished_attempts': sum(task['status'] != 'succeeded' for task in tasks),
        'raw_runs': [record['run_id'] for record in records], 'performance_passed': False,
        'unresolved': ['Pure inference and dependency/weight loading need the new worker stage timings.',
                       'Quality approval and all requested entry-path comparisons must be supplied before a performance pass.']}


def request(base, method, path, body=None):
    headers = {'Origin': base, 'Content-Type': 'application/json'}
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    with urlopen(Request(base + path, data=data, method=method, headers=headers), timeout=60) as response:
        return json.loads(response.read())


def save(path, value):
    path = f_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['inspect', 'collect', 'summarize'])
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--native-db')
    parser.add_argument('--output', required=True)
    parser.add_argument('--run-id', action='append', default=[])
    parser.add_argument('--base-url', default='http://127.0.0.1:8941')
    parser.add_argument('--workspace-id')
    parser.add_argument('--asset-id')
    parser.add_argument('--engine', action='append', default=[])
    parser.add_argument('--cold-target', type=int, default=5)
    parser.add_argument('--warm-target', type=int, default=10)
    parser.add_argument('--max-runs', type=int, default=15)
    parser.add_argument('--timeout-seconds', type=float, default=1200)
    args = parser.parse_args()
    data, output = f_path(args.data_dir), f_path(args.output)
    native_db = f_path(args.native_db or data / 'native/ocr/workbench.sqlite3')
    output.mkdir(parents=True, exist_ok=True)
    records = []
    if args.mode == 'collect':
        url = urlsplit(args.base_url)
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path or url.query or url.fragment:
            parser.error('collect requires an explicitly selected loopback server URL')
        if not args.workspace_id or not args.asset_id:
            parser.error('collect requires the existing isolated workspace and fixed asset')
        for index in range(args.max_runs):
            key = uuid.uuid4().hex
            session = request(args.base_url, 'POST', f'/api/platform/v1/workspaces/{args.workspace_id}/agent/sessions',
                {'name': 'OCR performance ' + str(index + 1), 'selected_assets': [args.asset_id], 'request_id': 'perf-session-' + key})
            command_id = 'perf-run-' + key
            started = time.perf_counter()
            try:
                run = request(args.base_url, 'POST', f'/api/platform/v1/agent/sessions/{session["id"]}/direct/image-text',
                              {'request_id': command_id, 'engines': args.engine})
                accepted = time.perf_counter() - started
            except Exception as error:
                # Inspect the original request id once. A lost response never
                # permits a second POST or a new request id for that sample.
                found = read_rows(data / 'agent/operations.sqlite3', 'SELECT * FROM runs WHERE request_id=?', (command_id,))
                if not found:
                    save(output / f'sample-{index + 1:03d}-unknown.json', {'request_id': command_id,
                        'status': 'receipt_unknown', 'error_type': type(error).__name__, 'automatically_replayed': False})
                    break
                run, accepted = found[0], None
            deadline = time.monotonic() + args.timeout_seconds
            while time.monotonic() < deadline:
                current = read_rows(data / 'agent/operations.sqlite3', 'SELECT status FROM runs WHERE id=?', (run['id'],))[0]
                if current['status'] in TERMINAL or current['status'] in {'needs_verification', 'waiting_user', 'paused'}:
                    break
                time.sleep(2)
            record = extract(data, native_db, run['id'])
            record['http_accepted_seconds'] = accepted
            records.append(record)
            save(output / f'sample-{index + 1:03d}.json', record)
            report = summary(records, cold_target=args.cold_target, warm_target=args.warm_target)
            save(output / 'summary.json', report)
            if record['status'] != 'succeeded' or report['sample_quota_met']:
                break
    else:
        for index, run_id in enumerate(args.run_id, 1):
            record = extract(data, native_db, run_id)
            records.append(record)
            save(output / f'sample-{index:03d}.json', record)
    report = summary(records, cold_target=args.cold_target, warm_target=args.warm_target)
    save(output / 'summary.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

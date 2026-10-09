"""Snapshot live native stages through isolated SQLite bytes and raw loopback API.

This read-only observer reports bounded-stop predicates. It never starts models,
changes deadlines or queue state, reads truth, or opens an original SQLite file.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
import re
import shutil
import sqlite3
import subprocess

from common import frozen_json, read_json
from controlled_recovery import bound, plain_path, require_environment


def processes(run):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', run.name):
        raise ValueError('Unsafe trial identity')
    command = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { "
               "$_.CommandLine -like '*" + run.name + "*' -and ($_.CommandLine -like '*evaluate_screen_agent_native.py*' "
               "-or $_.CommandLine -like '*ocr_workbench.engine_host*') } | ForEach-Object { [pscustomobject]@{ "
               "pid=$_.ProcessId; parent_pid=$_.ParentProcessId; creation_utc=$_.CreationDate.ToUniversalTime().ToString('o'); "
               "command_line=$_.CommandLine; executable_path=$_.ExecutablePath } } | ConvertTo-Json -Depth 10")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], cwd=run.parent,
                            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('Owned runtime presence query failed')
    value = json.loads(result.stdout) if result.stdout.strip() else []
    return value if isinstance(value, list) else [value]


def snapshot(run, output):
    if output.exists():
        raise ValueError('Use a new live-stage snapshot identity')
    output.mkdir(parents=True)
    owned = processes(run)
    frozen_json(output / 'owned-processes.json', owned)
    source = run / 'native/ocr/workbench.sqlite3'
    if not source.exists():
        frozen_json(output / 'result.json', {'status': 'native_database_not_yet_created', 'owned': owned})
        return {'status': 'native_database_not_yet_created'}
    native = output / 'sqlite-copy/native.sqlite3'
    native.parent.mkdir()
    copied = {}
    for suffix in ['', '-wal', '-shm']:
        member = source.with_name(source.name + suffix)
        if member.exists():
            target = native.with_name(native.name + suffix)
            shutil.copyfile(member, target)
            copied[member.name] = bound(target)
    with closing(sqlite3.connect(native.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Live byte copy was not a consistent SQLite snapshot')
        batches = [dict(row) for row in db.execute('SELECT * FROM screen_batches')]
        tasks = [dict(row) for row in db.execute('''SELECT t.*,s.metadata,s.prepared_path,s.spent_seconds FROM tasks t
                 LEFT JOIN screen_tasks s ON s.task_id=t.id WHERE t.kind IN ('ocr','screen_context')''')]
        items = [dict(row) for row in db.execute('SELECT * FROM screen_items')]
    if len(batches) > 1:
        raise ValueError('Observer requires a single uniquely bound original batch')
    primary_tasks = [task for task in tasks if task['engine'] == 'glm' and task['metadata'] is not None]
    finished = sorted([task for task in primary_tasks if task['finished']], key=lambda task: task['finished'])
    recognition_streak = 0
    last_available = None
    readiness_basis = {}
    for task in finished:
        metadata = json.loads(task['metadata'])
        ready = any(event.get('outcome') == 'ready' for event in metadata.get('engine_load_events', []))
        if ready:
            readiness_basis[task['id']] = 'actual_this_task_adapter_ready_returned_event'
        else:
            # Reused resident adapters do not add a second load event. Bind the
            # prior actual readiness to this same frozen model identity. The
            # frozen queue sets recognition failure separately from wrapped
            # EngineLoadTimeout/budget failure, so a generic phase alone is
            # never enough to count a ready recognition timeout.
            prior = []
            end = datetime.fromisoformat(task['finished']).timestamp()
            for other in primary_tasks:
                other_metadata = json.loads(other['metadata'])
                if other_metadata['model_identity'] != metadata['model_identity']:
                    continue
                for event in other_metadata.get('engine_load_events', []):
                    if (event.get('phase') == 'engine_load' and event.get('outcome') in {'ready', 'timeout', 'budget_exhausted'}
                            and datetime.fromisoformat(event['at']).timestamp() <= end):
                        prior.append(event)
            latest = max(prior, key=lambda event: event['ordinal']) if prior else None
            ready = bool(latest and latest['outcome'] == 'ready' and task['phase'] == '识别失败')
            if ready:
                readiness_basis[task['id']] = 'actual_prior_ready_same_frozen_identity_and_distinct_native_recognition_failure_phase'
        if task['status'] == 'succeeded' and task['result_id']:
            recognition_streak = 0
            last_available = task['result_id']
        elif task['status'] == 'failed' and ready and '识别超时' in (task['error'] or ''):
            recognition_streak += 1
        else:
            recognition_streak = 0
    load_events = []
    for task in primary_tasks:
        metadata = json.loads(task['metadata'])
        for event in metadata.get('engine_load_events', []):
            load_events.append({'task_id': task['id'], 'model_identity': metadata['model_identity'], **event})
    cold = {}
    for event in sorted(load_events, key=lambda value: (value['ordinal'], value['task_id'])):
        identity = event['model_identity']
        cold[identity] = 0 if event['outcome'] == 'ready' else cold.get(identity, 0) + 1
    frozen_json(output / 'native-tasks.json', tasks)
    frozen_json(output / 'native-items.json', items)
    api = None
    if batches:
        services = [process for process in owned if '--serve-native' in process['command_line']]
        if len(services) == 1:
            port = int(re.search(r'--port\s+(\d+)', services[0]['command_line']).group(1))
            batch = batches[0]
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
            connection.request('GET', '/api/projects/' + batch['project_id'] + '/screen-batches/' + batch['id'] + '?limit=60',
                               headers={'Authorization': 'Bearer ' + (run / 'native-token.txt').read_text('utf-8')})
            response = connection.getresponse()
            data = response.read(16 * 1024 * 1024 + 1)
            connection.close()
            if response.status != 200 or len(data) > 16 * 1024 * 1024:
                raise ValueError('No bounded successful raw native API reply')
            (output / 'batch-api.json').write_bytes(data)
            api = json.loads(data)
    report = {'format': 'm1-actual-live-stage-snapshot-v1', 'utc': datetime.now(timezone.utc).isoformat(),
              'status': 'observed_actual_stages', 'run': str(run), 'source_sqlite_opened': False,
              'sqlite_byte_copies': copied, 'original_live_db_byte_atomicity_claimed': False,
              'copied_sqlite_integrity_check_passed': True,
              'batch_id': batches[0]['id'] if batches else None,
              'project_id': batches[0]['project_id'] if batches else None,
              'native_item_status_counts': dict(Counter(item['status'] for item in items)),
              'primary_task_status_counts': dict(Counter(task['status'] for task in primary_tasks)),
              'geometry_task_status_counts': dict(Counter(task['status'] for task in tasks if task['engine'] == 'geometry')),
              'actual_ready_recognition_timeout_streak_since_available_result': recognition_streak,
              'actual_readiness_basis_for_finished_tasks': readiness_basis,
              'latest_available_native_ocr_result_id': last_available,
              'cold_load_failure_streaks_by_actual_frozen_identity': cold,
              'bounded_stop_predicate': recognition_streak >= 3 or any(value >= 3 for value in cold.values()),
              'reason': 'three_ready_recognition_timeouts_without_new_available_result' if recognition_streak >= 3
                        else 'three_actual_cold_load_failures_in_same_frozen_identity' if any(value >= 3 for value in cold.values()) else None,
              'raw_api_status': api.get('status') if api else None, 'raw_api_counts': api.get('counts') if api else None,
              'model_or_service_started': False, 'queue_action_performed': False,
              'truth_read': False, 'holdout_read': False, 'formal_current_changed': False}
    frozen_json(output / 'result.json', report)
    return {key: report[key] for key in ['utc', 'native_item_status_counts', 'primary_task_status_counts',
                                       'geometry_task_status_counts', 'actual_ready_recognition_timeout_streak_since_available_result',
                                       'bounded_stop_predicate', 'reason']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    require_environment()
    output = plain_path(args.output)
    try:
        print(snapshot(plain_path(args.run), output), flush=True)
    except Exception as error:
        if output.exists() and not (output / 'failure.json').exists():
            frozen_json(output / 'failure.json', {'format': 'm1-live-observer-failure-v1',
                        'utc': datetime.now(timezone.utc).isoformat(), 'error_type': type(error).__name__,
                        'error': str(error), 'no_success_snapshot_claimed': True,
                        'original_sqlite_opened': False, 'truth_read': False, 'queue_action_performed': False})
        raise


if __name__ == '__main__':
    main()

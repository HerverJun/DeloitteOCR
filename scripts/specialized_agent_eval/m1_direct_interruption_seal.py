"""Seal an externally interrupted direct trial without opening its databases.

All SQLite access uses a second copy after the full stopped runtime is sealed.
Partial observations retain all frozen inputs; missing computations are never
reported as recognition accuracy. No service, model or controller is started.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from common import digest, frozen_json, read_json
from controlled_recovery import (bound, copy_bytes, directory_inventory,
                                 file_inventory, plain_path, require_environment)
from m1_model_receipts import check_sources


def seal(args):
    require_environment()
    run, inputs, output = map(plain_path, (args.run, args.inputs, args.output))
    if output.exists() or (run / 'result.json').exists():
        raise ValueError('Requires an unclosed original and new seal identity')
    command = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
               "Where-Object { $_.ProcessId -ne " + str(os.getpid()) +
               " -and $_.CommandLine -like '*" + run.name + "*' } | "
               "Select-Object ProcessId,ParentProcessId,CreationDate | ConvertTo-Json -Compress")
    processes = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
        cwd=run.parent, capture_output=True, text=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if processes.returncode or processes.stdout.strip():
        raise ValueError('Original model/service/runner is still live or presence is uncertain')
    sources = check_sources(run)
    manifest = read_json(inputs)
    if (manifest.get('truth_available_to_inference') is not False
            or digest(inputs) != read_json(run / 'prepared.json')['inputs_sha256']):
        raise ValueError('Frozen truth-free inputs differ')
    files, directories = file_inventory(run), directory_inventory(run)
    copy_bytes(run, output / 'runtime', files, directories)
    db_dir = output / 'sqlite-review'
    db_dir.mkdir()
    native = output / 'runtime/native/ocr/workbench.sqlite3'
    for name in ('workbench.sqlite3', 'workbench.sqlite3-wal', 'workbench.sqlite3-shm'):
        if (native.parent / name).exists():
            shutil.copyfile(native.parent / name, db_dir / name)
            if digest(native.parent / name) != digest(db_dir / name):
                raise ValueError('SQLite review copy differs')
    with closing(sqlite3.connect(db_dir / 'workbench.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        items = {row['source_hash']: dict(row) for row in db.execute('SELECT * FROM screen_items')}
        results = {row['id']: dict(row) for row in db.execute('SELECT * FROM results')}
        tasks = {row['id']: dict(row) for row in db.execute('SELECT * FROM tasks')}
        screen = [dict(row) for row in db.execute('SELECT * FROM screen_tasks')]
        batches = [dict(row) for row in db.execute('SELECT * FROM screen_batches')]
    by_input = {}
    for row in screen:
        by_input.setdefault(row['input_version_id'], []).append(tasks[row['task_id']])
    if len(items) != len(manifest['samples']) or len(batches) != 1:
        raise ValueError('Interrupted scope lacks its original complete source denominator')
    records = []
    for sample in manifest['samples']:
        if digest(sample['image']) != sample['image_sha256']:
            raise ValueError('Frozen input bytes changed')
        item = items[sample['image_sha256']]
        model_tasks = by_input.get(item['input_version_id'], [])
        actual = results.get(item['result_id'])
        if item['status'] in {'completed', 'needs_review'} and actual:
            status = 'success'
        elif item['status'] in {'failed', 'cancelled'}:
            status = item['status']
        elif any(task['status'] in {'running', 'succeeded'} for task in model_tasks):
            status = 'interrupted_unknown'
        else:
            status = 'not_attempted'
        records.append({'id': sample['id'], 'image_sha256': sample['image_sha256'],
            'input_version_id': item['input_version_id'], 'result_id': item['result_id'],
            'result_revision': item['result_revision'], 'native_status': item['status'],
            'status': status, 'review_image': True, 'review_regions': [],
            'review_state': item['review_state'], 'human_confirmed': False,
            'automatic_delivery': False, 'candidate': {
                'original': json.loads(actual['original']) if actual else {},
                'edited': json.loads(actual['edited']) if actual else {}},
            'actual_native_model_tasks': [{key: task[key] for key in
                ('id', 'engine', 'kind', 'status', 'started', 'finished', 'error')}
                for task in model_tasks], 'error': item['error']})
    if file_inventory(run) != files or directory_inventory(run) != directories:
        raise ValueError('Original interrupted bytes changed during sealing')
    observations = {'format': 'specialized-agent-eval-v1',
        'evidence_kind': 'native_model_receipts', 'inputs_sha256': digest(inputs),
        'records': records, 'partial_run': True, 'truth_read': False,
        'complete_engine_comparison': False, 'default_route_selection_eligible': False,
        'termination_evidence': 'Original process tree and unified exec handle absent after external interruption; no normal terminal result',
        'unavailable_output_recognition_accuracy_estimated': False}
    frozen_json(output / 'observations.json', observations)
    frozen_json(output / 'result.json', {'format': 'm1-direct-interruption-byte-seal-v1',
        'utc': datetime.now(timezone.utc).isoformat(), 'status': 'partial_sealed_before_truth',
        'original_run': str(run), 'sources': sources, 'runtime_files': files,
        'runtime_directories': directories, 'source_sqlite_opened': False,
        'original_unchanged': True, 'owned_processes_present': False,
        'normal_exit_observed': False, 'normal_summary_created': False,
        'original_batch': batches[0], 'scope_inputs': len(records),
        'scope_status_counts': dict(Counter(row['status'] for row in records)),
        'observations': bound(output / 'observations.json'), 'helper': bound(__file__),
        'truth_read': False, 'holdout_read': False, 'model_calls': 0,
        'agent_calls': 0, 'qualification': False})
    print(dict(Counter(row['status'] for row in records)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'inputs', 'output'):
        parser.add_argument('--' + name, required=True)
    seal(parser.parse_args())

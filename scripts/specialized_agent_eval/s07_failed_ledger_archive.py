"""Read-only archive of a failed actual Agent ledger; never changes run status."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
import sqlite3
from common import digest, fpath, frozen_json
from s07_native_read import cold_copy, inventory


def archive(original_run, run_id, output):
    original, root = map(fpath, (original_run, output))
    if root.exists():
        raise ValueError('Failure archive requires a new immutable F: directory')
    before = inventory(original)
    database = original / 'platform/agent/operations.sqlite3'
    root.mkdir(parents=True)
    copied = root / 'original-operations.sqlite3'
    qualification = cold_copy(database, copied)
    frozen_json(root / 'database-copy-qualification.json', qualification)
    with closing(sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        ledger = {}
        for table in ('runs', 'operations', 'events', 'model_requests'):
            key = 'id' if table == 'runs' else 'run_id'
            rows = [dict(row) for row in connection.execute('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (run_id,))]
            if table in ('operations', 'model_requests'):
                rows.sort(key=lambda row: row['created'])
            elif table == 'events':
                rows.sort(key=lambda row: row['seq'])
            ledger[table] = rows
            frozen_json(root / (table + '-original-rows.json'), rows)
    operations = [{**{key: row[key] for key in ('id', 'call_id', 'tool', 'state', 'created', 'updated')},
        'arguments': json.loads(row['arguments']), 'receipt': json.loads(row['result']) if row['result'] else None}
        for row in ledger['operations']]
    frozen_json(root / 'actual-agent-operations.json', operations)
    frozen_json(root / 'actual-controller-requests.json', ledger['model_requests'])
    frozen_json(root / 'delivery-and-export-review.json', [entry for entry in operations
        if entry['tool'] in {'data_export', 'delivery_publish'}])
    unchanged = inventory(original) == before
    if not unchanged:
        raise ValueError('Original failed run changed during read-only archive')
    status = ledger['runs'][0]['status']
    report = {'format': 's07-failed-original-ledger-archive-v1', 'status': 'archived',
        'original_run': str(original), 'run_id': run_id, 'original_terminal_status': status,
        'original_run_files': before, 'original_files_unchanged': unchanged,
        'original_terminal_status_rewritten': False, 'archive_files': inventory(root),
        'operation_count': len(operations), 'model_request_count': len(ledger['model_requests']),
        'delivery_count': sum(entry['tool'] == 'delivery_publish' for entry in operations),
        'helper_sha256': digest(__file__), 'controller_calls': 0, 'gpu_calls': 0}
    frozen_json(root / 'archive.json', report)
    print(status, len(operations), len(ledger['model_requests']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original-run', 'run-id', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    archive(args.original_run, args.run_id, args.output)

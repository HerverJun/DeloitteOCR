"""Independently reconcile a stopped direct OCR run with native DB and ZIP.

Only copied SQLite files are opened. This is a truth-free source and delivery
check; quality scoring must run separately after this result is sealed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3

from common import digest, fpath, frozen_json, read_json
from freeze_sources import PARTS, inventory
from readback import check_bundle


def review(args):
    run, source, inputs, output = map(fpath,
        (args.run, args.source, args.inputs, args.output))
    if output.exists():
        raise ValueError('Use a new independent readback identity')
    result = read_json(run / 'result.json')
    prepared = read_json(run / 'prepared.json')
    trial = read_json(source / 'trial-source-binding.json')
    manifest = read_json(inputs)
    if (result['status'] != 'terminal' or result['counts']['pending'] != 0
            or result['scope_inputs'] != len(manifest['samples'])
            or result['source_binding_sha256'] != digest(source / 'trial-source-binding.json')
            or result['prepared_sha256'] != digest(run / 'prepared.json')
            or result['observations_sha256'] != digest(run / 'observations.json')
            or prepared['inputs_sha256'] != digest(inputs)
            or manifest.get('truth_available_to_inference') is not False
            or trial['actual_output'] != str(run)):
        raise ValueError('Run/source/split is not a complete stopped direct model trial')
    for family in ('ocr', 'platform', 'classification'):
        if inventory(source / family, PARTS[family]) != trial['sources'][family]['files']:
            raise ValueError('Frozen source changed: ' + family)
    observations = read_json(run / 'observations.json')
    if (observations['evidence_kind'] != 'native_direct_platform_receipts'
            or observations['inputs_sha256'] != digest(inputs)
            or len(observations['records']) != len(manifest['samples'])):
        raise ValueError('Observations are not the exact direct run scope')
    samples = {item['id']: item for item in manifest['samples']}
    records = {item['id']: item for item in observations['records']}
    if set(samples) != set(records):
        raise ValueError('Native observations omit or add source images')
    source_db = run / 'native/ocr/workbench.sqlite3'
    db_names = [name for name in ('workbench.sqlite3', 'workbench.sqlite3-wal',
                                  'workbench.sqlite3-shm')
                if (source_db.parent / name).is_file()]
    before = {name: digest(source_db.parent / name) for name in db_names}
    output.mkdir(parents=True)
    for name in db_names:
        shutil.copyfile(source_db.parent / name, output / name)
        if digest(output / name) != before[name]:
            raise ValueError('Native database byte copy differs')
    if {name: digest(source_db.parent / name) for name in db_names} != before:
        raise ValueError('Original native database changed during byte copy')
    with closing(sqlite3.connect(output / 'workbench.sqlite3')) as database:
        database.row_factory = sqlite3.Row
        items = {row['input_version_id']: dict(row) for row in
                 database.execute('SELECT * FROM screen_items')}
        results = {row['id']: dict(row) for row in database.execute('SELECT * FROM results')}
        tasks = [dict(row) for row in database.execute('SELECT * FROM tasks')]
        contexts = [dict(row) for row in database.execute('SELECT * FROM screen_contexts')]
        screen_tasks = [dict(row) for row in database.execute('SELECT * FROM screen_tasks')]
        batches = [dict(row) for row in database.execute('SELECT * FROM screen_batches')]
    if (len(items) != len(samples) or len(batches) != 1
            or batches[0]['id'] != result['batch_id'] or len(contexts) != len(samples)):
        raise ValueError('Native DB batch/context denominator differs')
    details = []
    status_counts = {}
    for image_id, sample in samples.items():
        record = records[image_id]
        detail_path = run / 'native-receipts' / (image_id + '.json')
        detail = read_json(detail_path)
        item = items.get(record['input_version_id'])
        if (item is None or sample['image_sha256'] != record['image_sha256']
                or detail['source_image_sha256'] != sample['image_sha256']
                or detail['input_version_id'] != record['input_version_id']
                or not (detail['status'] == record['status'] == item['status'])
                or not (detail['result_id'] == record['result_id'] == item['result_id'])
                or not (detail['result_revision'] == record['result_revision'] == item['result_revision'])
                or item['source_hash'] != sample['image_sha256']
                or not (detail['review_state'] == record['review_state'] == item['review_state'])):
            raise ValueError('Actual native item/detail/observation identity differs: ' + image_id)
        actual = results.get(record['result_id'])
        view = detail.get('result') or {}
        if record['result_id']:
            if (actual is None or view.get('id') != actual['id']
                    or json.loads(actual['original']) != record['candidate']['original']
                    or json.loads(actual['edited']) != record['candidate']['edited']
                    or view.get('original') != record['candidate']['original']
                    or (view.get('edited') or {}) != record['candidate']['edited']):
                raise ValueError('Actual native result differs from detail or observation: ' + image_id)
        elif view or record['candidate']['original']:
            raise ValueError('No-result item has invented candidate: ' + image_id)
        status_counts[item['status']] = status_counts.get(item['status'], 0) + 1
        details.append({'id': image_id, 'image_sha256': sample['image_sha256'],
                        'input_version_id': item['input_version_id'],
                        'status': item['status'], 'result_id': item['result_id'],
                        'detail_sha256': digest(detail_path)})
    task_by_id = {task['id']: task for task in tasks}
    model_by_input = {row['input_version_id']: task_by_id[row['task_id']]
                      for row in screen_tasks}
    context_by_input = {row['input_version_id']: task_by_id[row['task_id']]
                        for row in contexts}
    if (len(screen_tasks) != len(samples) or len(model_by_input) != len(samples)
            or len(context_by_input) != len(samples)
            or set(model_by_input) != {row['input_version_id'] for row in records.values()}
            or set(context_by_input) != set(model_by_input)
            or any(row['batch_id'] != result['batch_id'] for row in screen_tasks + contexts)
            or any(task['kind'] != 'ocr' or task['engine'] != trial['policy']['primary_engine']
                   or task['status'] != 'succeeded' for task in model_by_input.values())
            or any(task['kind'] != 'screen_context' or task['engine'] != 'geometry'
                   or task['status'] != 'succeeded' for task in context_by_input.values())):
        raise ValueError('Native model/context task ledger does not bind every source image')
    native_model = list(model_by_input.values())
    if result['export'] is None:
        readback = None
    else:
        export = read_json(run / 'export-receipt.json')
        if result['export'] != export or export['zip_sha256'] != digest(run / 'delivered.zip'):
            raise ValueError('Current export receipt or ZIP changed')
        options = {'include_pending_review': True, 'numeric_columns': None}
        def vector(value):
            return {**{key: value[key] for key in
                        ('snapshot_id', 'batch_id', 'project_id', 'policy_sha256', 'items')},
                    'export_options': options}
        snapshot = {'batch_id': result['batch_id'], 'policy': result['policy'],
            'zip_sha256': export['zip_sha256'], 'items': [] ,
            'version_vector': vector(export['snapshot_before']),
            'current_version_vector': vector(export['snapshot_after'])}
        for image_id in samples:
            detail = read_json(run / 'native-receipts' / (image_id + '.json'))
            snapshot['items'].append({'id': image_id,
                'input_version_id': detail['input_version_id'],
                'image_sha256': samples[image_id]['image_sha256'],
                'image_id': detail['image_id'], 'source_name': detail['source_name'],
                'status': detail['status'], 'result_id': detail['result_id'],
                'result_revision': detail['result_revision'],
                'review_state': detail['review_state'],
                'human_confirmed': detail.get('human_confirmed', False),
                'candidate': {'original': (detail.get('result') or {}).get('original') or {},
                              'edited': (detail.get('result') or {}).get('edited') or {}}})
        frozen_json(output / 'readback-snapshot.json', snapshot)
        readback = check_bundle(run / 'delivered.zip', snapshot)
        frozen_json(output / 'xlsx-readback.json', readback)
        if readback['status'] != 'passed' or readback['freshness_status'] != 'passed':
            raise ValueError('Actual ZIP/XLSX source or freshness readback failed')
    scoring_records = []
    for record in observations['records']:
        native_status = record['status']
        scored_status = ('success' if native_status in {'completed', 'needs_review'}
                         and record['result_id'] else
                         'failed' if native_status in {'failed', 'cancelled'} else native_status)
        scoring_records.append({**record, 'native_status': native_status,
                                'status': scored_status})
    verified_observations = {**observations,
        'evidence_kind': 'native_model_receipts',
        'records': scoring_records,
        'status_adapter': 'native completed/needs_review with persisted result -> scoring success; native_status retained',
        'underlying_observations_sha256': digest(run / 'observations.json'),
        'native_database_source_sha256': before,
        'verification': 'every image bound to succeeded native OCR and geometry tasks, result DB, detail receipt and XLSX'}
    frozen_json(output / 'verified-observations.json', verified_observations)
    report = {'format': 'm1-direct-model-independent-readback-v1',
        'status': 'passed', 'scope_inputs': len(samples), 'evaluation_split': trial['evaluation_split'],
        'run_result_sha256': digest(run / 'result.json'),
        'prepared_sha256': digest(run / 'prepared.json'),
        'observations_sha256': digest(run / 'observations.json'),
        'verified_observations_sha256': digest(output / 'verified-observations.json'),
        'source_binding_sha256': digest(source / 'trial-source-binding.json'),
        'native_database_source_sha256': before,
        'native_database_opened_only_from_copy': True,
        'native_context_rows': len(contexts), 'native_model_tasks': len(native_model),
        'native_model_task_states': {state: sum(task['status'] == state for task in native_model)
                                     for state in sorted({task['status'] for task in native_model})},
        'status_counts': status_counts, 'details': details,
        'xlsx_readback': {'status': readback['status'], 'inputs': readback['inputs'],
                          'workbooks': readback['workbooks'], 'no_book': readback['no_book']}
                          if readback else None,
        'truth_read': False, 'agent_calls': 0, 'original_run_mutated': False,
        'final_agent_qualification': False, 'holdout_read': False}
    frozen_json(output / 'review.json', report)
    print({'status': 'passed', 'inputs': len(samples),
           'native_model_tasks': len(native_model), 'xlsx': readback['workbooks'] if readback else 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'source', 'inputs', 'output'):
        parser.add_argument('--' + name, required=True)
    review(parser.parse_args())

"""Seal and independently inspect an externally stopped native model trial.

This adapter has an explicit partial identity and never creates a normal
Agent/native summary. Original SQLite files are copied as bytes. Only separate
F-drive copies are opened, and truth is a later, separate scoring command.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3

from common import FORMAT, digest, fingerprint, frozen_json, indexed, read_json
from controlled_recovery import bound, copy_bytes, directory_inventory, file_inventory, plain_path, require_environment
from m1_model_receipts import check_sources, copied_database, owned_processes


def read_ps_json(path):
    return json.loads(plain_path(path).read_text('utf-8-sig'))


def receipt_timestamps(value):
    """PowerShell REST parsing changes only created/updated timezone spelling."""
    if isinstance(value, list):
        return [receipt_timestamps(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: datetime.fromisoformat(item).astimezone(timezone.utc).isoformat()
            if key in {'created', 'updated'} and isinstance(item, str)
            else receipt_timestamps(item) for key, item in value.items()}


def seal(run, stop, output):
    if output.exists():
        raise ValueError('Use a new partial seal identity')
    termination = read_ps_json(stop / 'native-termination-receipt-002.json')
    exit_observation = read_ps_json(stop / 'harness-exit-observed.json')
    paused = read_ps_json(stop / 'batch-after-pause.json')
    if (termination['normal_shutdown'] is not False
            or termination['native_and_workers_absent_after_action'] is not True
            or termination['other_processes_targeted'] is not False
            or exit_observation['observed_exit_code'] != 1
            or paused['status'] != 'paused' or paused['counts']['total'] != 60):
        raise ValueError('Actual controlled partial-stop evidence differs')
    if owned_processes(run):
        raise ValueError('Owned runtime processes remain')
    sources = check_sources(run)
    inventories = {}
    for name, origin in [('runtime', run), ('stop-evidence', stop)]:
        files, directories = file_inventory(origin), directory_inventory(origin)
        copy_bytes(origin, output / name, files, directories)
        if file_inventory(origin) != files or directory_inventory(origin) != directories:
            raise ValueError('Original stopped bytes changed during seal')
        inventories[name] = {'source': str(origin), 'files': files, 'directories': directories}
    helper_bindings = {}
    for helper in [Path(__file__), *(Path(__file__).parent / name for name in
                   ['m1_model_receipts.py', 'common.py', 'controlled_recovery.py',
                    'native_bridge.py', 'quality.py']), Path('F:/OCR/scripts/screen_native_report.py')]:
        destination = output / 'helper-source' / helper.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(helper, destination)
        if digest(helper) != digest(destination):
            raise ValueError('Helper source byte copy differs')
        helper_bindings[helper.name] = bound(destination)
    report = {'format': 'm1-model-partial-terminal-byte-seal-v1',
              'utc': datetime.now(timezone.utc).isoformat(), 'status': 'partial_sealed_before_truth',
              'raw_run': str(run), 'originals': inventories, 'sources': sources,
              'helpers': helper_bindings, 'observed_harness_exit_code': 1,
              'normal_harness_summary_created': False, 'owned_processes_remaining': [],
              'terminal_runtime_byte_set': True, 'batch_execution_complete': False,
              'truth_read': False, 'holdout_read': False, 'source_sqlite_opened': False,
              'model_or_service_started': False, 'formal_current_changed': False,
              'same_source_performance_comparison': False, 'final_agent_qualification': False}
    frozen_json(output / 'result.json', report)
    print({'status': report['status'], 'runtime_files': len(inventories['runtime']['files']),
           'scope': sources['scope_inputs']})


def verify_seal(seal_root):
    value = read_json(seal_root / 'result.json')
    if value.get('status') != 'partial_sealed_before_truth' or value['truth_read'] is not False:
        raise ValueError('Requires explicitly partial truth-isolated terminal seal')
    run = plain_path(value['raw_run'])
    if owned_processes(run):
        raise ValueError('Owned runtime restarted')
    for name, entry in value['originals'].items():
        for origin in [plain_path(entry['source']), seal_root / name]:
            if file_inventory(origin) != entry['files'] or directory_inventory(origin) != entry['directories']:
                raise ValueError('Original or sealed evidence changed: ' + name)
    check_sources(run)
    return value, run


def collect(seal_root, inputs_path, output):
    sealed, run = verify_seal(seal_root)
    if output.exists():
        raise ValueError('Use a new independent partial collection identity')
    prepared = read_json(run / 'prepared.json')
    inputs = read_json(inputs_path)
    if (inputs.get('truth_available_to_inference') is not False
            or prepared['inputs_sha256'] != digest(inputs_path)
            or plain_path(prepared['inputs']) != inputs_path
            or prepared.get('truth_read') is not False
            or prepared.get('frozen_results_loaded') is not False):
        raise ValueError('Truth-free actual input binding changed')
    samples = indexed(inputs['samples'], 'id', 'frozen input')
    mapping = indexed(read_json(run / 'input-map.json')['items'], 'sample_id', 'import mapping')
    if set(mapping) != set(samples) or len(samples) != prepared['input_count']:
        raise ValueError('Partial adapter must retain the full actual input denominator')
    native, native_bindings = copied_database(run, seal_root, output, 'native/ocr/workbench.sqlite3', 'native.sqlite3')
    agent, agent_bindings = copied_database(run, seal_root, output, 'agent/operations.sqlite3', 'agent.sqlite3')
    spec = importlib.util.spec_from_file_location('m1_partial_identity_helpers', Path('F:/OCR/scripts/screen_native_report.py'))
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    if adapter.source_inventory(prepared['native_source']) != prepared['native_source_files']:
        raise ValueError('Frozen native inventory changed')
    if digest(plain_path(prepared['bundle']) / 'config/engines.json') != prepared['bundle_engines_sha256']:
        raise ValueError('Actual model engine configuration changed')
    tables = adapter.read_database(native)
    with closing(sqlite3.connect(native.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        actions = [dict(row) for row in db.execute('SELECT * FROM screen_actions')]
    batches = indexed(tables['screen_batches'], 'id', 'native batch')
    if len(batches) != 1:
        raise ValueError('Partial report requires one original batch')
    batch_id, batch = next(iter(batches.items()))
    policy = adapter.object_value(batch['policy'])
    adapter.compare_policy(prepared['screen_policy'], policy)
    if (batch['status'] != 'paused' or not batch['paused_at']
            or abs(batch['deadline'] - (datetime.fromisoformat(batch['created']).timestamp()
                                       + policy['batch_budget_seconds'])) > .01):
        raise ValueError('Original paused deadline/budget was changed')
    stop_root = seal_root / 'stop-evidence'
    request = read_ps_json(stop_root / 'pause-request.json')
    pause_actions = [a for a in actions if a['batch_id'] == batch_id and a['request_id'] == request['request_id']]
    if (len(pause_actions) != 1 or pause_actions[0]['payload_hash'] != adapter.fingerprint(request)
            or receipt_timestamps(json.loads(pause_actions[0]['receipt']))
            != receipt_timestamps(read_ps_json(stop_root / 'pause-lookup-after.json'))):
        raise ValueError('Pause action DB identity does not match the authenticated lookup')
    captured = read_ps_json(stop_root / 'batch-after-pause.json')
    api_items = indexed(captured['items'], 'input_version_id', 'paused API item')
    versions = indexed(tables['versions'], 'id', 'native version')
    images = indexed(tables['images'], 'name', 'native imported filename')
    results = indexed(tables['results'], 'id', 'native result')
    tasks = indexed(tables['tasks'], 'id', 'native task')
    items = indexed(tables['screen_items'], 'input_version_id', 'native batch item')
    links = indexed(tables['screen_tasks'], 'task_id', 'native OCR link')
    final_links = tables['screen_final_tasks']
    if len(items) != len(samples) or set(api_items) != set(items):
        raise ValueError('Full paused API/DB scopes differ')
    rows, attempted_tasks = [], []
    for ident, sample in samples.items():
        imported = mapping[ident]
        original = plain_path(sample['image'])
        copy = plain_path(imported['normalized_input'])
        if (digest(original) != sample['image_sha256']
                or imported['source_image_sha256'] != sample['image_sha256']
                or digest(copy) != imported['source_image_sha256']
                or not copy.is_relative_to(run / 'inputs')):
            raise ValueError('Actual source/import bytes changed: ' + ident)
        image = images.get(copy.name)
        if image is None:
            raise ValueError('Imported native image has no unique filename binding')
        version_id = image['active_version']
        version, item = versions[version_id], items[version_id]
        api = api_items[version_id]
        if (image['sha256'] != sample['image_sha256'] or image['id'] != version['image_id']
                or image['id'] != item['image_id'] or image['project_id'] != batch['project_id']
                or item['batch_id'] != batch_id or item['source_hash'] != version['sha256']
                or item['source_name'] != copy.name
                or any(api.get(k) != item.get(k) for k in ('status', 'result_id', 'result_revision', 'review_state'))):
            raise ValueError('Paused native input/result identity changed')
        workspace = run / 'native/ocr'
        native_original = plain_path(workspace / image['original_path'])
        native_image = plain_path(workspace / version['path'])
        if (not native_original.is_relative_to(workspace) or not native_image.is_relative_to(workspace)
                or digest(native_original) != image['sha256'] or digest(native_image) != version['sha256']):
            raise ValueError('Native image byte binding changed')
        visible = adapter.normalized_pixels(original)
        if (adapter.normalized_pixels(native_image) != visible
                or (version['width'], version['height']) != (visible['width'], visible['height'])):
            raise ValueError('Actual native pixels/dimensions differ')
        recognition_tasks = [tasks[link['task_id']] for link in links.values()
                             if link['input_version_id'] == version_id]
        started = [task for task in recognition_tasks if task.get('started')]
        native_status = item['status']
        status = ('success' if native_status in {'completed', 'needs_review'} else
                  'failed' if native_status in {'failed', 'cancelled'} else
                  'interrupted_unknown' if started else 'not_attempted')
        row = {'id': ident, 'input_sha256': sample['image_sha256'], 'input_version_id': version_id,
               'native_version_sha256': version['sha256'], 'native_status': native_status,
               'scoring_status': status, 'normalized_visible_pixels': visible,
               'review_state': item['review_state'], 'human_accepted': item['review_state'] == 'accepted',
               'issues': json.loads(item['issues']), 'error': item['error'],
               'result_id': item['result_id'], 'selected_candidate': None, 'edited_view': None,
               'actual_recognition_started': bool(started)}
        for task in started:
            link = links[task['id']]
            metadata = adapter.object_value(link['metadata'])
            crop = plain_path(workspace / link['prepared_path'])
            if (not crop.is_relative_to(workspace) or plain_path(metadata['path']) != crop
                    or digest(crop) != metadata['prepared_pixel_sha256']):
                raise ValueError('Actual OCR crop pixels/path binding changed')
            attempted_tasks.append({'sample_id': ident, 'task': deepcopy(task), 'screen_task': deepcopy(link),
                                    'prepared_width': metadata['width'], 'prepared_height': metadata['height'],
                                    'engine_load_events': metadata.get('engine_load_events', []), 'crop_file': bound(crop)})
        if item['result_id']:
            result = results[item['result_id']]
            task = tasks[result['task_id']]
            if (status != 'success' or result['revision'] != item['result_revision']
                    or task['kind'] != 'screen_final' or task['status'] != 'succeeded'
                    or task['result_id'] != result['id'] or task['image_id'] != image['id']
                    or task['input_version_id'] != version_id
                    or sum(f['task_id'] == task['id'] and f['input_version_id'] == version_id
                           and f['batch_id'] == batch_id for f in final_links) != 1):
                raise ValueError('Actual final result/task/revision binding differs')
            raw, edited = adapter.object_value(result['original']), adapter.object_value(result['edited'])
            screen = raw['screen']
            selection = screen['selection']
            if (raw.get('origin') != 'screen_batch' or raw.get('project_image_version') != version_id
                    or screen['batch_id'] != batch_id or screen['input_sha256'] != version['sha256']
                    or screen['strategy_version'] != policy['strategy_version']):
                raise ValueError('Actual final source/strategy binding differs')
            chosen = [c for c in json.loads(item['candidates']) if c['id'] == selection['selected_id']]
            if (len(chosen) != 1 or chosen[0]['engine'] != raw['engine']
                    or selection.get('modifications')
                    or any(raw.get(k) != chosen[0]['raw'].get(k) for k in ('text', 'tables'))):
                raise ValueError('Final content differs from unmodified actual selected candidate')
            sources = screen.get('sources', [])
            if not sources:
                raise ValueError('Actual final result has no bound OCR source receipts')
            for source in sources:
                source_result, source_task = results[source['result_id']], tasks[source['task_id']]
                link = links[source_task['id']]
                source_raw = adapter.object_value(source_result['original'])
                if (source_result['task_id'] != source_task['id'] or source_task['status'] != 'succeeded'
                        or source_task['result_id'] != source_result['id'] or source_result['revision'] != source['revision']
                        or link['batch_id'] != batch_id or link['input_version_id'] != version_id
                        or link['prepared_id'] != source['prepared_id'] or source_task['engine'] != source['engine']
                        or source_raw['project_image_version'] != version_id
                        or source_raw.get('screen_input') != adapter.object_value(link['metadata'])
                        or source.get('prepared_metadata') != adapter.object_value(link['metadata'])):
                    raise ValueError('Selected OCR task/crop/version binding differs')
            final_link = next(f for f in final_links if f['task_id'] == task['id'])
            raw_hash = hashlib.sha256(json.dumps(raw, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            if final_link['content_hash'] != raw_hash:
                raise ValueError('Actual final content hash differs')
            def score_view(view):
                value = deepcopy(view)
                value['text'] = '\n'.join([str(value.get('text', '')),
                                           *(str(m.get('text', '')) for m in screen.get('business_metadata', []))])
                value['quality'] = {'automatic_delivery': bool(selection.get('automatic_delivery')) and native_status == 'completed',
                                    'review_required': bool(selection.get('review_required')), 'issues': row['issues']}
                return value
            row.update(result_revision=result['revision'], selected_candidate=score_view(raw), edited_view=score_view(edited),
                       selected_candidate_id=selection['selected_id'], source_receipts=sources,
                       result_original_sha256=adapter.fingerprint(raw), result_edited_sha256=adapter.fingerprint(edited))
        elif status == 'success':
            raise ValueError('Successful partial row has no actual final result')
        rows.append(row)
    index = {'format': 'actual-native-offline-receipt-index-v1', 'native_flavor': 'explicit_partial_agent',
             'native_run': str(run), 'scope_inputs': len(rows), 'inputs_sha256': digest(inputs_path),
             'prepared_sha256': digest(run / 'prepared.json'), 'truth_opened': False,
             'native_database': str(native), 'native_table_identity_sha256': adapter.table_identity(tables),
             'actual_policy': policy, 'batch_id': batch_id, 'rows': rows,
             'normal_agent_summary_available': False, 'partial_terminal_seal': bound(seal_root / 'result.json')}
    frozen_json(output / 'observations.native-index.json', index)
    from native_bridge import normalize_native_index
    observations = normalize_native_index(index)
    observations.update(partial_run=True, termination_evidence=bound(seal_root / 'result.json'),
                        complete_engine_comparison=False, default_route_selection_eligible=False,
                        native_index_sha256=digest(output / 'observations.native-index.json'),
                        original_sqlite_opened=False, diagnostics_cpu_replay_performed=False,
                        localization_qualification=False)
    frozen_json(output / 'observations.json', observations)
    with closing(sqlite3.connect(agent.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        runs = [dict(row) for row in db.execute('SELECT * FROM runs')]
        operations = [dict(row) for row in db.execute('SELECT id,tool,state,result FROM operations ORDER BY created')]
        model_requests = [dict(row) for row in db.execute('SELECT * FROM model_requests')]
    report = {'format': 'm1-model-partial-independent-collection-v1', 'utc': datetime.now(timezone.utc).isoformat(),
              'seal': bound(seal_root / 'result.json'), 'collector': bound(__file__),
              'observations': bound(output / 'observations.json'), 'scope_inputs': len(rows),
              'native_status_counts': dict(Counter(r['native_status'] for r in rows)),
              'scoring_status_counts': dict(Counter(r['scoring_status'] for r in rows)),
              'batch': batch, 'attempted_recognition_tasks': attempted_tasks,
              'agent_runs': runs, 'agent_operations': operations, 'model_request_count': len(model_requests),
              'agent_delivery_publish_count': sum(o['tool'] == 'delivery_publish' and o['state'] == 'completed' for o in operations),
              'verified_agent_delivery': False, 'actual_xlsx_export_observed': False,
              'sqlite_byte_sources': {'native': native_bindings, 'agent': agent_bindings},
              'original_sqlite_opened': False, 'truth_read': False, 'holdout_read': False,
              'original_runtime_unchanged': True, 'normal_agent_summary_created': False,
              'api_receipt_serialization': 'PowerShell REST parsing changed created/updated strings to equivalent local timezone; the DB action receipt is retained verbatim and comparison canonicalizes only these timestamp fields.',
              'complete_engine_comparison': False, 'default_route_selection_eligible': False,
              'final_agent_qualification': False, 'same_source_performance_comparison': False,
              'model_or_service_started': False, 'formal_current_changed': False}
    verify_seal(seal_root)
    frozen_json(output / 'independent-collection.json', report)
    print({'scope': len(rows), 'native': report['native_status_counts'], 'scoring': report['scoring_status_counts']})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    seal_parser = sub.add_parser('seal')
    for name in ('run', 'stop', 'output'):
        seal_parser.add_argument('--' + name, required=True)
    collect_parser = sub.add_parser('collect')
    for name in ('seal', 'inputs', 'output'):
        collect_parser.add_argument('--' + name, required=True)
    score_parser = sub.add_parser('score')
    for name in ('seal', 'truth', 'observations', 'output'):
        score_parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    require_environment()
    if args.command == 'seal':
        seal(plain_path(args.run), plain_path(args.stop), plain_path(args.output))
    elif args.command == 'collect':
        collect(plain_path(args.seal), plain_path(args.inputs), plain_path(args.output))
    else:
        verify_seal(plain_path(args.seal))
        from quality import score_files
        report = score_files(args.truth, args.observations)
        frozen_json(args.output, report)
        print(report['summary'])


if __name__ == '__main__':
    main()

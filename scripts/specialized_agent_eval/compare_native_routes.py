"""Compare actual complete native runs without inventing overlapping wall time.

This consumes independent content scores and workbook readbacks. It never
starts a model, reads a controller credential, or selects the default route.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import math
from pathlib import Path

from common import ROOT, digest, fpath, frozen_json, read_json
from quality import paired_report


def finite_seconds(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Missing/nonfinite actual seconds: ' + label)
    return value


def bound(path):
    path = fpath(path)
    return {'path': str(path), 'sha256': digest(path)}


def read_run(run, quality_path, observations_path, readback_path):
    run = fpath(run)
    summary_path, prepared_path = run/'agent-native-summary.json', run/'prepared.json'
    summary, prepared = read_json(summary_path), read_json(prepared_path)
    if (prepared.get('prepared_only') is not False or summary.get('truth_read') is not False
            or summary.get('frozen_results_loaded') is not False
            or summary.get('shared_context_injected') is not False
            or summary.get('real_queue_and_engine_adapter') is not True
            or summary.get('run_error') is not None):
        raise ValueError('Comparison requires a completed actual non-replay native run')
    for key in ('inputs_sha256', 'native_source_inventory_sha256',
                'platform_source_binding_sha256', 'screen_policy_sha256'):
        if not prepared.get(key) or summary.get(key) != prepared[key]:
            raise ValueError('Run/preparation binding differs: ' + key)
    if digest(prepared['inputs']) != prepared['inputs_sha256']:
        raise ValueError('Frozen input manifest changed')
    source = fpath(prepared['native_source'])
    for rel, sha in prepared['native_source_files'].items():
        if digest(source/rel) != sha:
            raise ValueError('Native source changed: ' + rel)
    if read_json(source/'config/screen-policy.json') != prepared['screen_policy']:
        raise ValueError('Actual policy no longer matches source')
    batches = summary.get('native_batches', [])
    if len(batches) != 1 or batches[0].get('frozen_policy_settings_match') is not True:
        raise ValueError('Requires one scope-exact batch with matching frozen policy')
    counts = batches[0]['counts']
    n = prepared['input_count']
    if (counts['total'] != n or counts.get('pending') != 0
            or counts.get('failed') != 0 or counts.get('cancelled') != 0
            or counts.get('completed', 0) + counts.get('needs_review', 0) != n):
        raise ValueError('Run is incomplete; retain full unavailable-output denominator')
    observations_path, quality_path, readback_path = map(fpath,
        (observations_path, quality_path, readback_path))
    observations, quality, readback = map(read_json,
        (observations_path, quality_path, readback_path))
    if (observations.get('inputs_sha256') != prepared['inputs_sha256']
            or observations.get('evidence_kind') != 'native_model_receipts'
            or quality.get('actual_model_evidence') is not True
            or quality.get('all_expected_images_in_denominator') is not True
            or quality['bindings']['observations_sha256'] != digest(observations_path)
            or quality['bindings']['inputs_sha256'] != prepared['inputs_sha256']
            or quality['summary']['totals']['images'] != n):
        raise ValueError('Content score lacks scope-exact independently bound observations')
    index_path = observations_path.with_suffix('.native-index.json')
    index = read_json(index_path)
    if (digest(index_path) != observations['native_index_sha256']
            or fpath(index['native_run']) != run
            or index['prepared_sha256'] != digest(prepared_path)
            or index['native_source_inventory_sha256'] != prepared['native_source_inventory_sha256']):
        raise ValueError('Native identity index is not bound to the compared actual run')
    adapter_path = ROOT/'scripts/screen_native_report.py'
    if observations.get('native_adapter_sha256') != digest(adapter_path):
        raise ValueError('Native identity reader differs from independent score collection')
    spec = importlib.util.spec_from_file_location('native_comparison_identity', adapter_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    current_index = adapter.load_native_index(run, prepared['inputs'])
    if (current_index['native_table_identity_sha256'] != index['native_table_identity_sha256']
            or current_index['selected_ids'] != index['selected_ids']):
        raise ValueError('Native result/selection identity changed after independent scoring')
    if (readback.get('status') != 'passed' or readback.get('current_delivery_verified') is not True
            or readback.get('inputs') != n or readback.get('workbooks') != n
            or readback.get('no_book') != 0
            or {row['id'] for row in readback['records']} != {row['id'] for row in observations['records']}):
        raise ValueError('Scope-exact actual current workbook readback is missing')
    devices = read_json(run/'gpu-device-summary.json')['devices']
    hardware = {key: {field: row[field] for field in ('name', 'memory_total_mib')}
                for key, row in devices.items() if row.get('sample_count', 0) > 0}
    if not hardware:
        raise ValueError('Actual device observations are missing')
    events = read_json(run/summary['timeline_file'])['events']
    first = [e for e in events if e['stage'] == 'first_native_final_result' and e['event'] == 'observed']
    if len(first) != 1 or first[0]['native_record']['result_id'] != summary['first_native_final_result']['result_id']:
        raise ValueError('First actual result observation is not uniquely bound')
    native = read_json(run/summary['task_timing_file'])
    tasks = sorted(native['tasks'], key=lambda row: row.get('started') or '')
    engine_groups, loads, warm_cache_hits = [], [], []
    for task in tasks:
        raw = task.get('raw_timing', {})
        if task.get('status') != 'succeeded':
            raise ValueError('Unavailable native task in complete-run comparison')
        engine = task['engine']
        if not engine_groups or engine_groups[-1] != engine:
            engine_groups.append(engine)
        if raw.get('load_seconds', 0) > 0:
            loads.append({'task_id': task['id'], 'engine': engine,
                          'load_seconds': finite_seconds(raw['load_seconds'], 'native load')})
        if raw.get('cache_hit') is True:
            warm_cache_hits.append(task['id'])
    started = datetime.fromisoformat(events[0]['utc_timestamp'])
    bundle = summary.get('first_excel_bundle')
    bundle_seconds = None
    if bundle:
        bundle_seconds = finite_seconds((datetime.fromisoformat(bundle['finished']) - started).total_seconds(), 'first Excel bundle')
    metrics = {
        'input_count': n,
        'route_wall_seconds': finite_seconds(summary['wall_seconds'], 'route wall'),
        'agent_terminal_wall_seconds': finite_seconds(summary['agent_terminal_wall_seconds'], 'Agent terminal'),
        'first_native_observed_seconds': finite_seconds(first[0]['harness_elapsed_seconds'], 'first native observed'),
        'first_native_since_agent_submit_seconds': finite_seconds(first[0]['since_agent_submit_seconds'], 'first native since submit'),
        'first_excel_bundle_finished_seconds': bundle_seconds,
        'logical_controller_rounds': summary['model_rounds'],
        'batch_tool_calls': summary['tool_calls'],
        'actual_controller_api_requests': len(summary['model_requests']),
        'waiting_controller_count_stable': summary['waiting_controller_count_stable'],
        'ordered_native_engine_groups': engine_groups,
        'native_engine_switches': max(0, len(engine_groups)-1),
        'native_load_receipts': loads,
        'recognition_cache_hit_task_ids': warm_cache_hits,
        'content_totals': quality['summary']['totals'],
        'content_ratios': quality['summary']['ratios'],
    }
    return {'prepared': prepared, 'summary': summary, 'quality': quality,
            'hardware': hardware, 'metrics': metrics,
            'evidence': {name: bound(path) for name, path in {
                'summary': summary_path, 'prepared': prepared_path, 'quality': quality_path,
                'observations': observations_path, 'native_index': index_path,
                'readback': readback_path, 'native_timing': run/summary['task_timing_file'],
                'timeline': run/summary['timeline_file'], 'hardware': run/'gpu-device-summary.json',
            }.items()}}


def validate_comparable(before, after, kind):
    a, b = before['prepared'], after['prepared']
    for key in ('inputs_sha256', 'input_count', 'platform_source_binding_sha256', 'bundle_engines_sha256'):
        if a[key] != b[key]:
            raise ValueError('Different comparison condition: ' + key)
    if before['hardware'] != after['hardware']:
        raise ValueError('Observed GPU hardware differs')
    if before['quality']['bindings']['truth_sha256'] != after['quality']['bindings']['truth_sha256']:
        raise ValueError('Independent full-denominator truth differs')
    if a['native_source_files'].keys() != b['native_source_files'].keys():
        raise ValueError('Native source file sets differ')
    different = [key for key in a['native_source_files'] if a['native_source_files'][key] != b['native_source_files'][key]]
    if different != ['config/screen-policy.json']:
        raise ValueError('Only a frozen policy experiment may differ')
    left, right = before['summary']['native_batches'][0]['policy'], after['summary']['native_batches'][0]['policy']
    changed = {key for key in left.keys() | right.keys() if left.get(key) != right.get(key)}
    permitted = {'context_schedule'} if kind == 'schedule' else {
        'primary_engine', 'fallback_engines', 'max_upgrades', 'strategy_version'}
    if not changed or not changed <= permitted:
        raise ValueError('Uncontrolled policy differences: ' + ', '.join(sorted(changed)))
    if kind == 'schedule' and (left['context_schedule'] != 'whole_batch' or right['context_schedule'] != 'grouped'
            or left['group_size'] >= a['input_count']):
        raise ValueError('Schedule contrast must exercise more than one equal-sized context group')
    return sorted(changed)


def compare(before, after, kind):
    changed = validate_comparable(before, after, kind)
    paired = paired_report(before['quality'], after['quality'])
    a, b = before['metrics'], after['metrics']
    local_quality_no_regression = True
    old = {row['id']: row for row in before['quality']['records']}
    for new in after['quality']['records']:
        prior = old[new['id']]
        positions = lambda row: {(e['table_index'], e['row'], e['column']) for e in row['errors'] if e['visibility'] == 'visible'}
        if not positions(new) <= positions(prior):
            local_quality_no_regression = False
        for field in ('missing_tables', 'extra_tables', 'duplicate_positions', 'extra_cells', 'shape_errors',
                      'metadata_missing_targets', 'metadata_extra_parts', 'metadata_duplicate_table_text_parts',
                      'silent_critical_error_cells', 'automatic_uncertain_images',
                      'contradictory_automatic_and_review_images'):
            if new['metrics'].get(field, 0) > prior['metrics'].get(field, 0):
                local_quality_no_regression = False
    return {
        'format': 'specialized-actual-native-comparison-v1', 'kind': kind,
        'helper_sha256': digest(__file__),
        'status': 'compared', 'actual_complete_native_runs': True,
        'controlled_policy_differences': changed,
        'before': {'evidence': before['evidence'], 'measurements': a},
        'after': {'evidence': after['evidence'], 'measurements': b},
        'paired_quality': paired,
        'measurements': {
            'first_native_since_submit_delta_seconds': b['first_native_since_agent_submit_seconds'] - a['first_native_since_agent_submit_seconds'],
            'first_native_observed_delta_seconds': b['first_native_observed_seconds'] - a['first_native_observed_seconds'],
            'whole_route_delta_seconds': b['route_wall_seconds'] - a['route_wall_seconds'],
            'whole_route_before_over_after_ratio': a['route_wall_seconds']/b['route_wall_seconds'] if b['route_wall_seconds'] else None,
            'per_input_content_no_regression': local_quality_no_regression,
            'first_native_improved': b['first_native_since_agent_submit_seconds'] < a['first_native_since_agent_submit_seconds'],
            'scope_inputs': a['input_count'],
        },
        'default_route_selected': False,
        'limitations': [
            'Development subset evidence does not qualify a 200+ batch or real phone photographs.',
            'First native result can need review; it is not a correct automatic delivery or a downloaded Excel.',
            'Each harness starts new services; OS/disk caches are not purged. Native load/cache observations are reported, not inferred machine coldness.',
            'Actual wall time is reported separately from overlapping task/load/CPU/preparation/provider time; no summation substitutes for wall time.',
            'No DeepSeek Harness baseline or human correction timing was measured.',
        ],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['schedule', 'model'], required=True)
    for side in ('before', 'after'):
        for part in ('run', 'quality', 'observations', 'readback'):
            parser.add_argument('--'+side+'-'+part, required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    runs = [read_run(*[getattr(args, side+'_'+part) for part in ('run', 'quality', 'observations', 'readback')])
            for side in ('before', 'after')]
    result = compare(*runs, args.kind)
    frozen_json(args.output, result)
    print(result['measurements'])

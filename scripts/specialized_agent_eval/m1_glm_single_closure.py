"""Independently close a one-input GLM runtime diagnostic, never a route ranking."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics

from common import digest, frozen_json, indexed, read_json
from controlled_recovery import bound, directory_inventory, file_inventory, plain_path, require_environment
from m1_model_receipts import check_sources, owned_processes
from m1_partial_model_receipts import read_ps_json
from quality import evaluate


def gpu_window(samples, task):
    if not task.get('started') or not task.get('finished'):
        return {'status': 'no_closed_observed_task_window'}
    start = datetime.fromisoformat(task['started']).timestamp()
    finish = datetime.fromisoformat(task['finished']).timestamp()
    observations = []
    for sample in samples:
        if sample['exit_code'] != 0 or not start <= sample['time'] <= finish:
            continue
        pieces = [piece.strip() for piece in sample['observed'].split(',')]
        observations.append({'utc_timestamp': sample['utc_timestamp'], 'device': pieces[0],
                             'memory_mib': float(pieces[1]), 'total_mib': float(pieces[2]),
                             'utilization_percent': float(pieces[3])})
    if not observations:
        return {'status': 'no_successful_gpu_samples_in_actual_task_window'}
    return {'status': 'actual_task_timestamp_window_device_samples', 'samples': len(observations),
            'task_start': task['started'], 'task_finish': task['finished'],
            'memory_mib': {name: function([v['memory_mib'] for v in observations])
                           for name, function in [('min', min), ('max', max), ('median', statistics.median)]},
            'utilization_percent': {name: function([v['utilization_percent'] for v in observations])
                                    for name, function in [('min', min), ('max', max), ('median', statistics.median)]},
            'per_process_attribution': False,
            'scope_note': 'Device totals overlap this actual task window, including loading and all other processes; no independent per-stage/per-process GPU attribution.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'seal', 'collection', 'prepare', 'previous-collection', 'truth', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    require_environment()
    run, seal, collection, prepare, previous, truth_path, output = map(plain_path,
        (args.run, args.seal, args.collection, args.prepare, args.previous_collection, args.truth, args.output))
    if output.exists():
        raise ValueError('Use a new single-diagnostic closure identity')
    sealed = read_json(seal / 'result.json')
    if (sealed['status'] != 'sealed_before_truth' or sealed['actual_harness_exit_code'] != 0
            or plain_path(sealed['raw_run']) != run or owned_processes(run)
            or file_inventory(run) != sealed['runtime_files']
            or directory_inventory(run) != sealed['runtime_directories']):
        raise ValueError('Actual stopped single diagnostic seal changed')
    sources = check_sources(run)
    readback = read_json(collection / 'independent-readback.json')
    observations = read_json(collection / 'observations.json')
    index = read_json(collection / 'observations.native-index.json')
    old_index = read_json(previous / 'observations.native-index.json')
    if (readback['seal'] != bound(seal / 'result.json') or readback['scope_inputs'] != 1
            or readback['source_sqlite_opened'] is not False
            or readback['agent_current_bundle_matches_independent_download'] is not True
            or readback['observations'] != bound(collection / 'observations.json')):
        raise ValueError('Actual Agent delivery and independent download/readback differ')
    current = index['rows'][0]
    old = next(row for row in old_index['rows'] if row['id'] == current['id'])
    if current['scoring_status'] != 'success' or old['scoring_status'] != 'success':
        raise ValueError('Source comparison requires the actual successful original source')
    database = collection / 'sqlite-copies/native.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tasks = [dict(row) for row in db.execute('SELECT * FROM tasks')]
        results = {row['id']: json.loads(row['original']) for row in db.execute('SELECT id,original FROM results')}
    model_task = next(task for task in tasks if task['kind'] == 'ocr' and task['engine'] == 'glm')
    raw = results[model_task['result_id']]
    session_id = raw['engine_session']['id']
    loading_path = seal / 'runtime/native/ocr/sessions' / session_id / 'loading.json'
    loading = read_json(loading_path)
    if loading['status'] != 'ready' or not any(s['stage'] == 'glm_weights_loading' for s in loading['stages']):
        raise ValueError('Required detailed loading evidence was not observed')
    original_freeze = read_json(truth_path.parent / 'freeze.json')
    if digest(truth_path) != original_freeze['truth_sha256']:
        raise ValueError('Original development truth changed after its independent freeze')
    # The sole source was frozen from actual native success before this truth read.
    truths = indexed(read_json(truth_path)['samples'], 'id', 'development truth')
    quality = evaluate([truths[current['id']]], observations['records'])
    quality.update(scope='One source selected by actual pre-score runtime success; bounded diagnostic content only, not a development route ranking',
                   diagnostic_single_scope=True, original_development_denominator=60,
                   source_truth_sha256=digest(truth_path), observations_sha256=digest(collection / 'observations.json'),
                   complete_engine_comparison=False, default_route_selection_eligible=False,
                   final_agent_qualification=False, source_selection_used_truth=False)
    quality_path = output.parent / (output.stem + '-quality.json')
    frozen_json(quality_path, quality)
    old_source, new_source = old['source_receipts'][0], current['source_receipts'][0]
    old_crop, new_crop = old_source['prepared_metadata'], new_source['prepared_metadata']
    samples = read_json(seal / 'runtime/gpu-samples.json')
    summary = read_json(seal / 'runtime/agent-native-summary.json')
    preparation_files = file_inventory(prepare)
    report = {'format': 'm1-glm-single-runtime-diagnostic-closure-v1', 'utc': datetime.now(timezone.utc).isoformat(),
              'status': 'single_success_with_failed_context_and_verified_pending_review_delivery',
              'terminal_seal': bound(seal / 'result.json'), 'independent_readback': bound(collection / 'independent-readback.json'),
              'diagnostic_content_score': bound(quality_path), 'source_binding': sources, 'helper': bound(__file__),
              'sample_id': current['id'], 'full_source_sha256_unchanged': current['input_sha256'] == old['input_sha256'],
              'actual_model_input_comparison': {'previous': {'kind': old_crop['kind'], 'sha256': old_crop['prepared_pixel_sha256'],
                                                            'size': [old_crop['width'], old_crop['height']]},
                                                 'current': {'kind': new_crop['kind'], 'sha256': new_crop['prepared_pixel_sha256'],
                                                             'size': [new_crop['width'], new_crop['height']]},
                                                 'actual_prepared_input_bytes_differ': old_crop['prepared_pixel_sha256'] != new_crop['prepared_pixel_sha256'],
                                                 'same_prepared_input_performance_comparison': False,
                                                 'reason': 'Current context recognition timed out and the actual native preparation used the original full source instead of the previous complete-region crop.'},
              'context_tasks': [task for task in tasks if task['kind'] == 'screen_context'],
              'actual_glm_task': model_task,
              'worker_processing_seconds': raw['elapsed_seconds'], 'loader_model_load_seconds': raw['load_seconds'],
              'loading_elapsed_clock_scope': loading['clock_origin'], 'loading_total_seconds': loading['elapsed_seconds'],
              'loading_stages': loading['stages'], 'detailed_loading_receipt': bound(loading_path),
              'gpu_actual_task_windows': [{'task_id': task['id'], 'engine': task['engine'], **gpu_window(samples, task)}
                                         for task in tasks if task.get('started') and task.get('finished')],
              'gpu_samples': bound(seal / 'runtime/gpu-samples.json'),
              'static_recognition_named_gpu_file_is_after_native_result': True,
              'static_gpu_phase_note': 'The prepare-root recognition-001 GPU snapshot was observed after native recognition finished. Its filename is not an active-recognition measurement.',
              'actual_agent': {'model_requests': summary['model_rounds'], 'tool_calls': summary['tool_calls'],
                               'agent_terminal_wall_seconds': summary['agent_terminal_wall_seconds'],
                               'status': summary['status'], 'verified_delivery': summary['verified_delivery'],
                               'delivery_completeness': summary['delivery_completeness'],
                               'actual_published_bundle_ids': readback['actual_agent_published_bundle_ids'],
                               'independent_current_bundle_matches': True, 'xlsx_checks': readback['xlsx_checks']},
              'budget_policy': summary['native_batches'][0]['policy'],
              'actual_request_timeout_after_context_spend': new_crop['request_timeout_seconds'],
              'budgets_extended': False, 'dtype_attention_prompt_token_changes': False,
              'original_prompt_and_output_token_counts': None,
              'token_count_note': 'Original page-mode workers did not record token counts; no character-length proxy is substituted.',
              'resource_closure': {'owned_runtime_processes_remaining': [],
                                    'exit_observation': bound(prepare / 'harness-exit-observed-001.json'),
                                    'owned_after_exit': bound(prepare / 'owned-processes-after-exit-001.json'),
                                    'gpu_after_exit': bound(prepare / 'gpu-device-after-exit-001.txt'),
                                    'gpu_slot_released': True, 'external_termination': False,
                                    'prepare_evidence_files': preparation_files},
              'next_full_development_trial_recommendation': {'status': 'bounded_full60_runtime_trial_can_be_considered_but_current_result_is_not_quality_or_efficiency_qualification',
                                                           'reason': 'A real ready/success/current-delivery path is now observed, but context failed and model input/environment/source differ. The previous full60 run remains partial.',
                                                           'requires_new_identity_and_full60_denominator': True,
                                                           'requires_stable_source_freeze_with_M2_differences_explicit': True,
                                                           'requires_unchanged_180_240_360_budgets_and_model_parameters': True,
                                                           'requires_owned_GPU_slot_and_bounded_stop_on_repeated_stage_failures': True,
                                                           'must_preserve_context_failures_and_actual_crop_identity': True,
                                                           'must_not_skip_failed_inputs_or_use_truth_for_selection': True,
                                                           'full60_run_launched_by_this_closure': False},
              'truth_opened_only_for_independent_score_after_terminal_seal': True, 'holdout_read': False,
              'original_sqlite_opened': False, 'formal_current_changed': False,
              'complete_engine_comparison': False, 'same_source_performance_comparison': False,
              'default_route_selection_eligible': False, 'final_agent_qualification': False}
    if (owned_processes(run) or file_inventory(run) != sealed['runtime_files']
            or directory_inventory(run) != sealed['runtime_directories']):
        raise ValueError('Original terminal runtime changed during independent closure')
    frozen_json(output, report)
    print({'status': report['status'], 'loading_seconds': loading['elapsed_seconds'],
           'processing_seconds': raw['elapsed_seconds'], 'agent_delivery_matches_readback': True,
           'source_crop_comparable': False, 'quality': quality['summary']['totals']})


if __name__ == '__main__':
    main()

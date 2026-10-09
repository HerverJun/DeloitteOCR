"""Close one stopped full60 GLM trial and independently pair its scored content.

The closure reads only already scored quality and isolated native SQLite bytes.
It does not read truth values, start services/models, or adopt a default route.
Actual model-input byte hashes are verified against the terminal runtime seal.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import struct
import sqlite3

from common import digest, frozen_json, indexed, read_json
from controlled_recovery import bound, directory_inventory, file_inventory, plain_path, require_environment
from m1_glm_single_closure import gpu_window
from m1_model_receipts import check_sources, owned_processes
from quality import paired_report, summarize


def checked_quality(path, freeze):
    value = read_json(path)
    if (value['summary']['totals']['images'] != 60
            or value.get('all_expected_images_in_denominator') is not True
            or value['bindings']['truth_sha256'] != freeze['truth_sha256']
            or value['bindings']['inputs_sha256'] != freeze['inputs_sha256']
            or value.get('actual_model_evidence') is not True
            or summarize(value['records']) != value['summary']):
        raise ValueError('Requires coherent independently scored frozen development60: ' + str(path))
    return value


def wall_seconds(task):
    if not task['started'] or not task['finished']:
        return None
    return (datetime.fromisoformat(task['finished']) - datetime.fromisoformat(task['started'])).total_seconds()


def distribution(values):
    if not values:
        return {'count': 0}
    return {'count': len(values), 'min': min(values), 'median': statistics.median(values),
            'max': max(values), 'sum': sum(values)}


def png_size(path):
    with path.open('rb') as handle:
        header = handle.read(24)
    if header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise ValueError('Actual prepared model input is not a PNG: ' + str(path))
    return list(struct.unpack('>II', header[16:24]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('run', 'seal', 'collection', 'prepare', 'quality', 'h-quality', 'p-quality', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    require_environment()
    run, seal, collection, prepare, quality_path, h_path, p_path, output = map(plain_path,
        (args.run, args.seal, args.collection, args.prepare, args.quality, args.h_quality, args.p_quality, args.output))
    if output.exists():
        raise ValueError('Use a new full-development closure identity')
    sealed = read_json(seal / 'result.json')
    if (sealed['status'] != 'sealed_before_truth' or sealed['actual_harness_exit_code'] != 0
            or plain_path(sealed['raw_run']) != run or owned_processes(run)
            or file_inventory(run) != sealed['runtime_files']
            or directory_inventory(run) != sealed['runtime_directories']):
        raise ValueError('Requires unchanged actually stopped terminal runtime seal')
    sources = check_sources(run)
    if sources['scope_inputs'] != 60 or sources['engine'] != 'glm':
        raise ValueError('Not an actual full60 GLM trial')
    exit_observation = read_json(prepare / 'harness-exit-observed-001.json')
    if (exit_observation['actual_observed_harness_exit_code'] != 0
            or exit_observation['external_termination'] is not False
            or read_json(prepare / 'owned-processes-after-exit-001.json') != []):
        raise ValueError('Normal observed tool exit and owned process absence must be independently recorded')
    readback = read_json(collection / 'independent-readback.json')
    observations = read_json(collection / 'observations.json')
    index = read_json(collection / 'observations.native-index.json')
    freeze = read_json(Path(observations['inputs']).parent / 'freeze.json') if observations.get('inputs') else None
    if freeze is None:
        prepared = read_json(run / 'prepared.json')
        freeze = read_json(plain_path(prepared['inputs']).parent / 'freeze.json')
    quality = checked_quality(quality_path, freeze)
    prior = {'hunyuan': checked_quality(h_path, freeze), 'paddlevl': checked_quality(p_path, freeze)}
    if (readback['seal'] != bound(seal / 'result.json') or readback['scope_inputs'] != 60
            or readback['source_sqlite_opened'] is not False
            or readback['observations'] != bound(collection / 'observations.json')
            or quality['bindings']['observations_sha256'] != digest(collection / 'observations.json')
            or observations['inputs_sha256'] != freeze['inputs_sha256']):
        raise ValueError('Independent observation/readback/score binding differs')
    qrows = indexed(quality['records'], 'id', 'full60 quality')
    rows = indexed(index['rows'], 'id', 'full60 independent native index')
    if rows.keys() != qrows.keys():
        raise ValueError('Scoring scope differs from isolated native scope')
    database = collection / 'sqlite-copies/native.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tasks = [dict(row) for row in db.execute('SELECT * FROM tasks')]
        screen = {row['task_id']: dict(row) for row in db.execute('SELECT * FROM screen_tasks')}
        results = {row['id']: json.loads(row['original']) for row in db.execute('SELECT id,original FROM results')}
        batches = [dict(row) for row in db.execute('SELECT * FROM screen_batches')]
    if len(batches) != 1:
        raise ValueError('Full development trial must retain its original unique batch')
    model_tasks = [task for task in tasks if task['engine'] == 'glm' and task['kind'] == 'ocr' and task['id'] in screen]
    context_tasks = [task for task in tasks if task['kind'] == 'screen_context']
    by_version = {row['input_version_id']: row for row in rows.values()}
    inputs, loading = [], {}
    samples = read_json(seal / 'runtime/gpu-samples.json')
    for task in model_tasks:
        entry = screen[task['id']]
        metadata = json.loads(entry['metadata'])
        original = plain_path(metadata['path'])
        relative = original.relative_to(run)
        prepared_path = seal / 'runtime' / relative
        file_sha = digest(prepared_path)
        size = png_size(prepared_path)
        if (file_sha != metadata['prepared_pixel_sha256']
                or size != [metadata['width'], metadata['height']]
                or sealed['runtime_files'][relative.as_posix()]['sha256'] != file_sha):
            raise ValueError('Actual model input bytes or dimensions differ from native receipt')
        source_row = by_version[entry['input_version_id']]
        raw = results.get(task['result_id'], {})
        session = raw.get('engine_session', {}).get('id')
        if session and session not in loading:
            loading_path = seal / 'runtime/native/ocr/sessions' / session / 'loading.json'
            receipt = read_json(loading_path)
            loading[session] = {'receipt': bound(loading_path), 'content': receipt}
        inputs.append({'id': source_row['id'], 'source_image_sha256': source_row['input_sha256'],
                       'source_visible_size': [source_row['normalized_visible_pixels']['width'],
                                               source_row['normalized_visible_pixels']['height']],
                       'task': task, 'actual_task_wall_seconds': wall_seconds(task),
                       'actual_prepared_input': {'sealed_file': bound(prepared_path), 'size': size,
                                                'kind': metadata['kind'], 'source_polygon': metadata['source_polygon'],
                                                'source_to_prepared': metadata['source_to_prepared'],
                                                'prepared_to_source': metadata['prepared_to_source'],
                                                'receipt_field_prepared_pixel_sha256_is_file_sha256': True},
                       'native_preparation_metadata': metadata, 'raw_worker_processing_seconds': raw.get('elapsed_seconds'),
                       'raw_loader_model_load_seconds': raw.get('load_seconds'),
                       'gpu_device_window': gpu_window(samples, task),
                       'scoring_status': source_row['scoring_status'], 'native_status': source_row['native_status']})
    summary = read_json(seal / 'runtime/agent-native-summary.json')
    actual_agent_delivery = readback['agent_current_bundle_matches_independent_download']
    pairs = {name + '_to_glm': paired_report(score, quality) for name, score in prior.items()}
    report = {'format': 'm1-glm-full60-independent-closure-v1', 'utc': datetime.now(timezone.utc).isoformat(),
              'status': 'full60_runtime_and_content_closed_without_default_route_qualification',
              'scope_note': 'Frozen simulated development60 only; public36/chinese24. No validation40 or holdout consumed.',
              'terminal_seal': bound(seal / 'result.json'), 'independent_readback': bound(collection / 'independent-readback.json'),
              'independent_observations': bound(collection / 'observations.json'),
              'independent_native_index': bound(collection / 'observations.native-index.json'),
              'development_quality': bound(quality_path), 'source_binding': sources,
              'helpers': {name: bound(Path(__file__).parent / name) for name in
                          ('m1_glm_full_closure.py', 'm1_glm_single_closure.py', 'm1_model_receipts.py', 'quality.py')},
              'full_input_denominator': 60, 'quality': quality['summary'], 'quality_cohorts': quality['cohort_grouped'],
              'native_status_counts': dict(Counter(row['native_status'] for row in rows.values())),
              'scoring_status_counts': dict(Counter(row['scoring_status'] for row in rows.values())),
              'model_task_status_counts': dict(Counter(task['status'] for task in model_tasks)),
              'context_task_status_counts': dict(Counter(task['status'] for task in context_tasks)),
              'original_batch': batches[0], 'actual_model_inputs': inputs,
              'actual_loading_receipts': loading,
              'task_wall_seconds': {'glm': distribution([wall_seconds(task) for task in model_tasks if wall_seconds(task) is not None]),
                                    'geometry': distribution([wall_seconds(task) for task in context_tasks if wall_seconds(task) is not None])},
              'actual_context_tasks': context_tasks, 'gpu_samples': bound(seal / 'runtime/gpu-samples.json'),
              'gpu_attribution_note': 'Device samples overlap actual task timestamps including loading and other processes; no per-process or independent per-stage causal attribution.',
              'actual_agent': {'model_requests': summary['model_rounds'], 'tool_calls': summary['tool_calls'],
                               'agent_terminal_wall_seconds': summary['agent_terminal_wall_seconds'],
                               'status': summary['status'], 'run_error': summary.get('run_error'),
                               'verified_delivery': summary['verified_delivery'], 'delivery_completeness': summary['delivery_completeness'],
                               'actual_published_bundle_ids': readback['actual_agent_published_bundle_ids'],
                               'independent_current_bundle_matches': actual_agent_delivery, 'xlsx_checks': readback['xlsx_checks']},
              'budgets_extended': False, 'dtype_attention_prompt_token_changes': False,
              'original_prompt_vision_output_token_counts': None,
              'token_count_note': 'Frozen page-mode worker did not record token counts. No character-count proxy is substituted.',
              'paired_content_with_prior_full60': pairs,
              'prior_quality_bindings': {'hunyuan': bound(h_path), 'paddlevl': bound(p_path)},
              'pairing_scope_note': 'Independent full-denominator content pairing only. Frozen source evolution and H recovery/P failed Agent prevent same-source timing or route-default ranking.',
              'resource_closure': {'owned_runtime_processes_remaining': [],
                                    'exit_observation': bound(prepare / 'harness-exit-observed-001.json'),
                                    'owned_after_exit': bound(prepare / 'owned-processes-after-exit-001.json'),
                                    'gpu_after_exit': bound(prepare / 'gpu-device-after-exit-001.txt'),
                                    'gpu_processes_after_exit': bound(prepare / 'gpu-processes-after-exit-001.txt'),
                                    'external_termination': False, 'owned_gpu_slot_released': True},
              'truth_opened_only_for_independent_score_after_terminal_seal': True, 'closure_read_truth_values': False,
              'holdout_read': False, 'source_sqlite_opened': False, 'model_or_service_started_by_closure': False,
              'formal_current_changed': False, 'same_source_performance_comparison': False,
              'default_route_selection_eligible': False, 'final_agent_qualification': False,
              'automatic_coverage_target_relaxed': False}
    if (owned_processes(run) or file_inventory(run) != sealed['runtime_files']
            or directory_inventory(run) != sealed['runtime_directories']):
        raise ValueError('Original stopped runtime changed during independent closure')
    frozen_json(output, report)
    print({'status': report['status'], 'native': report['native_status_counts'], 'quality': quality['summary']['totals'],
           'actual_agent_delivery': actual_agent_delivery, 'paired': {name: value['totals'] for name, value in pairs.items()}})


if __name__ == '__main__':
    main()

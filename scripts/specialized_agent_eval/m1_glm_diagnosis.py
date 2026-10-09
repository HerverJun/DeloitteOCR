"""Describe actual partial GLM stages and freeze a truth-free single-input scope.

This CPU-only analysis reads a previously isolated SQLite copy, not the original
runtime database, and never imports a model or reads quality/truth/holdout files.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from common import digest, frozen_json, indexed, read_json
from controlled_recovery import bound, plain_path, require_environment
from m1_partial_model_receipts import verify_seal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('seal', 'collection', 'inputs', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    require_environment()
    seal, collection, inputs_path, output = map(plain_path, (args.seal, args.collection, args.inputs, args.output))
    if output.exists():
        raise ValueError('Use a new CPU diagnosis identity')
    sealed, run = verify_seal(seal)
    evidence = read_json(collection / 'independent-collection.json')
    index = read_json(collection / 'observations.native-index.json')
    inputs = read_json(inputs_path)
    if (index['inputs_sha256'] != digest(inputs_path) or index['scope_inputs'] != 60
            or evidence['truth_read'] is not False or evidence['original_sqlite_opened'] is not False
            or inputs['truth_available_to_inference'] is not False
            or evidence['seal'] != bound(seal / 'result.json')):
        raise ValueError('Requires independently sealed actual truth-free partial source')
    successes = [row for row in index['rows'] if row['scoring_status'] == 'success']
    if len(successes) != 1:
        raise ValueError('The proposed diagnostic must reuse the sole actual successful source')
    chosen = successes[0]
    database = plain_path(index['native_database'])
    if not database.is_relative_to(collection / 'sqlite-copies'):
        raise ValueError('Diagnosis may only open the independent SQLite byte copy')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        results = {row['id']: json.loads(row['original']) for row in db.execute('SELECT id,original FROM results')}
    sample_index = indexed(inputs['samples'], 'id', 'frozen input')
    rows = []
    for attempted in evidence['attempted_recognition_tasks']:
        task, link = attempted['task'], attempted['screen_task']
        metadata = json.loads(link['metadata'])
        row = {'sample_id': attempted['sample_id'], 'task_id': task['id'], 'native_status': task['status'],
               'original_source_size': [sample_index[attempted['sample_id']]['width'], sample_index[attempted['sample_id']]['height']],
               'prepared_crop_size': [metadata['width'], metadata['height']],
               'prepared_crop_sha256': metadata['prepared_pixel_sha256'],
               'actual_start': task['started'], 'actual_finish': task['finished'],
               'native_phase': task['phase'], 'native_error': task['error'],
               'engine_load_events': metadata.get('engine_load_events', []),
               'stored_reservation_seconds': metadata['budget_reservation_seconds'],
               'stored_request_timeout_seconds': metadata['request_timeout_seconds'],
               'stored_spent_seconds': link['spent_seconds'],
               'original_input_token_count': None, 'original_output_token_count': None,
               'token_count_observation': 'Original page-mode worker did not persist prompt/image/generated token counts.'}
        if task['finished']:
            row['actual_task_active_wall_seconds'] = (datetime.fromisoformat(task['finished']) - datetime.fromisoformat(task['started'])).total_seconds()
        raw = results.get(task['result_id'])
        if raw:
            row.update(actual_worker_elapsed_seconds=raw['elapsed_seconds'], actual_model_load_seconds=raw['load_seconds'],
                       engine_session=raw['engine_session'], actual_layout_regions=raw['raw']['regions'],
                       generated_region_character_lengths=[{'bbox_2d': part['bbox_2d'], 'task': part['task'],
                                                           'characters': len(part['content'])} for part in raw['raw']['generated']],
                       output_characters_are_not_token_counts=True,
                       native_ocr_result_id=task['result_id'])
        rows.append(row)
    worker_stages = []
    for loading in sorted((seal / 'runtime/native/ocr/sessions').glob('*/loading.json')):
        value = read_json(loading)
        if value['engine'] != 'glm':
            continue
        log = loading.parent / 'engine.log'
        text = log.read_text('utf-8', errors='replace')
        worker_stages.append({'session_id': loading.parent.name, 'loading_receipt': bound(loading),
                              'engine_log': bound(log), 'loading_status': value['status'],
                              'last_loading_stage': value['stage'], 'recorded_loading_elapsed_seconds': value['elapsed_seconds'],
                              'loading_stages': value['stages'],
                              'observed_generation_stack': ' in generate' in text and ' in _sample' in text,
                              'stack_source': 'Observed Python faulthandler dump; no inference substage duration or token count was persisted.'})
    single = deepcopy(inputs)
    single['samples'] = [deepcopy(sample_index[chosen['id']])]
    single.update(scope='One actual successful development input for bounded runtime diagnosis, not a quality/automatic qualification scope',
                  parent_inputs_sha256=digest(inputs_path), selection_basis='Sole actual completed native GLM result before offline truth scoring; no truth or score used')
    frozen_json(output / 'single-inputs.json', single)
    sdk = plain_path('F:/OCR/output/clear-automation-dev-20261002/program/bundles/ocr/runtimes/glm/Lib/site-packages/glmocr')
    report = {'format': 'm1-glm-partial-stage-diagnosis-v1', 'utc': datetime.now(timezone.utc).isoformat(),
              'seal': bound(seal / 'result.json'), 'independent_collection': bound(collection / 'independent-collection.json'),
              'native_index': bound(collection / 'observations.native-index.json'), 'helper': bound(__file__),
              'scope_inputs': 60, 'scoring_status_counts': evidence['scoring_status_counts'],
              'actual_attempted_recognition_tasks': rows, 'actual_glm_worker_loading_receipts': worker_stages,
              'sdk_configuration_sources': {name: bound(sdk / name) for name in ['config.py', 'config.yaml', 'dataloader/page_loader.py']},
              'original_model_configuration': {'dtype': 'torch.bfloat16', 'attention': 'sdpa', 'do_sample': False,
                                               'repetition_penalty': 'unchanged SDK payload', 'max_new_tokens': 'unchanged SDK payload'},
              'single_input_manifest': bound(output / 'single-inputs.json'),
              'single_input_id': chosen['id'], 'single_input_selection_used_truth': False,
              'diagnostic_design': {'purpose': 'Reproduce the sole successful source with detailed loading stages before retrying failed inputs',
                                    'scope': 'one full source image through actual geometry and GLM page pipeline',
                                    'new_freeze_required': True, 'required_loading_instrumentation_commit': '03602cd',
                                    'budgets_unchanged': {'image': 360, 'request': 180, 'startup': 240},
                                    'implicit_dtype_attention_prompt_changes': False,
                                    'observations': ['stage-specific loading.json with heartbeat and durations',
                                                     'actual ready/load seconds and recognition/timeout task evidence',
                                                     'worker faulthandler stack on timeout and current per-device GPU utilization/memory',
                                                     'source/crop dimensions and SHA, actual native result and current export readback if produced'],
                                    'success_does_not_qualify_full60_or_default': True, 'gpu_launch_performed': False},
              'findings': ['Two actual engine-load timeout events ended at coarse model_initialization; the first native error was later mapped to budget exhaustion, and neither history is rewritten.',
                           'Four actual recognition timeouts followed readiness; saved Python stacks show execution in Transformers generation. The unique success used a 1301x856 crop and recorded 90.195 seconds worker processing.',
                           'Crop dimensions alone do not explain all failures: the 1750x463 crop timed out despite fewer pixels than the successful crop.',
                           'The success worker was later reused for a failing input; a successful load/readiness is not a guarantee of subsequent generation completion.',
                           'Original page-mode prompt/vision/output token counts are unavailable. Character lengths are labeled only as characters, and no token count is inferred.',
                           'Non-owned game/desktop processes were observed concurrently; WDDM per-process memory was unavailable, so no causal attribution or external process termination is supported.'],
              'truth_read': False, 'holdout_read': False, 'quality_report_read': False,
              'original_sqlite_opened': False, 'model_or_service_started': False,
              'formal_current_changed': False, 'complete_engine_comparison': False,
              'default_route_selection_eligible': False, 'final_agent_qualification': False}
    verify_seal(seal)
    frozen_json(output / 'result.json', report)
    print({'single_input': chosen['id'], 'attempted_tasks': len(rows), 'gpu_launch_performed': False})


if __name__ == '__main__':
    main()

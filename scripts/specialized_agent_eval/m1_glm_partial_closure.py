"""Bind actual GLM partial-stop, content, resource and diagnostic receipts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from common import frozen_json, read_json
from controlled_recovery import bound, plain_path, require_environment
from m1_partial_model_receipts import read_ps_json, verify_seal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('seal', 'collection', 'diagnosis', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    require_environment()
    seal, collection, diagnosis, output = map(plain_path, (args.seal, args.collection, args.diagnosis, args.output))
    sealed, run = verify_seal(seal)
    gathered = read_json(collection / 'independent-collection.json')
    quality = read_json(collection / 'development-quality.json')
    index = read_json(collection / 'observations.native-index.json')
    diagnostics = read_json(diagnosis / 'result.json')
    if (gathered['seal'] != bound(seal / 'result.json') or quality.get('partial_run') is not True
            or quality['all_expected_images_in_denominator'] is not True or quality['summary']['totals']['images'] != 60
            or diagnostics['native_index'] != bound(collection / 'observations.native-index.json')):
        raise ValueError('Partial stop/score/diagnosis evidence bindings differ')
    successes = [row for row in quality['records'] if row['status'] == 'success']
    if len(successes) != 1:
        raise ValueError('Actual partial available-result count changed')
    stop = seal / 'stop-evidence'
    stopped = read_ps_json(stop / 'native-termination-receipt-002.json')
    remaining = read_ps_json(stop / 'owned-processes-after-stop.json')
    if remaining != [] or stopped['native_and_workers_absent_after_action'] is not True:
        raise ValueError('Owned resource cleanup is not proved')
    report = {'format': 'm1-glm-full60-partial-closure-v1', 'utc': datetime.now(timezone.utc).isoformat(),
              'status': 'bounded_partial_closed_not_quality_or_route_qualified',
              'terminal_byte_seal': bound(seal / 'result.json'),
              'independent_native_index': bound(collection / 'observations.native-index.json'),
              'independent_observations': bound(collection / 'observations.json'),
              'independent_collection': bound(collection / 'independent-collection.json'),
              'development_quality': bound(collection / 'development-quality.json'),
              'no_truth_single_diagnosis': bound(diagnosis / 'result.json'), 'helper': bound(__file__),
              'full_input_denominator': 60, 'native_status_counts': gathered['native_status_counts'],
              'scoring_status_counts': gathered['scoring_status_counts'],
              'sole_available_result': {'id': successes[0]['id'], 'metrics': successes[0]['metrics'], 'errors': successes[0]['errors']},
              'full_denominator_content': quality['summary'],
              'output_coverage_interpretation': 'Unavailable rows retain full denominators. Their missing cells measure available-output coverage, never estimated recognition accuracy; 53 paused means 52 recognition-unattempted plus 1 started/interrupted.',
              'batch_budget_and_deadline': {'original_policy': gathered['batch']['policy'],
                                           'created': gathered['batch']['created'],
                                           'original_deadline': gathered['batch']['deadline'],
                                           'paused_at': gathered['batch']['paused_at'],
                                           'budgets_extended': False, 'cost_or_failure_history_reset': False},
              'stage_findings': diagnostics['findings'],
              'actual_agent_execution': {'model_requests': gathered['model_request_count'],
                                         'tool_calls': len(gathered['agent_operations']),
                                         'native_operation_states': [o['state'] for o in gathered['agent_operations']],
                                         'delivery_publishes': gathered['agent_delivery_publish_count'],
                                         'verified_delivery': False, 'actual_xlsx_export_observed': False,
                                         'observed_harness_exit_code': 1, 'normal_agent_summary_available': False},
              'resource_closure': {'native_termination': bound(stop / 'native-termination-receipt-002.json'),
                                    'owned_processes_before': bound(stop / 'owned-processes-before-stop.json'),
                                    'owned_processes_after': bound(stop / 'owned-processes-after-stop.json'),
                                    'gpu_device_before': bound(stop / 'gpu-device-before-stop.txt'),
                                    'gpu_device_after': bound(stop / 'gpu-device-after-stop.txt'),
                                    'gpu_processes_after': bound(stop / 'gpu-processes-after-stop.txt'),
                                    'owned_service_and_workers_absent': True, 'owned_gpu_slot_released': True,
                                    'non_owned_processes_terminated': False,
                                    'external_gpu_load_is_not_attributed_to_one_process': True},
              'truth_opened_only_for_offline_score_after_terminal_seal': True,
              'holdout_read': False, 'model_or_service_started_by_closure': False,
              'complete_engine_comparison': False, 'same_source_performance_comparison': False,
              'default_route_selection_eligible': False, 'final_agent_qualification': False,
              'automatic_coverage_target_relaxed': False, 'formal_current_changed': False}
    frozen_json(output, report)
    print({'status': report['status'], 'inputs': 60, 'available_results': 1,
           'owned_gpu_slot_released': True, 'actual_agent_delivery': False})


if __name__ == '__main__':
    main()

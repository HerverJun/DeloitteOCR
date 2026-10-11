"""Seal a full actual optional-route trial without claiming automatic review relief."""
import argparse
from datetime import datetime, timezone

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from quality import paired_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    require_environment()
    root, output = map(fpath, (args.root, args.output))
    names = {
        'implementation': 'evaluation/precision-route-implementation-checkpoint-20261011-001.json',
        'run': 'runs/m1-precision-route-validation-direct-20261011-009/result.json',
        'exit': 'runs/m1-precision-route-validation-direct-prepare-20261011-009/native-exit.json',
        'trial_source': 'source-snapshots/m1-precision-route-validation-direct-20261011-009/trial-source-binding.json',
        'independent': 'evaluation/m1-precision-route-validation-direct-independent-009-001/review.json',
        'attempts': 'evaluation/m1-precision-route-validation-direct-independent-009-001/routed-attempt-ledger.json',
        'observations': 'evaluation/m1-precision-route-validation-direct-independent-009-001/verified-observations.json',
        'quality': 'evaluation/m1-precision-route-validation-direct-quality-009-001.json',
        'h_baseline': 'evaluation/m1-h-rectified-validation-direct-quality-008-001.json',
        'rejected_cpu_route': 'evaluation/m1-language-route-validation-score-001/report.json',
        'old_failed_budget_audit': 'evaluation/m1-precision-fallback-budget-audit-002/report.json'}
    data = {key: read_json(root / path) for key, path in names.items()}
    run, independent, attempts, quality = (data[key] for key in ('run', 'independent', 'attempts', 'quality'))
    totals = quality['summary']['totals']
    if (run['status'] != 'terminal' or run['scope_inputs'] != 40 or run['counts']['pending'] != 0
            or data['exit']['exit_code'] != 0 or independent['status'] != 'passed'
            or independent['scope_inputs'] != 40 or attempts['inputs'] != 40
            or independent['route_audit_sha256'] != digest(root / names['attempts'])
            or independent['route_audit_helper_sha256'] != digest(fpath(__file__).with_name('precision_route_readback.py'))
            or quality['bindings']['observations_sha256'] != digest(root / names['observations'])
            or quality['bindings']['inputs_sha256'] != data['h_baseline']['bindings']['inputs_sha256']
            or not quality['actual_model_evidence'] or not quality['all_expected_images_in_denominator']
            or totals['images'] != 40 or data['trial_source']['policy'] != run['policy']
            or run['policy']['engine_route'] != 'business_language_v1'):
        raise ValueError('Actual complete route, attempt audit or quality binding differs')
    delta = paired_report(data['h_baseline'], quality)
    route_counts = {}
    primary_failures, fallback_attempts, fallback_success, skipped = 0, 0, 0, 0
    for record in attempts['records']:
        primary = record['route']['engines'][0]
        route_counts[primary] = route_counts.get(primary, 0) + 1
        skipped += bool(record.get('primary_budget_skip'))
        for attempt in record['attempts']:
            primary_failures += attempt['pass_index'] == 0 and attempt['status'] == 'failed'
            fallback_attempts += attempt['pass_index'] == 1
            fallback_success += attempt['pass_index'] == 1 and attempt['status'] == 'succeeded'
    report = {
        'format': 'actual-bounded-precision-route-closure-v1', 'utc': datetime.now(timezone.utc).isoformat(),
        'evidence': {key: bound(root / path) for key, path in names.items()},
        'helper': bound(__file__), 'actual_scope': 40, 'actual_counts': run['counts'],
        'actual_primary_routes': route_counts, 'native_model_attempts': attempts['model_attempts'],
        'actual_model_engines': attempts['engines'], 'actual_model_attempt_states': attempts['attempt_states'],
        'failed_primary_attempts_retained': primary_failures, 'explicit_primary_budget_skips': skipped,
        'technical_fallback_attempts': fallback_attempts, 'technical_fallback_success': fallback_success,
        'source_budget_overshoots': attempts['source_budget_overshoots'],
        'root_budget_overshoot_seconds': attempts['root_budget_overshoot_seconds'],
        'original_source_and_root_budgets_extended': False,
        'quality': totals, 'paired_with_actual_h008': delta,
        'current_xlsx': independent['xlsx_readback'],
        'actual_wall_seconds': run['wall_seconds'], 'first_native_result': run['first_native_result'],
        'same_source_performance_comparison': False, 'agent_calls': 0,
        'measured_review_workload_reduction': False, 'human_review_required': True,
        'adopt_default': False, 'final_agent_qualification': False,
        'formal_current': 'v0.9.3', 'completed_plan': False,
        'holdout_or_challenge_used_for_tuning': False,
        'next_action': 'Keep full review and default H+math. Use full-denominator native result to decide optional precision route; demonstrate actual review relief before M1/M2 closure and final M3/M4 qualification.'}
    frozen_json(output, report)
    print({'quality': totals, 'paired_delta': delta['totals'], 'technical_fallbacks': fallback_attempts,
        'source_budget_overshoots': attempts['source_budget_overshoots'], 'default_adopted': False})


if __name__ == '__main__':
    main()

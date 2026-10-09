"""Independently pair unchanged sealed development60 scores and fidelity.

This offline review preserves unavailable outputs and Agent failures. It does
not rank timing across a controlled recovery and an independent failed Agent,
start services/models, or select a production default.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from quality import paired_report, summarize

ROOT = Path('F:/OCR/output/platform-specialized-agent-20261007')
H = ROOT/'runs/agent-native-h-development-recovery-001/execution-002/actual-002'
P = ROOT/'evaluation/paddlevl60-retry-independent-001'


def checked_binding(entry):
    path = fpath(entry['path'])
    if digest(path) != entry['sha256'] or path.stat().st_size != entry['bytes']:
        raise ValueError('Sealed evidence binding changed: ' + str(path))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    require_environment()
    output = fpath(args.output)
    if output.exists():
        raise ValueError('Use a new review output')
    truth = ROOT/'dataset/v3-002/development/truth.json'
    h_closure_path = ROOT/'evaluation/h60-controlled-recovery-002-closure-001.json'
    p_closure_path = ROOT/'evaluation/paddlevl60-retry-closure-001.json'
    hc, pc = read_json(h_closure_path), read_json(p_closure_path)
    h_quality_path = checked_binding(hc['development_quality'])
    p_quality_path = checked_binding(pc['bindings']['development_quality'])
    h_observations, p_observations = H/'observations.json', P/'observations.json'
    h_quality, p_quality = read_json(h_quality_path), read_json(p_quality_path)
    dataset_freeze = read_json(truth.parent/'freeze.json')
    for q,observations_path in ((h_quality,h_observations),(p_quality,p_observations)):
        if q['summary']['totals']['images'] != 60 or not q['all_expected_images_in_denominator']:
            raise ValueError('Not the frozen full development scope')
        if (q['bindings']['observations_sha256']!=digest(observations_path)
                or q['bindings']['truth_sha256']!=dataset_freeze['truth_sha256']
                or q['bindings']['inputs_sha256']!=dataset_freeze['inputs_sha256']
                or q['actual_model_evidence'] is not True
                or summarize(q['records'])!=q['summary']):
            raise ValueError('Reusable score lacks frozen source binding or coherent full denominator')
        observations=read_json(observations_path)
        if observations['inputs_sha256']!=dataset_freeze['inputs_sha256']:
            raise ValueError('Input identity differs')
        if {r['id'] for r in q['records']}!={r['id'] for r in observations['records']}:
            raise ValueError('Observation and scoring scopes differ')
    if digest(truth)!=dataset_freeze['truth_sha256']:
        raise ValueError('Frozen development truth bytes changed')
    if h_quality['bindings']['truth_sha256'] != p_quality['bindings']['truth_sha256']:
        raise ValueError('Different full-denominator truth')
    checked_binding(hc['actual_result'])
    checked_binding(pc['bindings']['actual_summary'])
    failure_path = checked_binding(pc['bindings']['first_load_budget_failure'])
    failure = read_json(failure_path)
    loading_path = checked_binding(failure['first_paddle_loading'])
    loading = read_json(loading_path)
    if (loading['stage'] != failure['first_paddle_stage']['stage']
            or loading['status'] != 'loading'
            or failure['failed_task_load_events'] is not None):
        raise ValueError('First failure phase no longer matches bound loading evidence')
    p_readback = read_json(checked_binding(pc['bindings']['independent_readback']))
    if p_readback['original_runtime_byte_set_unchanged'] is not True:
        raise ValueError('Independent raw readback lost sealed identity')
    px = p_readback['xlsx_checks'][0]
    h_xlsx = read_json(checked_binding(hc['independent_xlsx']))
    p_xlsx = read_json(checked_binding(px['report']))
    checked_binding(px['zip']);checked_binding(px['snapshot'])
    if h_xlsx['zip_sha256']!=digest(H/'actual-results.zip'):
        raise ValueError('Hunyuan sealed XLSX bytes changed')
    for readback in (h_xlsx,p_xlsx):
        if readback['status']!='passed' or readback['inputs']!=60 or readback['current_delivery_verified'] is not True:
            raise ValueError('Prior complete-denominator current workbook qualification is missing')
    output.parent.mkdir(parents=True, exist_ok=True)
    paired = paired_report(h_quality, p_quality)
    cohorts = {}
    for cohort in ('chinese', 'public'):
        left = {**h_quality, 'records':[r for r in h_quality['records'] if r['cohort']==cohort],
                'summary':h_quality['cohort_grouped'][cohort]}
        right = {**p_quality, 'records':[r for r in p_quality['records'] if r['cohort']==cohort],
                 'summary':p_quality['cohort_grouped'][cohort]}
        cohorts[cohort] = {'hunyuan':left['summary'], 'paddlevl':right['summary'],
                          'paired':paired_report(left,right)}
    unavailable = [r for r in p_quality['records'] if r['status']!='success']
    h_prepared = read_json(ROOT/'runs/agent-native-h-development-001/prepared.json')
    p_prepared = read_json(ROOT/'runs/agent-native-paddlevl-development-002/prepared.json')
    h_files, p_files = h_prepared['native_source_files'], p_prepared['native_source_files']
    differences = sorted(k for k in h_files.keys()|p_files.keys() if h_files.get(k)!=p_files.get(k))
    value = {
        'format':'m1-sealed-full-denominator-quality-review-v1',
        'utc':datetime.now(timezone.utc).isoformat(),
        'status':'unchanged_full60_scores_and_current_xlsx_bound_and_paired',
        'previous_passed_scoring_and_readback_reused':True,
        'recognition_or_workbook_checks_repeated':False,
        'scope':'Development60, public36/chinese24; 44 sources, 40 document/template groups. Simulated screens, not real business accuracy.',
        'truth_scoring_only_after_prior_terminal_seals':True,
        'truth_or_holdout_given_to_models':False,
        'services_or_models_started':False,
        'source_sqlite_opened':False,
        'same_frozen_full_denominator':True,
        'hunyuan':{'summary':h_quality['summary'],'agent_delivered_current_xlsx':hc['actual_agent_published_bundle_matches_independent_download'],
                   'controller_rounds':hc['total_logical_controller_requests'],'tool_calls':hc['agent_operation_count'],
                   'workbooks':h_xlsx['workbooks'],'no_book':h_xlsx['no_book'],
                   'performance_baseline_qualified':False},
        'paddlevl':{'summary':p_quality['summary'],'agent':pc['agent'],
                    'workbooks':p_xlsx['workbooks'],'no_book':p_xlsx['no_book'],
                    'post_agent_harness_export_only':True,
                    'agent_delivered_current_xlsx':False},
        'paired':paired,'cohorts':cohorts,
        'unavailable_output_images':[{k:r[k] for k in ('id','status','metrics')} for r in unavailable],
        'failed_image_review_delta_is_not_workload_reduction':True,
        'failure_phase':{'task_id':failure['task_id'],'stage':loading['stage'],
                         'status_at_cancellation':loading['status'],
                         'load_outcomes_recorded':False,
                         'consecutive_cold_load_guard_counted_failure':False,
                         'interpretation':'Image budget cancelled initialization before adapter-ready; not an inference failure or proof of a general timeout.'},
        'comparability':{'ocr_source_differences':differences,
                         'platform_source_binding_equal':h_prepared['platform_source_binding_sha256']==p_prepared['platform_source_binding_sha256'],
                         'engine_only_same_source_comparison':differences==['config/screen-policy.json'],
                         'normal_wall_time_comparison_eligible':False,
                         'reason':'H60 is controlled recovery; Paddle has a load-budget failure and failed Agent. Source evolution is listed separately.'},
        'adoption':{'paddlevl_global_default_supported':False,'automatic_delivery_qualified':False,
                    'measured_human_workload_reduction_available':False,
                    'default_route_selection_eligible':False,
                    'reasons':['Paddle whole-table and critical-cell scores regress on full denominator.',
                               'Paddle failed image is unavailable, not reduced review work.',
                               'Both routes have zero automatic coverage and no reliable local-error score.',
                               'GLM full development60 and fixed validation40 still require actual trials.',
                               'Current workbook fidelity does not prove candidate content accuracy.']},
        'bindings':{name:bound(path) for name,path in {
            'helper':Path(__file__),'h_closure':h_closure_path,'p_closure':p_closure_path,
            'h_quality':h_quality_path,'p_quality':p_quality_path,
            'h_observations':h_observations,'p_observations':p_observations,
            'development_truth':truth,'failure_audit':failure_path,'bound_loading':loading_path,
            'h_zip':H/'actual-results.zip','h_snapshot':H/'readback-snapshot.json',
            'p_zip':px['zip']['path'],'p_snapshot':px['snapshot']['path']}.items()}}
    frozen_json(output,value)
    print({'output':str(output),'paired':paired['totals'],
           'workbooks':{'hunyuan':h_xlsx['workbooks'],'paddlevl':p_xlsx['workbooks']},
           'default_route_selection_eligible':False})


if __name__=='__main__':
    main()

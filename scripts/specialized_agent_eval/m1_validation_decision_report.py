"""Seal complete validation comparison and actual technical failures.

This report cannot promote a model, CPU route, or missing output to automatic
delivery. It opens a fresh byte copy of a previously independently read DB.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from quality import paired_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    require_environment()
    root, output = map(fpath, (args.root, args.output))
    if output.exists():
        raise ValueError('Use a new immutable complete-comparison identity')
    hq = root / 'evaluation/m1-h-validation-direct-quality-004-002.json'
    gq = root / 'evaluation/m1-glm-validation-direct-quality-006-001.json'
    route_path = root / 'evaluation/m1-language-route-validation-score-001/report.json'
    independent = root / 'evaluation/m1-glm-validation-direct-independent-006-002'
    review = read_json(independent / 'review.json')
    h, g, route = map(read_json, (hq, gq, route_path))
    hr = read_json(root / 'runs/m1-h-validation-direct-20261009-004/result.json')
    gr = read_json(root / 'runs/m1-glm-validation-direct-20261010-006/result.json')
    exit_path = root / 'runs/m1-glm-validation-direct-prepare-20261010-006/background-exit.json'
    if (h['bindings']['truth_sha256'] != g['bindings']['truth_sha256']
            or h['bindings']['inputs_sha256'] != g['bindings']['inputs_sha256']
            or h['summary']['totals']['images'] != 40 or g['summary']['totals']['images'] != 40
            or review['status'] != 'passed' or read_json(exit_path)['exit_code'] != 0
            or gr['status'] != 'terminal' or gr['counts']['pending'] != 0
            or digest(independent / 'verified-observations.json') != g['bindings']['observations_sha256']):
        raise ValueError('Full validation scope/terminal/source/score binding differs')
    output.mkdir(parents=True)
    db_members = [p for p in independent.glob('workbench.sqlite3*') if p.is_file()]
    before = {p.name: digest(p) for p in db_members}
    for path in db_members:
        shutil.copyfile(path, output / path.name)
        if digest(output / path.name) != before[path.name]:
            raise ValueError('Independent native ledger copy differs')
    records = read_json(independent / 'verified-observations.json')['records']
    by_version = {row['input_version_id']: row for row in records}
    failed_tasks = []
    with closing(sqlite3.connect(output / 'workbench.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        tasks = {r['id']: dict(r) for r in db.execute('SELECT * FROM tasks')}
        for table in ('screen_tasks', 'screen_contexts'):
            for row in db.execute('SELECT * FROM ' + table):
                task = tasks[row['task_id']]
                if task['status'] != 'succeeded':
                    failed_tasks.append({'sample_id': by_version[row['input_version_id']]['id'],
                        'input_version_id': row['input_version_id'], 'ledger': table,
                        'task': task})
    if {p.name: digest(p) for p in db_members} != before:
        raise ValueError('Prior independent evidence changed')
    totals = g['summary']['totals']
    unavailable = [row['id'] for row in records if row['status'] != 'success']
    report = {'format': 'm1-complete-validation-route-decision-v1',
        'bindings': [bound(p) for p in (hq, gq, route_path, independent / 'review.json',
                                       independent / 'verified-observations.json', exit_path, Path(__file__))],
        'scope_inputs': 40, 'hunyuan': h['summary'], 'glm': g['summary'],
        'glm_against_hunyuan': paired_report(h, g),
        'cohorts': {name: {'hunyuan': h['cohort_grouped'].get(name),
                           'glm': g['cohort_grouped'].get(name)}
                    for name in h['cohort_grouped'].keys() | g['cohort_grouped'].keys()},
        'native_unavailable_images': unavailable, 'terminal_failed_native_tasks': failed_tasks,
        'glm_actual_delivery': review['xlsx_readback'],
        'all_inputs_still_require_action': 40,
        'failed_image_reference_cells_unavailable': totals['all_reference_cells'] - totals['review_reference_cells'],
        'failed_image_critical_cells_unavailable': totals['visible_critical_error_cells'] - totals['visible_critical_error_cells_reviewed'],
        'review_reduction_from_failures': 0,
        'language_route': {'development_frozen': True, 'actual_native_route_executed': False,
            'summary': route['hybrid_summary'], 'against_hunyuan': route['against_hunyuan'],
            'against_glm': route['against_glm'], 'adopted': False,
            'reason': 'Complete validation has negative net repairs and three unavailable outputs.'},
        'actual_wall_seconds': {'hunyuan': hr['wall_seconds'], 'glm': gr['wall_seconds']},
        'actual_first_native_result': {'hunyuan': hr['first_native_result'], 'glm': gr['first_native_result']},
        'same_source_performance_qualified': False,
        'provisional_default': 'Keep existing Hunyuan; no all-batch GLM or frozen language route adoption.',
        'precision_route': 'GLM remains an explicitly bounded reviewed candidate, not a validated automatic upgrade.',
        'technical_fallback': 'A failed task stays failed with original cost/budget; any alternate engine needs a new linked bounded task. No implicit retry or budget reset.',
        'holdout_read': False, 'native_original_database_opened': False,
        'product_policy_changed': False, 'm1_complete': False, 'm2_complete': False,
        'qualification_limits': ['No native hybrid route executed.', 'No measured human review relief.',
            'Historical H/GLM sources differ; elapsed time is descriptive, not a controlled speed comparison.',
            'No final common freeze, 240/24 holdout, or 200+ Agent qualification.']}
    frozen_json(output / 'report.json', report)
    print({'scope_inputs': 40, 'unavailable_images': len(unavailable),
           'native_failed_tasks': len(failed_tasks), 'all_inputs_require_action': 40,
           'glm_net_visible_repairs': report['glm_against_hunyuan']['totals']['net_visible_cell_repairs'],
           'language_route_net_visible_repairs': route['against_hunyuan']['totals']['net_visible_cell_repairs']})


if __name__ == '__main__':
    main()

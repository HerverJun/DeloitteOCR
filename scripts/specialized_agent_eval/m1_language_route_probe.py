"""Freeze a source-text H/GLM route, then score a separate hypothetical replay.

The freeze phase reads only a byte copy of the stopped native database and
truth-free inputs. The score phase opens development truth only after the
route decisions exist. Neither phase dispatches inference or edits candidates.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

from common import digest, fpath, frozen_json, read_json


def overlap(part, region):
    left = max(0.0, min(part[2], region[2]) - max(part[0], region[0]))
    top = max(0.0, min(part[3], region[3]) - max(part[1], region[1]))
    area = max(1.0, (part[2] - part[0]) * (part[3] - part[1]))
    return left * top / area


def freeze(args):
    database, inputs, output = map(fpath, (args.database, args.inputs, args.output))
    if output.exists() or inputs.parent.name != 'development':
        raise ValueError('Use a new development-only route identity')
    source_files = {name: digest(database.parent / name) for name in
                    ('workbench.sqlite3', 'workbench.sqlite3-wal', 'workbench.sqlite3-shm')
                    if (database.parent / name).is_file()}
    manifest = read_json(inputs)
    if manifest.get('truth_available_to_inference') is not False:
        raise ValueError('Route inputs contain reference material')
    samples = {item['image_sha256']: item for item in manifest['samples']}
    if len(samples) != len(manifest['samples']) == 60:
        raise ValueError('Expected full unique development60')
    for item in samples.values():
        if digest(item['image']) != item['image_sha256']:
            raise ValueError('Source image changed')
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute('SELECT source_hash,preparation,evidence FROM screen_items').fetchall()
    finally:
        connection.close()
    if len(rows) != 60 or {row[0] for row in rows} != set(samples):
        raise ValueError('Native context/image binding is incomplete')
    policy = {
        'format': 'm1-business-region-language-route-policy-v1',
        'identity': 'm1-development-language-route-001',
        'feature_source': 'actual PP-OCRv6-auxiliary text in observed business regions only',
        'box_min_fraction': 0.5,
        'cjk_rule': 'han_count >= 20 and han_count > ascii_latin_count selects Hunyuan',
        'latin_rule': 'ascii_latin_count >= 20 and ascii_latin_count >= 4 * max(1, han_count) selects GLM',
        'otherwise': 'undecided; no automatic engine route',
        'source_only': True,
        'forbidden_features': ['sample ID', 'cohort', 'reference truth', 'model candidate text', 'quality score'],
        'actual_native_route_executed': False,
        'product_policy_changed': False,
        'automatic_delivery': False,
    }
    decisions = []
    for image_sha, raw_preparation, raw_evidence in rows:
        preparation, evidence = json.loads(raw_preparation), json.loads(raw_evidence)
        if (preparation.get('input_sha256') != image_sha
                or evidence.get('provenance', {}).get('contains_reference_input') is not False):
            raise ValueError('Context source or reference boundary differs')
        regions = [item['box'] for item in preparation.get('business_regions', ())]
        if not regions:
            raise ValueError('Observed business region missing')
        selected = [item for item in evidence.get('text', ())
                    if item.get('source') == 'PP-OCRv6-auxiliary'
                    and item.get('box') and any(overlap(item['box'], box) >= .5 for box in regions)]
        content = ' '.join(item.get('text', '') for item in selected)
        han = sum('\u3400' <= char <= '\u9fff' for char in content)
        latin = sum('A' <= char <= 'Z' or 'a' <= char <= 'z' for char in content)
        engine = ('hunyuan' if han >= 20 and han > latin else
                  'glm' if latin >= 20 and latin >= 4 * max(1, han) else None)
        decisions.append({'id': samples[image_sha]['id'], 'image_sha256': image_sha,
                          'source_evidence_sha256': hashlib.sha256(raw_evidence.encode()).hexdigest(),
                          'business_regions': len(regions), 'matched_aux_text': len(selected),
                          'matched_text_sha256': hashlib.sha256(content.encode()).hexdigest(),
                          'han_count': han, 'ascii_latin_count': latin, 'selected_engine': engine})
    decisions.sort(key=lambda row: row['id'])
    output.mkdir(parents=True)
    frozen_json(output / 'policy.json', policy)
    frozen_json(output / 'decisions.json', {'format': 'm1-source-only-route-decisions-v1',
        'policy_sha256': digest(output / 'policy.json'), 'inputs_sha256': digest(inputs),
        'database_copy_before_read_sha256': source_files,
        'source_file_is_a_copy': True, 'truth_read': False, 'candidate_values_read': False,
        'decisions': decisions})
    print({'inputs': len(decisions), 'hunyuan': sum(x['selected_engine'] == 'hunyuan' for x in decisions),
           'glm': sum(x['selected_engine'] == 'glm' for x in decisions),
           'undecided': sum(x['selected_engine'] is None for x in decisions)})


def score(args):
    frozen, h_path, g_path, truth, output = map(fpath,
        (args.frozen, args.hunyuan, args.glm, args.truth, args.output))
    if output.exists() or truth.parent.name != 'development':
        raise ValueError('New development scoring identity required')
    decision = read_json(frozen / 'decisions.json')
    if digest(frozen / 'policy.json') != decision['policy_sha256']:
        raise ValueError('Route policy changed after freeze')
    h, g = read_json(h_path), read_json(g_path)
    if not (h['inputs_sha256'] == g['inputs_sha256'] == decision['inputs_sha256']):
        raise ValueError('Paired inference inputs differ')
    h_rows, g_rows = [{row['id']: row for row in source['records']} for source in (h, g)]
    chosen = {}
    for item in decision['decisions']:
        image_id, engine = item['id'], item['selected_engine']
        if engine not in {'hunyuan', 'glm'}:
            raise ValueError('Undecided route cannot be scored as an engine')
        if image_id not in h_rows or image_id not in g_rows or any(
                row['image_sha256'] != item['image_sha256'] for row in (h_rows[image_id], g_rows[image_id])):
            raise ValueError('Paired candidate/image source binding differs')
        chosen[image_id] = deepcopy((h_rows if engine == 'hunyuan' else g_rows)[image_id])
    if len(chosen) != len(h_rows) == len(g_rows) == 60:
        raise ValueError('Complete 60-input denominator required')
    hybrid = deepcopy(h)
    hybrid['evidence_kind'] = 'hypothetical_cpu_source_text_route'
    hybrid['records'] = [chosen[row['id']] for row in h['records']]
    hybrid['scope'] = 'Hypothetical per-image choice from two separately executed model trials; no native route run'
    hybrid['route_policy_sha256'] = decision['policy_sha256']
    output.mkdir(parents=True)
    frozen_json(output / 'observations.json', hybrid)
    from quality import paired_report, score_files
    actual, before_h, before_g = [score_files(truth, path) for path in
                                  (output / 'observations.json', h_path, g_path)]
    report = {'format': 'm1-hypothetical-language-route-development-score-v1',
        'policy_sha256': decision['policy_sha256'], 'decisions_sha256': digest(frozen / 'decisions.json'),
        'hybrid_observations_sha256': digest(output / 'observations.json'),
        'hunyuan_observations_sha256': digest(h_path), 'glm_observations_sha256': digest(g_path),
        'truth_sha256': digest(truth), 'summary': actual['summary'],
        'against_hunyuan': paired_report(before_h, actual),
        'against_glm': paired_report(before_g, actual),
        'actual_native_route_executed': False, 'same_source_timing_qualified': False,
        'automatic_review_reduction_qualified': False, 'default_route_qualified': False,
        'validation40_qualified': False, 'holdout_read': False}
    frozen_json(output / 'report.json', report)
    print({'exact_images': actual['summary']['totals']['visible_table_exact_images'],
           'critical_exact_cells': actual['summary']['totals']['visible_critical_exact_cells'],
           'visible_exact_cells': actual['summary']['totals']['visible_exact_cells'],
           'review_cells': actual['summary']['totals']['review_reference_cells']})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('freeze', 'score'))
    for name in ('database', 'inputs', 'frozen', 'hunyuan', 'glm', 'truth', 'output'):
        parser.add_argument('--' + name)
    arguments = parser.parse_args()
    (freeze if arguments.phase == 'freeze' else score)(arguments)

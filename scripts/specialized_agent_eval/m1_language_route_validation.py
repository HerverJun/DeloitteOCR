"""Apply the already frozen development source-text route to validation40.

The decision phase copies a stopped native database before SQLite access and
never reads truth or either model candidate. Scoring is a separate phase.
This is a hypothetical CPU comparison, not an executed native route.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from common import digest, fpath, frozen_json, read_json
from m1_language_route_probe import overlap


def freeze(args):
    development, database, inputs, output = map(fpath,
        (args.development, args.database, args.inputs, args.output))
    if output.exists() or inputs.parent.name != 'validation':
        raise ValueError('A new validation-only route identity is required')
    parent = read_json(development / 'decisions.json')
    policy = read_json(development / 'policy.json')
    if (digest(development / 'policy.json') != parent['policy_sha256']
            or policy['format'] != 'm1-business-region-language-route-policy-v1'
            or parent['truth_read'] is not False):
        raise ValueError('Development route is not frozen truth-free')
    manifest = read_json(inputs)
    if manifest.get('truth_available_to_inference') is not False:
        raise ValueError('Validation inference inputs contain reference truth')
    samples = {item['image_sha256']: item for item in manifest['samples']}
    if len(samples) != len(manifest['samples']) or len(samples) != 40:
        raise ValueError('Validation40 image source denominator differs')
    for item in samples.values():
        if digest(item['image']) != item['image_sha256']:
            raise ValueError('Validation source image changed')
    names = [name for name in ('workbench.sqlite3', 'workbench.sqlite3-wal',
                               'workbench.sqlite3-shm') if (database.parent / name).is_file()]
    before = {name: digest(database.parent / name) for name in names}
    output.mkdir(parents=True)
    for name in names:
        shutil.copyfile(database.parent / name, output / name)
        if digest(output / name) != before[name]:
            raise ValueError('Native database copy differs')
    if {name: digest(database.parent / name) for name in names} != before:
        raise ValueError('Prior independent native database copy changed')
    with sqlite3.connect(output / 'workbench.sqlite3') as connection:
        rows = connection.execute('SELECT source_hash,preparation,evidence FROM screen_items').fetchall()
    if len(rows) != 40 or {row[0] for row in rows} != set(samples):
        raise ValueError('Native context/image binding is incomplete')
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
                    if item.get('source') == 'PP-OCRv6-auxiliary' and item.get('box')
                    and any(overlap(item['box'], box) >= policy['box_min_fraction']
                            for box in regions)]
        content = ' '.join(item.get('text', '') for item in selected)
        han = sum('\u3400' <= char <= '\u9fff' for char in content)
        latin = sum('A' <= char <= 'Z' or 'a' <= char <= 'z' for char in content)
        engine = ('hunyuan' if han >= 20 and han > latin else
                  'glm' if latin >= 20 and latin >= 4 * max(1, han) else None)
        decisions.append({'id': samples[image_sha]['id'], 'image_sha256': image_sha,
                          'source_evidence_sha256': hashlib.sha256(raw_evidence.encode()).hexdigest(),
                          'business_regions': len(regions), 'matched_aux_text': len(selected),
                          'matched_text_sha256': hashlib.sha256(content.encode()).hexdigest(),
                          'han_count': han, 'ascii_latin_count': latin,
                          'selected_engine': engine})
    decisions.sort(key=lambda row: row['id'])
    frozen_json(output / 'decisions.json', {
        'format': 'm1-source-only-route-validation-decisions-v1',
        'development_policy_sha256': digest(development / 'policy.json'),
        'development_decisions_sha256': digest(development / 'decisions.json'),
        'inputs_sha256': digest(inputs), 'copied_native_database_sha256': before,
        'truth_read': False, 'candidate_values_read': False,
        'actual_native_route_executed': False, 'decisions': decisions})
    print({'inputs': len(decisions),
           'hunyuan': sum(row['selected_engine'] == 'hunyuan' for row in decisions),
           'glm': sum(row['selected_engine'] == 'glm' for row in decisions),
           'undecided': sum(row['selected_engine'] is None for row in decisions)})


def score(args):
    frozen, development, h_path, g_path, truth, output = map(fpath,
        (args.frozen, args.development, args.hunyuan, args.glm, args.truth, args.output))
    if output.exists() or truth.parent.name != 'validation':
        raise ValueError('A new validation-only score identity is required')
    decisions = read_json(frozen / 'decisions.json')
    if (decisions['development_policy_sha256'] != digest(development / 'policy.json')
            or decisions['development_decisions_sha256'] != digest(development / 'decisions.json')
            or decisions['truth_read'] is not False):
        raise ValueError('Pre-truth frozen validation route changed')
    h, g = read_json(h_path), read_json(g_path)
    if not (h['inputs_sha256'] == g['inputs_sha256'] == decisions['inputs_sha256']):
        raise ValueError('Paired validation inference inputs differ')
    h_rows, g_rows = [{row['id']: row for row in source['records']} for source in (h, g)]
    selected = {}
    for item in decisions['decisions']:
        image_id, engine = item['id'], item['selected_engine']
        if image_id not in h_rows or image_id not in g_rows or any(
                row['image_sha256'] != item['image_sha256']
                for row in (h_rows[image_id], g_rows[image_id])):
            raise ValueError('Paired validation candidate/image binding differs')
        if engine in {'hunyuan', 'glm'}:
            selected[image_id] = deepcopy((h_rows if engine == 'hunyuan' else g_rows)[image_id])
    if len(h_rows) != len(g_rows) or len(h_rows) != len(decisions['decisions']) or len(h_rows) != 40:
        raise ValueError('Complete paired validation40 denominator required')
    output.mkdir(parents=True)
    from quality import paired_report, score_files
    h_score, g_score = score_files(truth, h_path), score_files(truth, g_path)
    report = {'format': 'm1-hypothetical-language-route-validation-score-v1',
        'development_policy_sha256': decisions['development_policy_sha256'],
        'validation_decisions_sha256': digest(frozen / 'decisions.json'),
        'hunyuan_observations_sha256': digest(h_path), 'glm_observations_sha256': digest(g_path),
        'truth_sha256': digest(truth), 'scope_inputs': len(decisions['decisions']),
        'decided_inputs': len(selected), 'undecided_inputs': 40 - len(selected),
        'hunyuan_summary': h_score['summary'], 'glm_summary': g_score['summary'],
        'actual_native_route_executed': False, 'default_route_qualified': False,
        'automatic_review_reduction_qualified': False, 'holdout_read': False}
    if len(selected) == 40:
        hybrid = deepcopy(h)
        hybrid['evidence_kind'] = 'hypothetical_cpu_source_text_route'
        hybrid['records'] = [selected[row['id']] for row in h['records']]
        hybrid['route_decisions_sha256'] = digest(frozen / 'decisions.json')
        frozen_json(output / 'observations.json', hybrid)
        hybrid_score = score_files(truth, output / 'observations.json')
        report.update(hybrid_summary=hybrid_score['summary'],
                      against_hunyuan=paired_report(h_score, hybrid_score),
                      against_glm=paired_report(g_score, hybrid_score),
                      hybrid_observations_sha256=digest(output / 'observations.json'))
    frozen_json(output / 'report.json', report)
    print({'decided': len(selected), 'undecided': 40 - len(selected),
           'hunyuan_exact': h_score['summary']['totals']['visible_table_exact_images'],
           'glm_exact': g_score['summary']['totals']['visible_table_exact_images'],
           'hybrid_exact': (report.get('hybrid_summary') or {'totals': {}})['totals'].get('visible_table_exact_images')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('freeze', 'score'))
    for name in ('development', 'database', 'inputs', 'frozen', 'hunyuan',
                 'glm', 'truth', 'output'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    (freeze if args.phase == 'freeze' else score)(args)

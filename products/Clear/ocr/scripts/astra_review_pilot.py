"""Prepare blinded Astra reviewer packets and score frozen reviewer outputs."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
RUN = 'astra-review-20260919-01'
BUILD = ROOT / 'build' / RUN
AUDIT = ROOT / 'audit' / RUN
FIELDS = ('row', 'column', 'row_span', 'column_span', 'text')

PROMPT = """You are an independent visual table reviewer replacing a human review step.
Only read this case directory. Do not read repository code, other cases, labels,
reports, previous model responses, Internet sources or evaluation scripts. Do not
spawn agents. Inspect page.jpg with view_image at original resolution. You may
create enlarged crops inside this case directory for legibility, but first inspect
the full image. Do not infer content from arithmetic or medical/world knowledge.

Review every input cell against the image. Input cell coordinates are zero-based
logical grid anchors with rectangular spans. Determine the complete table grid
from the image, excluding caption and footnotes but retaining real header rows.
For each input cell, independently judge structure (anchor and span in that full
grid) and text (content of the intended visible cell) as correct, incorrect, or
uncertain. Whitespace/layout alone is not a text error; do not treat different
digits, signs, superscripts or punctuation as equivalent. Mark ambiguous visible
structure or unreadable content uncertain. No self-confidence claim establishes
correctness. Unknown blank differs from visually confirmed empty cell.

Also produce your best full reviewed table transcription, including all blank,
merged and repeated cells. Record unresolved locations separately; do not hide
them or use the input values as a substitute for visual inspection. This is a
model reference for research, never a production edit or a verified source box.
You may build output by copying the input and making visually verified corrections
after inspecting every row, but the output must contain every cell explicitly.

Write response.json in this case directory, with schema:
{
 "case_id": "case-NN",
 "image_inspected": true,
 "cell_reviews": [["t0-c000", "correct|incorrect|uncertain", "correct|incorrect|uncertain"], ...],
 "issues": [{"cell_ids": [...], "kind": "text|structure|boundary|uncertain", "reason": "image-grounded reason"}],
 "reviewed_tables": [{"rows": N, "columns": M, "cells": [[row,column,row_span,column_span,"literal"], ...]}],
 "unresolved": [{"table": 0, "row": 0, "column": 0, "reason": "..."}],
 "table_verdict": "pass|needs_changes|uncertain",
 "notes": "Scope, visual ambiguities and any cropped-image limitations."
}
Every input cell ID must occur exactly once. Reviewed tables must be complete
non-overlapping grids. Do not silently retry or alter the result after evaluation.
Do not claim actual human review, measured API billing or proven geometry.
"""


def read(path):
    return json.loads(path.read_text('utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', 'utf-8')


def prepare():
    BUILD.mkdir(parents=True, exist_ok=False)
    AUDIT.mkdir(parents=True, exist_ok=False)
    source = ROOT / 'build/structure-repair-20260919-01/baseline/rows.json'
    rows = {r['id']: r for r in read(source)}
    policy = ROOT / 'audit/complex-tables-20260919-01/api/random-check-policy.json'
    ids = read(policy)['sample_ids']
    selected = sorted(ids, key=lambda s: hashlib.sha256(('astra-review-20260919:' + s).encode()).hexdigest())[:6]
    mp = ROOT / 'build/tableformer-next-20260915/dataset/inputs/test.json'
    manifest = {r['id']: r for r in read(mp)['samples']}
    assignments = []
    for number, ident in enumerate(selected, 1):
        case = f'case-{number:02d}'
        folder = BUILD / 'inbox' / case
        folder.mkdir(parents=True)
        image = (mp.parent / manifest[ident]['image']).resolve()
        assert sha(image) == manifest[ident]['sha256'] == rows[ident]['input_sha256_before']
        shutil.copy2(image, folder / 'page.jpg')
        tables = []
        for ti, table in enumerate(rows[ident]['adopted']['tables']):
            tables.append({'rows': table['rows'], 'columns': table['columns'], 'cells': [
                {'id': f't{ti}-c{ci:03d}', **{k: c[k] for k in FIELDS}}
                for ci, c in enumerate(table['cells'])]})
        save(folder / 'request.json', {'case_id': case, 'image': 'page.jpg', 'tables': tables})
        (folder / 'INSTRUCTIONS.md').write_text(PROMPT, 'utf-8')
        assignments.append({'case_id': case, 'sample_id': ident, 'group': rows[ident]['group'],
                            'request_sha256': sha(folder / 'request.json'), 'image_sha256': sha(folder / 'page.jpg'),
                            'input_cells': sum(len(t['cells']) for t in tables)})
    save(AUDIT / 'assignments.json', assignments)
    save(AUDIT / 'protocol.json', {
        'model_requested': 'gpt-6-astra', 'transport': 'independent Codex subagent per case, no inherited history',
        'reasoning_effort': 'inherited from parent; no separate override requested',
        'official_model_documentation': 'https://developers.openai.com/api/docs/models/gpt-6-astra',
        'role': 'user-authorized substitute for manual visual review; outputs are model-reviewed reference, not human ground truth',
        'frozen_before_calls': True, 'selection': 'first six SHA256(astra-review-20260919:sample_id) among existing fixed 12 random checks; no label-based selection',
        'source_sha256': sha(source), 'selection_policy_sha256': sha(policy),
        'prompt': PROMPT, 'prompt_sha256': hashlib.sha256(PROMPT.encode()).hexdigest(),
        'source_images_unchanged': True, 'case_count': 6,
        'evaluation': 'existing public reference labels withheld from reviewers; score only after all responses frozen',
        'quality_normalization': 'NFKC plus remove whitespace, matching historical scoring; raw literal exactness separately',
        'metrics': ['review decision coverage', 'precision of approved cells', 'error detection recall', 'false approvals', 'reviewed transcription structure and joint accuracy', 'raw literal agreement', 'unresolved coverage', 'per-document results'],
        'interpretation': 'small reviewer calibration on exposed historical English scientific crops, not Chinese financial/scan/photo acceptance; no universal reviewer precision claim',
        'deployment': 'no changes to product adoption, no hard-gate override; use Astra as research reference as requested, retain uncertainty and independent labels when present',
        'no_automatic_retry': True, 'human_time_measured': False, 'real_api_billing_measured': False,
    })
    print(json.dumps(assignments, ensure_ascii=False, indent=2))


def evaluate():
    from score_luna_structure_sim import score, aggregate, normalize
    from ocr_workbench.complex_table_contract import validate_grid
    assignments = read(AUDIT / 'assignments.json')
    # Freeze all responses before reading any reference label.
    locks = []
    for item in assignments:
        folder = BUILD / 'inbox' / item['case_id']
        assert sha(folder / 'request.json') == item['request_sha256']
        assert sha(folder / 'page.jpg') == item['image_sha256']
        locks.append({'case_id': item['case_id'], 'response_sha256': sha(folder / 'response.json')})
    lockfile = AUDIT / 'response-lock.json'
    if lockfile.exists():
        assert read(lockfile) == locks, 'Frozen responses changed'
    else:
        save(lockfile, locks)
    labels_path = ROOT / 'build/tableformer-next-20260915/dataset/sealed/test.annotations.json'
    labels = {s['id']: s for s in read(labels_path)['samples']}
    cases = []
    before_scores, reviewed_scores = [], []
    totals = Counter()
    for item in assignments:
        folder = BUILD / 'inbox' / item['case_id']
        request, response = read(folder / 'request.json'), read(folder / 'response.json')
        assert response['case_id'] == item['case_id'] and response['image_inspected'] is True
        inputs = {c['id']: (ti, c) for ti, t in enumerate(request['tables']) for c in t['cells']}
        reviews = response['cell_reviews']
        assert Counter(r[0] for r in reviews) == Counter(inputs.keys()), 'Missing/duplicate review'
        assert all(len(r) == 3 and set(r[1:]) <= {'correct', 'incorrect', 'uncertain'} for r in reviews)
        tables = [{**t, 'cells': [dict(zip(FIELDS, c)) for c in t['cells']]} for t in response['reviewed_tables']]
        grid_errors = []
        for ti, table in enumerate(tables):
            try:
                validate_grid(table)
            except ValueError as error:
                grid_errors.append({'table': ti, 'reason': str(error)})
        reference = labels[item['sample_id']]
        before, after = score(reference, {'tables': request['tables']}), score(reference, {'tables': tables})
        before_scores.append(before); reviewed_scores.append(after)
        refs = {(c.get('table', 0), c['row'], c['column']): c for c in reference['targets']}
        count = Counter()
        cells = []
        for cid, sv, tv in reviews:
            ti, c = inputs[cid]
            ref = refs.get((ti, c['row'], c['column']))
            structure_good = bool(ref and all(c[k] == ref[k] for k in ('row_span', 'column_span')))
            joint_good = bool(structure_good and normalize(c['text']) == normalize(ref['text']))
            decision = 'incorrect' if 'incorrect' in (sv, tv) else ('uncertain' if 'uncertain' in (sv, tv) else 'correct')
            count['input_cells'] += 1
            count['actually_good' if joint_good else 'actually_bad'] += 1
            count['review_' + decision] += 1
            if decision == 'correct': count['true_approvals' if joint_good else 'false_approvals'] += 1
            if decision == 'incorrect': count['false_rejections' if joint_good else 'detected_errors'] += 1
            cells.append({'id': cid, 'structure_verdict': sv, 'text_verdict': tv, 'decision': decision,
                          'reference_structure_correct': structure_good, 'reference_joint_correct': joint_good})
        predicted = {(ti, c['row'], c['column']): c for ti, t in enumerate(tables) for c in t['cells']}
        raw_exact = sum(bool((c := predicted.get((r.get('table', 0), r['row'], r['column']))) and
                             all(c[k] == r[k] for k in ('row_span', 'column_span', 'text'))) for r in reference['targets'])
        count['reviewed_raw_exact_targets'] += raw_exact
        count['unresolved_locations'] += len(response['unresolved'])
        totals.update(count)
        cases.append({**item, 'counts': dict(count), 'before': {k:v for k,v in before.items() if k != 'rows'},
                      'reviewed': {k:v for k,v in after.items() if k != 'rows'}, 'grid_errors': grid_errors,
                      'table_verdict': response['table_verdict'], 'cell_reviews_scored': cells})
    def ratio(a,b): return totals[a] / totals[b] if totals[b] else None
    result = {'run': RUN, 'reference_sha256': sha(labels_path), 'cases': cases, 'counts': dict(totals),
              'before': aggregate(before_scores), 'reviewed_reference': aggregate(reviewed_scores),
              'approval_precision': ratio('true_approvals','review_correct'),
              'error_flag_precision': ratio('detected_errors','review_incorrect'),
              'error_detection_recall': ratio('detected_errors','actually_bad'),
              'decision_coverage': 1-ratio('review_uncertain','input_cells'),
              'production_edits': 0, 'actual_human_reviews': 0,
              'scope': 'model reviewer pilot; strict-slot agreement with public labels, not source geometry proof or production repair gain'}
    save(AUDIT / 'results.json', result)
    print(json.dumps({k:v for k,v in result.items() if k != 'cases'}, ensure_ascii=False, indent=2))


def compare_reference():
    """Post-hoc deterministic comparison; never change frozen model responses."""
    from score_luna_structure_sim import normalize
    result = read(AUDIT / 'results.json')
    for lock in read(AUDIT / 'response-lock.json'):
        assert sha(BUILD / 'inbox' / lock['case_id'] / 'response.json') == lock['response_sha256']
    totals = Counter()
    cases = []
    for case in result['cases']:
        folder = BUILD / 'inbox' / case['case_id']
        request, response = read(folder / 'request.json'), read(folder / 'response.json')
        reviewed = {(ti,c[0],c[1]):c for ti,t in enumerate(response['reviewed_tables']) for c in t['cells']}
        uncertain = {(u['table'],u['row'],u['column']) for u in response['unresolved']}
        truth = {r['id']:r['reference_joint_correct'] for r in case['cell_reviews_scored']}
        count = Counter()
        decisions = []
        for ti, table in enumerate(request['tables']):
            for c in table['cells']:
                key = (ti,c['row'],c['column'])
                target = reviewed.get(key)
                matches = bool(target and (c['row_span'],c['column_span']) == tuple(target[2:4]) and
                               normalize(c['text']) == normalize(target[4]))
                decision = 'uncertain' if key in uncertain else ('correct' if matches else 'incorrect')
                count['cells'] += 1
                count[decision] += 1
                if decision == 'correct': count['true_approvals' if truth[c['id']] else 'false_approvals'] += 1
                if decision == 'incorrect': count['false_rejections' if truth[c['id']] else 'detected_errors'] += 1
                decisions.append({'id':c['id'], 'decision':decision})
        totals.update(count)
        cases.append({'case_id':case['case_id'], 'counts':dict(count), 'decisions':decisions})
    summary = {
        'rule':'Compare full frozen Astra reference to input by strict slot, span and historical text normalization; uncertainty propagates by location',
        'scope':'post-hoc deterministic workflow analysis; no model retry or label-based modification; requires new validation before general precision claims',
        'counts':dict(totals), 'cases':cases,
        'approval_precision':totals['true_approvals']/totals['correct'] if totals['correct'] else None,
        'error_detection_recall':totals['detected_errors']/result['counts']['actually_bad'] if result['counts']['actually_bad'] else None,
    }
    save(AUDIT / 'reference-comparison.json', summary)
    print(json.dumps({k:v for k,v in summary.items() if k != 'cases'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare','evaluate','compare-reference'])
    args = parser.parse_args()
    {'prepare': prepare, 'evaluate': evaluate, 'compare-reference': compare_reference}[args.phase]()

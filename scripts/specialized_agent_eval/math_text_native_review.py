"""Reconcile actual rendered native cells with original model task ledgers.

Only a new byte copy of the independently read native database is opened.
Native original results stay immutable; separate before/after scores isolate
display parsing on the exact same model outputs, sources and failure scope.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment
from quality import paired_report, score_files


def main():
    require_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('independent', 'truth', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    independent, truth, output = map(fpath, (args.independent, args.truth, args.output))
    if output.exists() or truth.parent.name not in {'development', 'validation'}:
        raise ValueError('New native display-review identity required')
    review = read_json(independent / 'review.json')
    source_path = independent / 'verified-observations.json'
    observations = read_json(source_path)
    if (review['status'] != 'passed' or review['verified_observations_sha256'] != digest(source_path)
            or review['xlsx_readback']['status'] != 'passed'):
        raise ValueError('Actual native source/Excel readback required')
    output.mkdir(parents=True)
    originals = [p for p in independent.glob('workbench.sqlite3*') if p.is_file()]
    hashes = {p.name: digest(p) for p in originals}
    for path in originals:
        shutil.copyfile(path, output / path.name)
        if digest(output / path.name) != hashes[path.name]:
            raise ValueError('Native ledger byte copy differs')
    with closing(sqlite3.connect(output / 'workbench.sqlite3')) as db:
        db.row_factory = sqlite3.Row
        results = {row['id']: dict(row) for row in db.execute('SELECT * FROM results')}
        tasks = {row['id']: dict(row) for row in db.execute('SELECT * FROM tasks')}
    before = deepcopy(observations)
    changed, checked = [], []
    for row in before['records']:
        if row['status'] != 'success':
            continue
        raw = row['candidate']['original']
        sources = raw['screen']['sources']
        model_tables, source_bindings = [], []
        for source in sources:
            result = results[source['result_id']]
            task = tasks[source['task_id']]
            original, edited = json.loads(result['original']), json.loads(result['edited'])
            if (source['revision'] != 0 or result['revision'] != 0 or task['status'] != 'succeeded'
                    or task['kind'] != 'ocr' or result['task_id'] != task['id']
                    or source['task_id'] != result['task_id']
                    or edited.get('tables') != original.get('tables')
                    or source['text_sha256'] != __import__('hashlib').sha256(edited.get('text', '').encode()).hexdigest()):
                raise ValueError('Original native engine ledger was edited or mismatched')
            model_tables.extend(original.get('tables', []))
            source_bindings.append({'task_id': task['id'], 'result_id': result['id'],
                'revision': result['revision'], 'engine': task['engine'],
                'model_original_sha256': __import__('hashlib').sha256(result['original'].encode()).hexdigest()})
        if len(model_tables) != len(raw['tables']):
            raise ValueError('Final table count differs from original model table sequence')
        changes = raw['screen']['table_text_rendering']['changes']
        lookup = {(c['table'], c['row'], c['column']): c for c in changes}
        if len(lookup) != len(changes):
            raise ValueError('Duplicate rendering cell identity')
        for ti, (original_table, current_table) in enumerate(zip(model_tables, raw['tables'])):
            actual = {(c['row'], c['column']): c for c in original_table['cells']}
            if len(actual) != len(current_table['cells']):
                raise ValueError('Rendering changed cell count')
            for cell in current_table['cells']:
                original = actual[cell['row'], cell['column']]
                change = lookup.get((ti, cell['row'], cell['column']))
                if (original.get('row_span', 1), original.get('column_span', 1)) != (cell.get('row_span', 1), cell.get('column_span', 1)):
                    raise ValueError('Rendering changed a merge')
                if change:
                    if change['before'] != original['text'] or change['after'] != cell['text'] or change not in cell.get('provenance', []):
                        raise ValueError('Rendered cell lacks exact original engine text/provenance')
                    changed.append({'id': row['id'], **change, 'source_bindings': source_bindings})
                elif original['text'] != cell['text']:
                    raise ValueError('Cell change lacks a rendering receipt')
                cell['text'] = original['text']
        edited_tables = row['candidate'].get('edited', {}).get('tables')
        if edited_tables is not None:
            for ti, table in enumerate(edited_tables):
                for cell in table['cells']:
                    original = next(c for c in model_tables[ti]['cells']
                                    if (c['row'], c['column']) == (cell['row'], cell['column']))
                    cell['text'] = original['text']
        checked.append({'id': row['id'], 'original_model_sources': source_bindings, 'rendered_cells': len(changes)})
    before['evidence_kind'] = 'cpu_native_same_outputs_before_display'
    before['scope'] = 'Counterfactual original model cell text reconstructed from actual immutable native task ledgers; not a new export'
    frozen_json(output / 'before-display-observations.json', before)
    actual_score = score_files(truth, source_path)
    original_score = score_files(truth, output / 'before-display-observations.json')
    frozen_json(output / 'actual-quality.json', actual_score)
    frozen_json(output / 'before-display-quality.json', original_score)
    if hashes != {p.name: digest(p) for p in originals}:
        raise ValueError('Prior independent native evidence changed')
    report = {'format': 'native-explicit-math-display-independent-review-v1', 'status': 'passed',
        'bindings': [bound(p) for p in (independent / 'review.json', source_path, truth, Path(__file__))],
        'actual_native_xlsx': review['xlsx_readback'], 'source_inputs': len(observations['records']),
        'rendered_cells': len(changed), 'source_ledger_checks': checked, 'changes': changed,
        'same_native_outputs_paired': paired_report(original_score, actual_score),
        'before': original_score['summary'], 'after': actual_score['summary'],
        'native_original_database_opened': False, 'native_or_prior_evidence_mutated': False,
        'new_model_calls': 0, 'human_review_state_changed': False, 'human_review_reduction_qualified': False,
        'final_qualification': False, 'holdout_read': False}
    frozen_json(output / 'review.json', report)
    print({'status': 'passed', 'native_xlsx': review['xlsx_readback']['workbooks'],
           'rendered_cells': len(changed), 'paired': report['same_native_outputs_paired']['totals']})


if __name__ == '__main__':
    main()

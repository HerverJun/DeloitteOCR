"""Freeze, derive, then separately score explicit model display-syntax parsing.

Derived edits retain original native candidates, source and review state. This
CPU evidence does not claim a new native export or reduced human review.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import shutil
import sys

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import bound, require_environment


def freeze(args):
    source, output = map(fpath, (args.source, args.output))
    if output.exists():
        raise ValueError('Use a new immutable renderer freeze')
    output.mkdir(parents=True)
    shutil.copyfile(source, output / 'renderer.py')
    frozen_json(output / 'policy.json', {
        'format': 'explicit-math-text-rendering-freeze-v1',
        'utc': datetime.now(timezone.utc).isoformat(), 'renderer': bound(output / 'renderer.py'),
        'source': bound(source), 'helper': bound(__file__),
        'development_informed': True, 'validation_content_previously_exposed': True,
        'rule_truth_read': False, 'final_holdout_read': False,
        'numeric_powers_currency_unknown_syntax': 'retain complete original cell',
        'digit_sequence_and_original_minus_characters': 'never correct or normalize',
        'original_native_ledger': 'immutable; only separate derived edit proposal',
        'review_state': 'unchanged; display parsing never supplies approval',
        'product_default_changed': False, 'automatic_release_qualified': False})
    print({'frozen_renderer_sha256': digest(output / 'renderer.py')})


def derive(args):
    capsule, source, inputs_path, output = map(fpath,
        (args.capsule, args.observations, args.inputs, args.output))
    if output.exists() or inputs_path.parent.name not in {'development', 'validation'}:
        raise ValueError('New development/validation-only derived identity required')
    policy = read_json(capsule / 'policy.json')
    if bound(capsule / 'renderer.py') != policy['renderer']:
        raise ValueError('Frozen renderer changed')
    original, inputs = read_json(source), read_json(inputs_path)
    samples = {row['id']: row for row in inputs['samples']}
    records = {row['id']: row for row in original['records']}
    if (inputs.get('truth_available_to_inference') is not False
            or original['inputs_sha256'] != digest(inputs_path)
            or samples.keys() != records.keys() or len(records) != len(original['records'])
            or len(samples) != len(inputs['samples'])):
        raise ValueError('Full truth-free source denominator differs')
    for sid, row in records.items():
        if row['image_sha256'] != samples[sid]['image_sha256'] or digest(samples[sid]['image']) != row['image_sha256']:
            raise ValueError('Source pixels changed')
    output.mkdir(parents=True)
    opened = []
    def audit(event, values):
        if event in {'subprocess.Popen', 'socket.connect', 'socket.getaddrinfo', 'os.system'}:
            raise ValueError('Renderer cannot invoke a model or network')
        if event != 'open' or not isinstance(values[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(values[0])).absolute()
        mode, flags = values[1:3]
        writing = (isinstance(mode, str) and any(ch in mode for ch in 'wax+')
                   or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not path.resolve().is_relative_to(output):
            raise ValueError('Renderer write escaped its F: output')
        if 'truth' in path.name.lower() or any(part in {'holdout', 'challenge'} for part in path.parts):
            raise ValueError('Truth/final scope forbidden during derivation')
        opened.append(str(path))
    sys.addaudithook(audit)
    spec = importlib.util.spec_from_file_location('frozen_explicit_math_renderer', capsule / 'renderer.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    derived = deepcopy(original)
    changes, rejections = [], []
    for row in derived['records']:
        if row['status'] != 'success':
            continue
        native = row['candidate']['original']
        effective = deepcopy(native)
        effective['tables'] = deepcopy(row['candidate'].get('edited', {}).get('tables', native.get('tables', [])))
        rendered = module.render_screen_table_text(effective)
        receipt = rendered['screen']['table_text_rendering']
        if receipt['changes']:
            row['candidate'].setdefault('edited', {})['tables'] = rendered['tables']
            row['table_text_rendering_proposal'] = receipt
        changes.extend({'id': row['id'], **change} for change in receipt['changes'])
        rejections.extend({'id': row['id'], **reject} for reject in receipt['rejected'])
        if row['candidate']['original'] != records[row['id']]['candidate']['original']:
            raise ValueError('Original model candidate changed')
        for key in ('review_image', 'review_regions', 'review_state', 'human_confirmed', 'automatic_delivery'):
            if row.get(key) != records[row['id']].get(key):
                raise ValueError('Display parser changed review/approval state')
    derived.update(evidence_kind='cpu_derived_explicit_math_display', truth_read=False,
                   native_observations_sha256=digest(source), frozen_policy_sha256=digest(capsule / 'policy.json'))
    frozen_json(output / 'observations.json', derived)
    frozen_json(output / 'changes.json', {'changes': changes, 'rejections': rejections})
    frozen_json(output / 'result.json', {
        'format': 'explicit-math-text-derived-v1', 'scope_inputs': len(records),
        'evaluation_split': inputs_path.parent.name, 'policy': bound(capsule / 'policy.json'),
        'original_observations': bound(source), 'inputs': bound(inputs_path),
        'derived_observations': bound(output / 'observations.json'), 'changes': bound(output / 'changes.json'),
        'changed_cells': len(changes), 'changed_images': len({row['id'] for row in changes}),
        'rejected_cells': len(rejections), 'rejection_reasons': dict(Counter(row['reason'] for row in rejections)),
        'original_candidates_and_review_states_unchanged': True,
        'truth_read': False, 'holdout_read': False, 'model_calls': 0,
        'product_default_changed': False, 'native_export_qualified': False,
        'human_review_reduction_qualified': False, 'audited_file_accesses': opened})
    print({'scope_inputs': len(records), 'changed_cells': len(changes), 'changed_images': len({row['id'] for row in changes})})


def score(args):
    replay, truth, output = map(fpath, (args.replay, args.truth, args.output))
    if output.exists() or truth.parent.name not in {'development', 'validation'}:
        raise ValueError('New complete development/validation score required')
    receipt = read_json(replay / 'result.json')
    if (bound(replay / 'observations.json') != receipt['derived_observations']
            or bound(receipt['original_observations']['path']) != receipt['original_observations']
            or truth.parent.name != receipt['evaluation_split']):
        raise ValueError('Frozen derivation/source split differs')
    from quality import paired_report, score_files
    before = score_files(truth, fpath(receipt['original_observations']['path']))
    after = score_files(truth, replay / 'observations.json')
    report = {'format': 'explicit-math-text-independent-score-v1',
        'bindings': [bound(p) for p in (replay / 'result.json', replay / 'changes.json', truth, Path(__file__))],
        'before': before['summary'], 'after': after['summary'], 'paired': paired_report(before, after),
        'cohorts': after['cohort_grouped'], 'original_candidates_and_review_states_unchanged': True,
        'rule_truth_read': False, 'scoring_truth_read': True, 'holdout_read': False,
        'actual_native_export_qualified': False, 'human_review_reduction_qualified': False,
        'validation_content_previously_exposed': True, 'product_default_changed': False}
    output.mkdir(parents=True)
    frozen_json(output / 'quality.json', after)
    frozen_json(output / 'report.json', report)
    print({'paired': report['paired']['totals'],
           'visible_exact': after['summary']['totals']['visible_exact_cells'],
           'critical_exact': after['summary']['totals']['visible_critical_exact_cells']})


if __name__ == '__main__':
    require_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('freeze', 'derive', 'score'))
    for name in ('source', 'capsule', 'observations', 'inputs', 'replay', 'truth', 'output'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    {'freeze': freeze, 'derive': derive, 'score': score}[args.phase](args)

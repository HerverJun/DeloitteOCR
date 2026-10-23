"""Frozen, source-grouped AIGC evaluation. No fitting on holdout labels.

Each prediction has sample_id, group_id, split, domain, label, variant, engine,
status and signal_score (larger means more AI-related signal). Labels are
normal, generated and local_edit. Non-successes remain in denominators.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import random

VARIANTS = ('original', 'jpeg90', 'jpeg70', 'half_jpeg70', 'screenshot')
TARGETS = {'development': {'normal': 400, 'generated': 400, 'local_edit': 100},
           'calibration': {'document/normal': 1000, 'general/normal': 1000},
           'test': {'document/normal': 1000, 'general/normal': 1000,
                    'document/generated': 800, 'general/generated': 400, 'local_edit': 400}}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def good(row):
    return (row.get('status') == 'succeeded' and
            isinstance(row.get('signal_score'), (int, float)) and
            not isinstance(row.get('signal_score'), bool) and
            math.isfinite(row['signal_score']) and 0 <= row['signal_score'] <= 1)


def validate(rows):
    identities, groups, hashes, families, samples = set(), {}, {}, {}, {}
    for row in rows:
        for key in ('sample_id', 'group_id', 'split', 'domain', 'label', 'variant', 'engine', 'status'):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ValueError('Missing identity: ' + key)
        if row['split'] not in TARGETS or row['domain'] not in {'document', 'general'}:
            raise ValueError('Unknown split/domain')
        if row['label'] not in {'normal', 'generated', 'local_edit'} or row['variant'] not in VARIANTS:
            raise ValueError('Unknown label/variant')
        key = (row['engine'], row['sample_id'], row['variant'])
        if key in identities:
            raise ValueError('Duplicate prediction identity')
        identities.add(key)
        metadata = tuple(row.get(k) for k in ('group_id','split','domain','label','generator','prompt_family',
                                             'original_sha256','preprocessing_version','model_identity_sha256'))
        sample = (row['engine'], row['sample_id'])
        if samples.setdefault(sample, metadata) != metadata:
            raise ValueError('Inconsistent sample metadata across variants')
        for registry, identity in ((groups, row['group_id']), (hashes, row.get('original_sha256')),
                                   (families, row.get('prompt_family'))):
            if identity:
                if registry.setdefault(identity, row['split']) != row['split']:
                    raise ValueError('Source/hash/prompt family leaks across splits: ' + identity)
        if row.get('status') == 'succeeded' and not good(row):
            raise ValueError('Invalid successful score')
    return {'unique_source_groups': len(groups), 'unique_predictions': len(identities),
            'protocol': 'clear-aigc-evaluation-v1', 'hash': canonical_hash(rows)}


def calibrate(rows, engine, *, target_fpr=.005):
    """Choose strict > threshold from independent normal sources only."""
    validate(rows)
    if not 0 <= target_fpr < 1:
        raise ValueError('Invalid target FPR')
    groups, samples = defaultdict(list), defaultdict(list)
    for row in rows:
        if row['engine'] == engine and row['split'] == 'calibration':
            if row['label'] != 'normal':
                raise ValueError('Calibration accepts normal sources only')
            groups[(row['domain'], row['group_id'])].append(row)
            samples[row['sample_id']].append(row)
    incomplete = {sample for sample, values in samples.items() if {r['variant'] for r in values} != set(VARIANTS)}
    versions = {r.get('preprocessing_version') for r in rows if r['engine'] == engine}
    models = {r.get('model_identity_sha256') for r in rows if r['engine'] == engine}
    weight_maps = {canonical_hash(r['model_weight_hashes']): r['model_weight_hashes'] for r in rows
                   if r['engine'] == engine and r.get('model_weight_hashes')}
    if len(versions) > 1 or len(models) > 1:
        raise ValueError('Model or preprocessing changed within one engine evaluation')
    if len(weight_maps) > 1:
        raise ValueError('Weights changed within one engine evaluation')
    domains, thresholds, failures = {}, [], []
    for domain in ('document', 'general'):
        scores = []
        for (actual, group), values in groups.items():
            if actual != domain:
                continue
            if (any(row['sample_id'] in incomplete for row in values) or
                    {row['variant'] for row in values} != set(VARIANTS) or any(not good(row) for row in values)):
                failures.append({'domain': domain, 'group_id': group, 'reason': 'missing_or_failed_variant'})
                continue
            scores.append(max(row['signal_score'] for row in values))
        if scores:
            scores.sort(reverse=True)
            allowed = math.floor(len(scores) * target_fpr)
            threshold = scores[allowed]
            thresholds.append(threshold)
            domains[domain] = {'complete_groups': len(scores), 'threshold': threshold,
                               'empirical_fpr': sum(score > threshold for score in scores)/len(scores)}
        else:
            domains[domain] = {'complete_groups': 0, 'threshold': None, 'empirical_fpr': None}
    threshold = max(thresholds) if len(thresholds) == 2 else None
    result = {'format': 'clear-aigc-calibration-v1', 'engine': engine, 'comparison': 'strictly_greater',
              'threshold': threshold, 'target_fpr': target_fpr, 'domains': domains,
              'failed_groups': failures, 'qualified': False,
              'sample_requirements_met': all(v['complete_groups'] >= 1000 for v in domains.values()) and not failures,
              'basis': 'normal_source_max_over_all_five_transformations',
              'preprocessing_version': next(iter(versions), None),
              'model_identity_sha256': next(iter(models), None),
              'model_weight_hashes': next(iter(weight_maps.values()), {}),
              'input_hash': canonical_hash([r for r in rows if r['engine'] == engine and r['split'] == 'calibration'])}
    result['calibration_id'] = 'aigc-'+canonical_hash(result)[:24]
    result['sha256'] = canonical_hash(result)
    return result


def binomial_upper(k, n, confidence=.95):
    """Exact one-sided Clopper-Pearson upper bound, including zero events."""
    if not 0 <= k <= n or n <= 0:
        raise ValueError('Invalid binomial count')
    if k == n:
        return 1.
    if k == 0:
        return -math.expm1(math.log1p(-confidence)/n)
    def cdf(p):
        logs = [math.lgamma(n+1)-math.lgamma(i+1)-math.lgamma(n-i+1)
                + i*math.log(p)+(n-i)*math.log1p(-p) for i in range(k+1)]
        maximum = max(logs)
        return math.exp(maximum)*sum(math.exp(v-maximum) for v in logs)
    low, high = k/n, 1.-1e-15
    for _ in range(70):
        mid = (low+high)/2
        if cdf(mid) > 1-confidence:
            low = mid
        else:
            high = mid
    return (low+high)/2


def ranking_metrics(rows):
    """Tie-aware AUROC/AP on successful full-generation/normal predictions."""
    pairs = sorted((r['signal_score'], r['label'] == 'generated') for r in rows
                   if good(r) and r['label'] in {'normal', 'generated'})
    pos, neg = sum(y for _, y in pairs), sum(not y for _, y in pairs)
    if not pos or not neg:
        return {'auroc': None, 'auprc': None, 'ranking_successes': len(pairs)}
    rank_sum, index = 0., 0
    while index < len(pairs):
        end = index+1
        while end < len(pairs) and pairs[end][0] == pairs[index][0]:
            end += 1
        rank_sum += sum(y for _, y in pairs[index:end])*(index+1+end)/2
        index = end
    auc = (rank_sum-pos*(pos+1)/2)/(pos*neg)
    seen, true, ap, index = 0, 0, 0., len(pairs)
    while index:
        start = index-1
        while start and pairs[start-1][0] == pairs[index-1][0]:
            start -= 1
        hit = sum(y for _, y in pairs[start:index])
        true += hit
        seen += index-start
        ap += hit/pos*(true/seen)
        index = start
    return {'auroc': auc, 'auprc': ap, 'ranking_successes': len(pairs),
            'ranking_excludes_technical_failures': True}


def bootstrap_recall(rows, threshold, *, draws=2000):
    groups = defaultdict(list)
    for row in rows:
        groups[row['group_id']].append(good(row) and row['signal_score'] > threshold)
    values = list(groups.values())
    if not values:
        return None
    rng = random.Random(20261022)
    rates = []
    for _ in range(draws):
        chosen = rng.choices(values, k=len(values))
        rates.append(sum(sum(g) for g in chosen)/sum(map(len, chosen)))
    rates.sort()
    return [rates[int(.025*(draws-1))], rates[int(.975*(draws-1))]]


def metrics(rows, threshold):
    normal = [r for r in rows if r['label'] == 'normal']
    generated = [r for r in rows if r['label'] == 'generated']
    edited = [r for r in rows if r['label'] == 'local_edit']
    false = sum(good(r) and r['signal_score'] > threshold for r in normal)
    normal_failed = sum(not good(r) for r in normal)
    true = sum(good(r) and r['signal_score'] > threshold for r in generated)
    generated_groups = defaultdict(list)
    for r in generated:
        generated_groups[r['group_id']].append(good(r) and r['signal_score'] > threshold)
    normal_groups = defaultdict(list)
    for r in normal:
        normal_groups[r['group_id']].append(r)
    group_errors = sum(any(not good(r) or r['signal_score'] > threshold for r in g)
                       for g in normal_groups.values())
    return {'total': len(rows), 'failed_or_rejected': sum(not good(r) for r in rows),
            'normal': len(normal), 'normal_failed': normal_failed, 'false_positives': false,
            'observed_fpr': false/len(normal) if normal else None,
            'conservative_fpr_including_unknown': (false+normal_failed)/len(normal) if normal else None,
            'normal_source_groups': len(normal_groups), 'normal_error_groups_including_unknown': group_errors,
            'fpr_upper_95': binomial_upper(group_errors, len(normal_groups)) if normal_groups else None,
            'generated': len(generated), 'true_positives': true,
            'generated_source_groups': len(generated_groups),
            'source_balanced_recall': (sum(sum(v)/len(v) for v in generated_groups.values())/len(generated_groups)
                                       if generated_groups else None),
            'recall': true/len(generated) if generated else None,
            'recall_source_bootstrap_95': bootstrap_recall(generated, threshold),
            'local_edits': len(edited), 'local_edit_signals': sum(good(r) and r['signal_score'] > threshold for r in edited),
            **ranking_metrics(rows)}


def summarize(rows, engine, calibration):
    validate(rows)
    if calibration['engine'] != engine:
        raise ValueError('Calibration engine mismatch')
    digest = calibration.get('sha256')
    if digest != canonical_hash({k: v for k, v in calibration.items() if k != 'sha256'}):
        raise ValueError('Calibration changed after freezing')
    threshold = calibration.get('threshold')
    result = {'format': 'clear-aigc-evaluation-report-v1', 'engine': engine,
              'calibration_sha256': digest, 'qualified': False, 'groups': {}, 'gaps': []}
    if threshold is None:
        result['gaps'].append('No frozen common threshold: both normal domains are required')
        return result
    selected = [r for r in rows if r['engine'] == engine]
    if not calibration.get('preprocessing_version') or not calibration.get('model_identity_sha256'):
        result['gaps'].append('Pinned model/preprocessing identity missing')
    for row in selected:
        if (row.get('preprocessing_version') != calibration.get('preprocessing_version') or
                row.get('model_identity_sha256') != calibration.get('model_identity_sha256')):
            raise ValueError('Holdout model differs from frozen calibration')
    test = [r for r in selected if r['split'] == 'test']
    paired = defaultdict(list)
    for row in test:
        paired[row['sample_id']].append(row)
    for sample, values in paired.items():
        absent = set(VARIANTS)-{r['variant'] for r in values}
        if absent:
            result['gaps'].append(sample+': missing paired holdout variants')
            test.extend({**values[0], 'variant':v, 'status':'missing', 'signal_score':None} for v in sorted(absent))
    development_counts = {label: len({r['group_id'] for r in selected if r['split']=='development' and r['label']==label})
                          for label in ('normal','generated','local_edit')}
    result['development_source_counts'] = development_counts
    if any(development_counts[k] < n for k,n in TARGETS['development'].items()):
        result['gaps'].append('Development source targets incomplete; no qualified product selection')
    for domain in ('document', 'general'):
        for variant in VARIANTS:
            subset = [r for r in test if r['domain'] == domain and r['variant'] == variant]
            result['groups'][domain+'/'+variant] = metrics(subset, threshold)
        all_domain = [r for r in test if r['domain'] == domain]
        result['groups'][domain+'/worst_transform'] = metrics(all_domain, threshold)
    if not calibration.get('sample_requirements_met'):
        result['gaps'].append('Calibration source targets incomplete')
    for name, group in result['groups'].items():
        if name.endswith('/worst_transform'):
            if group['fpr_upper_95'] is None or group['fpr_upper_95'] > .01:
                result['gaps'].append(name+': normal-source FPR upper bound exceeds 1% or unavailable')
            continue
        required = 800 if name.startswith('document/') else 400
        if group['normal_source_groups'] < 1000 or group['generated_source_groups'] < required:
            result['gaps'].append(name+': holdout source targets incomplete')
        required_recall = .7 if name.endswith('/original') else .5
        if group['source_balanced_recall'] is None or group['source_balanced_recall'] < required_recall:
            result['gaps'].append(name+': recall target not met')
        if not group['total'] or group['failed_or_rejected']/group['total'] > .01:
            result['gaps'].append(name+': technical coverage target not met')
    generators = {r.get('generator') for r in selected if r['label'] == 'generated' and r.get('generator')}
    development = {r.get('generator') for r in selected if r['label'] == 'generated' and r['split'] != 'test'}
    heldout = {r.get('generator') for r in test if r['label'] == 'generated'}-development-{None}
    result['generators'] = sorted(generators)
    result['holdout_only_generators'] = sorted(heldout)
    if len(generators) < 6 or len(heldout) < 2:
        result['gaps'].append('At least six generators and two holdout-only generators required')
    local_groups = {r['group_id'] for r in test if r['label'] == 'local_edit' and r['variant'] == 'original'}
    if len(local_groups) < 400:
        result['gaps'].append('Local-edit holdout source target incomplete')
    result['qualified'] = not result['gaps']
    result['business_validated'] = False
    result['input_hash'] = canonical_hash(rows)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('predictions', type=Path)
    parser.add_argument('--engine', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.predictions.read_text('utf-8').splitlines() if line.strip()]
    calibration = calibrate(rows, args.engine)
    report = summarize(rows, args.engine, calibration)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in [('calibration.json', calibration), ('report.json', report)]:
        (args.output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'qualified': report['qualified'], 'gaps': report['gaps']}, ensure_ascii=False))


if __name__ == '__main__':
    main()

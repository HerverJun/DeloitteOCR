"""Run the frozen controlled corpus with explicit mock model outputs.

This measures pipeline regression, not OCR or live LLM extraction accuracy.
Use a separately reviewed business corpus and configured live endpoints before
claiming production precision/recall.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from workbench_platform.comparisons.compare import extract_and_compare
from workbench_platform.comparisons.report import render_report


DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'comparison_regression_v1.json'


def run_case(case):
    calls = []
    def call(*, stage, system, payload, schema):
        calls.append(stage)
        if stage.startswith('extract_'):
            side = stage[-1]
            visible = '\n'.join(block['text'] for block in payload['blocks'])
            facts = [deepcopy(fact) for fact in case[side]['extraction']['facts']
                     if any(entry['quote'] in visible for entry in fact['evidence'])]
            return {'data': {'facts': facts}, 'model': 'controlled-extraction-fixture-v1',
                    'usage': {'input_tokens': 0, 'output_tokens': 0}}
        decisions = []
        for pair in payload['pairs']:
            answer = case['semantic_answers'].get(pair['field']['key'])
            if answer is None:
                raise AssertionError('Fixture lacks an independently specified semantic answer: ' + case['id'])
            decisions.append({'pair_id': pair['pair_id'], 'status': answer['status'], 'explanation': answer['explanation'],
                              'evidence_ids': ['fabricated-evidence'] if answer.get('invalid_evidence') else pair['evidence_ids']})
        return {'data': {'decisions': decisions}, 'model': 'controlled-semantic-fixture-v1',
                'usage': {'input_tokens': 0, 'output_tokens': 0}}
    result = extract_and_compare(case['a']['document'], case['b']['document'], case['profile'], call)
    actual = {(item['entity'], item.get('work_item', ''), item['field']): item['status'] for item in result['items']}
    expected = {(item['entity'], item.get('work_item', ''), item['field']): item['status'] for item in case['gold']}
    mismatches = [{'key': list(key), 'expected': expected.get(key), 'actual': actual.get(key)}
                  for key in sorted(set(actual) | set(expected)) if actual.get(key) != expected.get(key)]
    html = render_report(result)
    assert '<script>' not in html and '<script ' not in html
    assert html == render_report(deepcopy(result))
    return {'result': result, 'actual': actual, 'expected': expected, 'mismatches': mismatches, 'calls': calls}


def evaluate(corpus=DEFAULT_CORPUS, split='all'):
    content = Path(corpus).read_bytes()
    data = json.loads(content)
    cases = [case for case in data['cases'] if split == 'all' or case['split'] == split]
    tp = fp = fn = correct = total = 0
    results = []
    distribution = Counter()
    fact_count = 0
    evidence_count = 0
    for case in cases:
        evaluated = run_case(case)
        actual, expected = evaluated['actual'], evaluated['expected']
        for key in set(actual) | set(expected):
            predicted, gold = actual.get(key), expected.get(key)
            total += 1
            correct += predicted == gold
            tp += predicted == gold == 'different'
            fp += predicted == 'different' and gold != 'different'
            fn += gold == 'different' and predicted != 'different'
            distribution[gold or 'unexpected'] += 1
        fact_count += sum(len(case[side]['extraction']['facts']) for side in ('a', 'b'))
        evidence_count += sum(len(item['evidence']) for item in evaluated['result']['items'])
        results.append({'id': case['id'], 'split': case['split'], 'name': case['name'],
                        'passed': not evaluated['mismatches'], 'mismatches': evaluated['mismatches']})
    return {'corpus_version': data['version'], 'corpus_sha256': hashlib.sha256(content).hexdigest(),
        'mode': 'controlled_mock_model_regression', 'split': split, 'pairs': len(cases), 'source_facts': fact_count,
        'gold_items': total, 'correct_items': correct, 'classification_accuracy': correct / total if total else None,
        'difference_precision': tp / (tp + fp) if tp + fp else None,
        'difference_recall': tp / (tp + fn) if tp + fn else None,
        'difference_counts': {'true_positive': tp, 'false_positive': fp, 'false_negative': fn},
        'gold_distribution': dict(sorted(distribution.items())), 'verified_evidence_references': evidence_count,
        'passed': all(case['passed'] for case in results), 'cases': results,
        'limitations': ['合成材料和预设模型响应，仅验证归一、对齐、证据校验、分类与报告代码。',
                       '未调用真实 OCR、多模态或比较供应商，不能据此声称真实业务精确率或召回率。',
                       '开发集与封存集各10对；真实业务验收仍需人工标注材料及配置后的模型实测。']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--split', choices=['all', 'development', 'holdout'], default='all')
    parser.add_argument('--output', type=Path)
    arguments = parser.parse_args()
    result = evaluate(arguments.corpus, arguments.split)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if arguments.output:
        output = arguments.output.resolve()
        if output.drive.upper() != 'F:':
            raise ValueError('Evaluation output must remain on local F:')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding='utf-8')
    print(text)
    raise SystemExit(0 if result['passed'] else 1)

"""Frozen synthetic gold regression; no claims about live model accuracy."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('controlled_comparison_eval', ROOT / 'scripts' / 'evaluate_document_comparison.py')
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)
CORPUS = json.loads((ROOT / 'tests' / 'fixtures' / 'comparison_regression_v1.json').read_text('utf-8'))


def test_frozen_corpus_has_twenty_pairs_and_two_hundred_facts():
    assert len(CORPUS['cases']) == 20
    assert CORPUS['source_fact_count'] >= 200
    assert sum(case['split'] == 'development' for case in CORPUS['cases']) == 10
    assert sum(case['split'] == 'holdout' for case in CORPUS['cases']) == 10


@pytest.mark.parametrize('case', CORPUS['cases'], ids=[case['id'] for case in CORPUS['cases']])
def test_controlled_gold_comparison(case):
    actual = EVAL.run_case(case)
    assert not actual['mismatches'], actual['mismatches']


def test_score_is_explicitly_a_mock_regression_result():
    score = EVAL.evaluate(split='holdout')
    assert score['mode'] == 'controlled_mock_model_regression'
    assert score['difference_precision'] == 1.0
    assert score['difference_recall'] == 1.0
    assert len(score['limitations']) >= 3

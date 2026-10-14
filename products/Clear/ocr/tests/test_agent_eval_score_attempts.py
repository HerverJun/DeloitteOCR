"""F05 scorer checks with frozen development definitions only; no sealed content."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.agent_eval.score_attempts import IncompleteEvidence, aggregate, score_attempt


ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT = ROOT / 'build/ocr-agent-20260920-p0/fixtures-final/development/tasks.json'
CONTRACT = ROOT / 'scripts/agent_eval/scoring-contract.json'


@pytest.fixture(scope='module')
def definitions():
    return json.loads(DEVELOPMENT.read_text('utf-8'))


@pytest.fixture(scope='module')
def contract():
    return json.loads(CONTRACT.read_text('utf-8'))


def observation_from_development_predicates(task):
    # Test input only. Real attempts require a separate DB/file/event adapter.
    observation = {}
    for predicate in task['assertions']:
        target = observation
        parts = predicate['path'].split('.')
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = deepcopy(predicate['expected'])
    return observation


def attempt_for(task, repeat):
    return {
        'task_id': task['id'], 'repeat': repeat,
        'workspace_id': f'development-isolated-{task["id"]}-{repeat}',
        'observation': observation_from_development_predicates(task),
        'terminal_state': task['terminal_condition']['acceptable_states'][0],
        'explanation_score': 3, 'intervention': False, 'provider_status': 'received',
        'native_calls': {'proposed': 1, 'first_pass_valid': 1},
        'references': {'emitted': 1, 'valid': 1, 'mandatory_missing': 0},
        'usage': {'estimated': {'input_tokens': 2, 'output_tokens': 1, 'method': 'test estimate'}},
        'writes': {'total': 0, 'traceable': 0},
    }


def test_frozen_financial_development_facts_and_strict_types(definitions):
    task = next(t for t in definitions if t['scenario'] == 'S02')
    attempt = attempt_for(task, 1)
    assert score_attempt(task, attempt)['success']
    attempt['observation']['facts']['difference'] = 'wrong Decimal'
    result = score_attempt(task, attempt)
    assert not result['success']
    assert 'facts.difference' in result['failed_assertions']
    # Frozen invariant expects integer zero; bool False must not compare equal.
    attempt['observation']['invariants']['cross_project_effects'] = False
    with pytest.raises(IncompleteEvidence, match='invariants.cross_project_effects'):
        score_attempt(task, attempt)


def test_missing_assertion_and_duplicate_page_cannot_pass(definitions):
    task = next(t for t in definitions if any(p['op'] == 'set_equals' for p in t['assertions']))
    attempt = attempt_for(task, 1)
    pages = attempt['observation']['coverage']['requested_pages']
    pages[0] = pages[1]
    assert 'coverage.requested_pages' in score_attempt(task, attempt)['failed_assertions']
    del attempt['observation']['evidence']['all_resolve']
    assert 'evidence.all_resolve' in score_attempt(task, attempt)['failed_assertions']


def test_missing_usage_and_invalid_counts_refused(definitions):
    task = definitions[0]
    attempt = attempt_for(task, 1)
    attempt['usage'] = {'actual': {}}
    with pytest.raises(IncompleteEvidence, match='input_tokens'):
        score_attempt(task, attempt)
    attempt['usage'] = {'actual': {'input_tokens': 0, 'output_tokens': 0}}
    attempt['references']['valid'] = 2
    with pytest.raises(IncompleteEvidence, match='valid references'):
        score_attempt(task, attempt)


def test_schedule_and_isolation_fail_closed(definitions, contract):
    task = definitions[0]
    attempts = [attempt_for(task, repeat) for repeat in (1, 2, 3)]
    result = aggregate([task], attempts, contract, split='development')
    assert result['attempts'] == 3
    assert result['all_gates_met'] is False
    assert result['qualification_status'] == 'not_attested'
    with pytest.raises(IncompleteEvidence, match='scheduled attempt'):
        aggregate([task], attempts[:2], contract, split='development')
    attempts[2]['workspace_id'] = attempts[0]['workspace_id']
    with pytest.raises(IncompleteEvidence, match='workspace reused'):
        aggregate([task], attempts, contract, split='development')
    with pytest.raises(IncompleteEvidence, match='mixed split'):
        aggregate([task], attempts, contract, split='holdout')


def test_missing_reference_and_invalid_tool_count_fail_gates(definitions, contract):
    task = definitions[0]
    attempts = [attempt_for(task, repeat) for repeat in (1, 2, 3)]
    attempts[0]['references']['mandatory_missing'] = 1
    attempts[1]['native_calls']['first_pass_valid'] = 0
    result = aggregate([task], attempts, contract, split='development')
    assert result['references'] == {'valid': 3, 'denominator': 4}
    assert not result['gates']['reference_validity']
    assert not result['gates']['first_pass_valid_tool_call_rate']


def test_provider_failure_retained_and_impact_count_from_state(definitions, contract):
    task = definitions[0]
    attempts = [attempt_for(task, repeat) for repeat in (1, 2, 3)]
    attempts[0]['provider_status'] = 'error'
    attempts[1]['observation']['invariants']['duplicate_effects'] = 2
    result = aggregate([task], attempts, contract, split='development')
    assert result['attempts'] == 3
    assert result['success'] == 1
    assert result['high_impact_errors'] == 2
    assert not result['gates']['high_impact_errors']

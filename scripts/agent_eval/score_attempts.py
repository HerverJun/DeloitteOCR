"""Fail-closed, offline F05 attempt accounting. Never invokes a model or opens tasks itself.

The caller must supply independently collected observations. This module checks the
frozen task predicates and aggregates the frozen gates; it does not attest that an
observation came from the DB/filesystem, or grant a real-model qualification.
"""

from __future__ import annotations

from collections import Counter
from math import sqrt


class IncompleteEvidence(ValueError):
    """An attempt or schedule lacks evidence needed for a gate decision."""


IMPACT_FIELDS = {
    'cross_project_effects': 'cross_project_read_or_write',
    'unauthorized_effects': 'unauthorized_external_send_or_edit',
    'duplicate_effects': 'duplicate_effect',
    'stale_overwrites': 'stale_overwrite',
    'false_full_success': 'false_full_success',
}


def _required(mapping, key):
    if not isinstance(mapping, dict) or key not in mapping or mapping[key] is None:
        raise IncompleteEvidence(f"missing {key}")
    return mapping[key]


def _natural_number(value, name):
    if type(value) is not int or value < 0:
        raise IncompleteEvidence(f"{name} must be a nonnegative integer")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise IncompleteEvidence(f"{name} must be a boolean")
    return value


def _path(observation, dotted):
    value = observation
    for part in dotted.split('.'):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_equal(a, e) for a, e in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_equal(actual[k], v) for k, v in expected.items())
    return actual == expected


def _set_equal(actual, expected):
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    # Canonical typed JSON scalars/containers: bool and 1 remain distinct.
    def typed(value):
        if isinstance(value, list):
            return ('list', tuple(map(typed, value)))
        if isinstance(value, dict):
            return ('dict', tuple(sorted((k, typed(v)) for k, v in value.items())))
        return (type(value).__name__, value)
    actual_keys = list(map(typed, actual))
    expected_keys = list(map(typed, expected))
    return (len(set(actual_keys)) == len(actual_keys)
            and len(set(expected_keys)) == len(expected_keys)
            and set(actual_keys) == set(expected_keys))


def score_attempt(task, attempt):
    """Score a task against a supplied state observation and explicit audit counts.

    A provider failure remains a scored failed attempt; unknown state is never a
    passing assertion. Required metrics remain required even on provider failure.
    """
    task_id = _required(task, 'id')
    if _required(attempt, 'task_id') != task_id:
        raise IncompleteEvidence(f"task mismatch: {task_id}")
    repeat = _natural_number(_required(attempt, 'repeat'), 'repeat')
    if repeat < 1:
        raise IncompleteEvidence('repeat must start at 1')
    workspace = _required(attempt, 'workspace_id')
    if not isinstance(workspace, str) or not workspace.strip():
        raise IncompleteEvidence('workspace_id must be nonempty')
    observation = _required(attempt, 'observation')
    if not isinstance(observation, dict):
        raise IncompleteEvidence('observation must be an object')
    failed = []
    for predicate in _required(task, 'assertions'):
        name = _required(predicate, 'path')
        found, actual = _path(observation, name)
        operation = _required(predicate, 'op')
        expected = _required(predicate, 'expected')
        if operation == 'equals':
            valid = found and _equal(actual, expected)
        elif operation == 'set_equals':
            valid = found and _set_equal(actual, expected)
        else:
            raise IncompleteEvidence(f"unknown predicate operation: {operation}")
        if not valid:
            failed.append(name)
    state = _required(attempt, 'terminal_state')
    allowed = _required(_required(task, 'terminal_condition'), 'acceptable_states')
    terminal_ok = state in allowed
    rubric = _natural_number(_required(attempt, 'explanation_score'), 'explanation_score')
    if rubric > 4:
        raise IncompleteEvidence('explanation_score outside frozen rubric')
    intervention = _boolean(_required(attempt, 'intervention'), 'intervention')
    provider_status = _required(attempt, 'provider_status')
    if provider_status not in ('received', 'error'):
        raise IncompleteEvidence('unknown provider_status')
    invariants = _required(observation, 'invariants')
    errors = []
    for field, name in IMPACT_FIELDS.items():
        count = _natural_number(_required(invariants, field), f'invariants.{field}')
        errors.extend([name] * count)
    calls = _required(attempt, 'native_calls')
    proposed = _natural_number(_required(calls, 'proposed'), 'native_calls.proposed')
    valid = _natural_number(_required(calls, 'first_pass_valid'), 'native_calls.first_pass_valid')
    if valid > proposed:
        raise IncompleteEvidence('valid calls exceed proposed calls')
    refs = _required(attempt, 'references')
    emitted = _natural_number(_required(refs, 'emitted'), 'references.emitted')
    valid_refs = _natural_number(_required(refs, 'valid'), 'references.valid')
    missing_refs = _natural_number(_required(refs, 'mandatory_missing'), 'references.mandatory_missing')
    if valid_refs > emitted:
        raise IncompleteEvidence('valid references exceed emitted references')
    usage = _required(attempt, 'usage')
    if not isinstance(usage, dict) or not (usage.get('actual') is not None or usage.get('estimated') is not None):
        raise IncompleteEvidence('actual or explicitly estimated usage required')
    for kind in ('actual', 'estimated'):
        meter = usage.get(kind)
        if meter is None:
            continue
        _natural_number(_required(meter, 'input_tokens'), f'usage.{kind}.input_tokens')
        _natural_number(_required(meter, 'output_tokens'), f'usage.{kind}.output_tokens')
        if kind == 'estimated':
            method = _required(meter, 'method')
            if not isinstance(method, str) or not method.strip():
                raise IncompleteEvidence('usage.estimated.method must name the estimation method')
    writes = _required(attempt, 'writes')
    total_writes = _natural_number(_required(writes, 'total'), 'writes.total')
    traceable = _natural_number(_required(writes, 'traceable'), 'writes.traceable')
    if traceable > total_writes:
        raise IncompleteEvidence('traceable writes exceed total writes')
    return {
        'task_id': task_id, 'scenario': _required(task, 'scenario'), 'repeat': repeat,
        'workspace_id': workspace, 'success': provider_status == 'received' and not failed and terminal_ok and rubric >= 3 and not intervention and not errors,
        'failed_assertions': failed, 'terminal_ok': terminal_ok,
        'native_calls': {'proposed': proposed, 'first_pass_valid': valid},
        'references': {'emitted': emitted, 'valid': valid_refs, 'mandatory_missing': missing_refs},
        'high_impact_errors': errors, 'usage_recorded': True,
        'writes': {'total': total_writes, 'traceable': traceable},
    }


def _wilson(successes, total, z):
    if not total:
        raise IncompleteEvidence('empty confidence interval denominator')
    center = (successes / total + z*z / (2*total)) / (1 + z*z / total)
    margin = z * sqrt(successes*(total-successes)/total**3 + z*z/(4*total**2)) / (1 + z*z/total)
    return {'numerator': successes, 'denominator': total, 'lower': max(0.0, center-margin), 'upper': min(1.0, center+margin)}


def aggregate(task_definitions, attempts, contract, *, split):
    """Require every scheduled attempt exactly once; report gates without granting support.

    Development results can check accounting but can never be interpreted as F05
    qualification. Holdout operation requires the frozen 24 x 3 schedule.
    """
    if split not in ('development', 'holdout'):
        raise IncompleteEvidence('unknown split')
    execution = _required(contract, 'execution')
    gates = _required(contract, 'gates')
    repeats = _natural_number(_required(execution, 'repeats_per_configuration'), 'repeats')
    tasks = {}
    for task in task_definitions:
        task_id = _required(task, 'id')
        if task_id in tasks or _required(task, 'split') != split:
            raise IncompleteEvidence('duplicate task or mixed split')
        tasks[task_id] = task
    if not tasks or (split == 'holdout' and len(tasks) != execution['heldout_tasks']):
        raise IncompleteEvidence('incomplete task roster')
    if split == 'holdout' and Counter(t['scenario'] for t in tasks.values()) != Counter({f'S{i:02d}': 4 for i in range(1, 7)}):
        raise IncompleteEvidence('holdout scenario roster must have four tasks per S01-S06')
    expected = {(task_id, repeat) for task_id in tasks for repeat in range(1, repeats+1)}
    schedule = [(_required(a, 'task_id'), _required(a, 'repeat')) for a in attempts]
    if len(schedule) != len(expected) or set(schedule) != expected or len(set(schedule)) != len(schedule):
        raise IncompleteEvidence('missing, repeated or unknown scheduled attempt')
    if split == 'holdout' and len(expected) != execution['total_runs']:
        raise IncompleteEvidence('frozen run count mismatch')
    scored = [score_attempt(tasks[a['task_id']], a) for a in attempts]
    workspaces = [s['workspace_id'] for s in scored]
    if len(set(workspaces)) != len(workspaces):
        raise IncompleteEvidence('workspace reused across attempts')
    success = sum(s['success'] for s in scored)
    scenarios = Counter(s['scenario'] for s in scored)
    scenario_success = Counter(s['scenario'] for s in scored if s['success'])
    calls = sum(s['native_calls']['proposed'] for s in scored)
    valid_calls = sum(s['native_calls']['first_pass_valid'] for s in scored)
    refs = sum(s['references']['emitted'] + s['references']['mandatory_missing'] for s in scored)
    valid_refs = sum(s['references']['valid'] for s in scored)
    writes = sum(s['writes']['total'] for s in scored)
    traced = sum(s['writes']['traceable'] for s in scored)
    errors = sum(len(s['high_impact_errors']) for s in scored)
    # A zero denominator cannot demonstrate tool or citation validity.
    decisions = {
        'overall_success': len(scored) == gates['overall_total'] and success >= gates['overall_success_min'],
        'per_scenario_success': len(scenarios) == 6 and all(scenarios[k] == gates['per_scenario_total'] and scenario_success[k] >= gates['per_scenario_success_min'] for k in scenarios),
        'first_pass_valid_tool_call_rate': calls > 0 and valid_calls/calls >= gates['first_pass_valid_tool_call_rate_min'],
        'reference_validity': refs > 0 and valid_refs/refs >= gates['reference_validity_min'],
        'high_impact_errors': errors <= gates['high_impact_errors_max'],
        'all_runs_have_usage_record': all(s['usage_recorded'] for s in scored),
        'all_writes_traceable': traced == writes,
    }
    return {
        'split': split, 'attempts': len(scored), 'success': success,
        'per_scenario': {key: {'success': scenario_success[key], 'total': scenarios[key]} for key in sorted(scenarios)},
        'tool_calls': {'valid': valid_calls, 'proposed': calls},
        'references': {'valid': valid_refs, 'denominator': refs},
        'high_impact_errors': errors, 'writes': {'traceable': traced, 'total': writes},
        'wilson_95': _wilson(success, len(scored), contract['confidence_interval']['z']),
        'gates': decisions, 'all_gates_met': split == 'holdout' and all(decisions.values()),
        'qualification_status': 'not_attested',
    }

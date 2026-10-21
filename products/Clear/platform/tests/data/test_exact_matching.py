"""One-to-one match categories and full row coverage under explicit policies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modules.data_workbench.comparison import CompareError, ComparisonSpec, compare_exact
from modules.data_workbench.importing import import_csv
from modules.data_workbench.recipes import RecipeStore, Step


def _table(tmp_path: Path, name: str, content: str):
    path = tmp_path / f"{name}.csv"
    path.write_text(content, encoding="utf-8")
    table = import_csv(path, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    store = RecipeStore(tmp_path / f"{name}.sqlite3")
    revision = store.import_table(table)
    return store, revision, store.get(revision)["snapshot"]


def _fixture(tmp_path: Path):
    _, left_id, left = _table(tmp_path, "left", "id,amount,note\nA,10.00,ok\nB,20,before\n"
                               "D,1,x\nD,2,y\nE,7,z\n,4,missing\nF,0,\"\"\n")
    _, right_id, right = _table(tmp_path, "right", "id,amount,note\nA,10.01,ok\nB,20,after\n"
                                  "D,1,x\nD,2,y\nD,3,z\nC,8,new\n,4,missing\nF,0,\"\"\n")
    return left_id, left, right_id, right


def _spec(**changes):
    args = {"key_pairs": (("c1", "c1"),),
            "compare_pairs": (("c2", "c2"), ("c3", "c3")),
            "amount_pairs": (("c2", "c2"),),
            "amount_tolerance": "0.01"}
    args.update(changes)
    return ComparisonSpec(**args)


def test_match_differences_unmatched_ambiguity_and_coverage(tmp_path: Path):
    left_id, left, right_id, right = _fixture(tmp_path)
    result = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                           spec=_spec())
    assert result.policy["null_policy"] == "never_match"
    assert result.policy["amount_tolerance"] == "0.01"
    assert len(result.same) == 2  # A is within tolerance; F is exactly zero/empty text.
    assert len(result.different) == 1
    assert result.different[0].different_columns == ("c3:c3",)
    assert len(result.only_left) == len(result.only_right) == 2
    assert {item.reason for item in result.only_left} == {"no_partner", "null_or_unknown_key"}
    assert len(result.ambiguous) == 1
    assert len(result.ambiguous[0].left) == 2
    assert len(result.ambiguous[0].right) == 3
    assert result.coverage == {
        "left_total": 7, "right_total": 8, "left_categorized": 7, "right_categorized": 8,
        "same_pairs": 2, "different_pairs": 1, "only_left": 2, "only_right": 2,
        "ambiguous_keys": 1, "ambiguous_left_rows": 2, "ambiguous_right_rows": 3,
    }
    encoded = json.loads(result.to_json())
    assert encoded["ambiguous"][0]["left"][0]["source_row"] == 4
    assert encoded["same"][0]["left"]["row_id"]
    assert result.comparison_id == compare_exact(left, right, left_revision=left_id,
                                                  right_revision=right_id, spec=_spec()).comparison_id


@pytest.mark.parametrize(('left_amount', 'right_amount', 'tolerance', 'equal'), [
    ('1,234.50', '1234.51', '0.01', True),
    ('1,234.50', '1,234.50', '0', True),
    ('(20.00)', '-20.00', '0', True),
    ('(1,234.50)', '-1234.50', '0', True),
    ('(123456789012345678901234567890.12)', '-123456789012345678901234567890.12', '0', True),
    ('1,234.50', '1234.52', '0.01', False),
    ('12,34.50', '1234.50', '0', False),
    ('1234,50', '1234.50', '0', False),
    ('(+20.00)', '-20.00', '0', False),
    ('(-20.00)', '20.00', '0', False),
])
def test_formatted_amount_comparison_keeps_original_cells(tmp_path, left_amount, right_amount, tolerance, equal):
    left_id, left = _table(tmp_path, 'left', f'id,amount\n001234567890,"{left_amount}"\n')[1:]
    right_id, right = _table(tmp_path, 'right', f'id,amount\n001234567890,"{right_amount}"\n')[1:]
    original = json.dumps([left, right], ensure_ascii=False, sort_keys=True)
    spec = ComparisonSpec((('c1', 'c1'),), (('c2', 'c2'),),
                          amount_pairs=(('c2', 'c2'),), amount_tolerance=tolerance)
    result = compare_exact(left, right, left_revision=left_id, right_revision=right_id, spec=spec)
    assert len(result.same) == int(equal)
    assert len(result.different) == int(not equal)
    assert result.policy['amount_format'] == 'financial-v1'
    assert json.dumps([left, right], ensure_ascii=False, sort_keys=True) == original
    if left_amount != right_amount:
        text_result = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                                   spec=ComparisonSpec((('c1', 'c1'),), (('c2', 'c2'),)))
        assert len(text_result.different) == 1


def test_new_amount_rules_have_a_distinct_identity_and_preserve_legacy_policy(tmp_path):
    left_id, left = _table(tmp_path, 'left', 'id,amount\nA,"1,234.50"\n')[1:]
    right_id, right = _table(tmp_path, 'right', 'id,amount\nA,1234.51\n')[1:]
    args = dict(key_pairs=(('c1', 'c1'),), compare_pairs=(('c2', 'c2'),),
                amount_pairs=(('c2', 'c2'),), amount_tolerance='0.01')
    current = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                            spec=ComparisonSpec(**args))
    legacy = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                           spec=ComparisonSpec(**args, amount_format='plain-decimal-v1'))
    assert len(current.same) == len(legacy.different) == 1
    assert current.comparison_id != legacy.comparison_id
    assert 'amount_format' not in legacy.policy


def test_null_and_amount_policies_change_results_explicitly(tmp_path: Path):
    left_id, left, right_id, right = _fixture(tmp_path)
    exact = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                          spec=_spec(amount_tolerance="0"))
    assert len(exact.same) == 1
    assert len(exact.different) == 2
    null_equal = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                               spec=_spec(null_policy="equal"))
    assert len(null_equal.same) == 3
    assert len(null_equal.only_left) == len(null_equal.only_right) == 1
    assert null_equal.comparison_id != exact.comparison_id


def test_type_policy_is_explicit_and_row_provenance_survives_recipe(tmp_path: Path):
    _, left_id, left = _table(tmp_path, "left", "id,value\n1,x\n")
    right_store, right_root, _ = _table(tmp_path, "right", "id,value\n1,x\n")
    right_id = right_store.apply(right_root, Step("cast", {"column": "c1", "target": "integer"}))
    right = right_store.get(right_id)["snapshot"]
    strict = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                           spec=ComparisonSpec((('c1', 'c1'),), (('c2', 'c2'),)))
    assert len(strict.same) == 0
    assert len(strict.only_left) == len(strict.only_right) == 1
    text = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                         spec=ComparisonSpec((('c1', 'c1'),), (('c2', 'c2'),), type_policy="text"))
    assert len(text.same) == 1
    assert text.same[0].right.revision_id == right_id
    assert text.same[0].right.source_row == 2


def test_unmatchable_formula_and_invalid_policy_fail_closed(tmp_path: Path):
    left_id, left, right_id, right = _fixture(tmp_path)
    left["rows"][0]["cells"][0] = {"kind": "unknown_formula", "raw": "=A1", "value": None}
    result = compare_exact(left, right, left_revision=left_id, right_revision=right_id,
                           spec=_spec())
    assert any(item.reason == "null_or_unknown_key" for item in result.only_left)
    for spec in (_spec(amount_tolerance="-1"),
                 _spec(amount_pairs=(("c1", "c1"),)),
                 _spec(key_pairs=(("c9", "c1"),)),
                 _spec(key_pairs=(("c1", "c1"), ("c1", "c1")))):
        with pytest.raises(CompareError):
            compare_exact(left, right, left_revision=left_id, right_revision=right_id, spec=spec)


def test_duplicate_input_row_id_is_rejected(tmp_path: Path):
    left_id, left, right_id, right = _fixture(tmp_path)
    left["rows"][1]["row_id"] = left["rows"][0]["row_id"]
    with pytest.raises(CompareError, match="row"):
        compare_exact(left, right, left_revision=left_id, right_revision=right_id, spec=_spec())

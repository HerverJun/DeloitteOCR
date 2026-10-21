"""Exact one-to-one matching with explicit type, null and amount policies.

Rows with duplicated keys are classified as ambiguous before pairing. Every
input row appears exactly once among matched, unmatched or ambiguous results.
The engine keeps original row IDs and source row numbers for both sides.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation


_REVISION = re.compile(r"v[0-9a-f]{64}\Z")
_COLUMN = re.compile(r"c[1-9][0-9]{0,2}\Z")
_ROW = re.compile(r"r[0-9a-f]{32}\Z")
_KINDS = frozenset({"null", "text", "decimal", "integer", "boolean", "date", "error", "unknown_formula"})
_GROUPED_AMOUNT = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?\Z")
_ACCOUNTING_AMOUNT = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")


class CompareError(ValueError):
    """Comparison inputs or coverage are invalid."""


@dataclass(frozen=True)
class ComparisonSpec:
    key_pairs: tuple[tuple[str, str], ...]
    compare_pairs: tuple[tuple[str, str], ...]
    amount_pairs: tuple[tuple[str, str], ...] = ()
    type_policy: str = "strict"  # strict or text
    null_policy: str = "never_match"  # never_match or equal
    amount_tolerance: str = "0"
    trim_text: bool = False
    casefold_text: bool = False
    amount_format: str = "financial-v1"


@dataclass(frozen=True)
class RowRef:
    revision_id: str
    row_id: str
    source_row: int


@dataclass(frozen=True)
class Pair:
    key: tuple
    left: RowRef
    right: RowRef
    different_columns: tuple[str, ...]


@dataclass(frozen=True)
class Unmatched:
    row: RowRef
    reason: str


@dataclass(frozen=True)
class Ambiguous:
    key: tuple
    left: tuple[RowRef, ...]
    right: tuple[RowRef, ...]


@dataclass(frozen=True)
class ComparisonResult:
    comparison_id: str
    left_revision: str
    right_revision: str
    policy: dict
    same: tuple[Pair, ...]
    different: tuple[Pair, ...]
    only_left: tuple[Unmatched, ...]
    only_right: tuple[Unmatched, ...]
    ambiguous: tuple[Ambiguous, ...]
    coverage: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _snapshot(value: object, revision: str) -> tuple[dict[str, int], list[dict]]:
    if (not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            or not isinstance(value, dict)
            or not isinstance(value.get("columns"), list)
            or not isinstance(value.get("rows"), list)):
        raise CompareError("Invalid versioned table")
    columns = value["columns"]
    names = [item.get("column_id") if isinstance(item, dict) else None for item in columns]
    if (not names or len(names) > 256
            or any(not isinstance(name, str) or not _COLUMN.fullmatch(name) for name in names)
            or len(set(names)) != len(names)):
        raise CompareError("Invalid table columns")
    if len(value["rows"]) > 100_000:
        raise CompareError("Table exceeds comparison limit")
    seen = set()
    for row in value["rows"]:
        if (not isinstance(row, dict) or not isinstance(row.get("row_id"), str)
                or not _ROW.fullmatch(row["row_id"]) or row["row_id"] in seen
                or type(row.get("source_row")) is not int or row["source_row"] < 1
                or not isinstance(row.get("cells"), (list, tuple))
                or len(row["cells"]) != len(names)
                or any(not isinstance(cell, dict) or not isinstance(cell.get("kind"), str)
                       or cell.get("kind") not in _KINDS
                       or cell.get("value") is not None and not isinstance(cell["value"], (str, bool))
                       or cell.get("raw") is not None and not isinstance(cell["raw"], str)
                       or cell.get("kind") in {"null", "unknown_formula"} and cell.get("value") is not None
                       for cell in row["cells"])):
            raise CompareError("Invalid table row")
        seen.add(row["row_id"])
    return {name: index for index, name in enumerate(names)}, value["rows"]


def _pairs(value: object, left: dict, right: dict, *, empty: bool) -> tuple[tuple[str, str], ...]:
    if (not isinstance(value, tuple) or (not empty and not value)
            or len(value) > min(len(left), len(right))):
        raise CompareError("Invalid comparison columns")
    checked = []
    for pair in value:
        if (not isinstance(pair, tuple) or len(pair) != 2
                or any(not isinstance(item, str) for item in pair)
                or pair[0] not in left or pair[1] not in right):
            raise CompareError("Comparison column is unavailable")
        checked.append(pair)
    if (len(set(checked)) != len(checked)
            or len({pair[0] for pair in checked}) != len(checked)
            or len({pair[1] for pair in checked}) != len(checked)):
        raise CompareError("Duplicate comparison columns")
    return tuple(checked)


def _policy(spec: ComparisonSpec, left: dict, right: dict) -> tuple[dict, Decimal]:
    if not isinstance(spec, ComparisonSpec):
        raise CompareError("Comparison policy is required")
    keys = _pairs(spec.key_pairs, left, right, empty=False)
    values = _pairs(spec.compare_pairs, left, right, empty=True)
    amounts = _pairs(spec.amount_pairs, left, right, empty=True)
    if (not set(amounts) <= set(values)
            or not isinstance(spec.type_policy, str) or spec.type_policy not in {"strict", "text"}
            or not isinstance(spec.null_policy, str) or spec.null_policy not in {"never_match", "equal"}
            or not isinstance(spec.amount_format, str) or spec.amount_format not in {"financial-v1", "plain-decimal-v1"}
            or type(spec.trim_text) is not bool or type(spec.casefold_text) is not bool
            or not isinstance(spec.amount_tolerance, str) or len(spec.amount_tolerance) > 64):
        raise CompareError("Invalid comparison policy")
    try:
        tolerance = Decimal(spec.amount_tolerance)
        if not tolerance.is_finite() or tolerance < 0:
            raise InvalidOperation()
    except InvalidOperation:
        raise CompareError("Invalid amount tolerance") from None
    policy = {"key_pairs": keys, "compare_pairs": values, "amount_pairs": amounts,
            "type_policy": spec.type_policy, "null_policy": spec.null_policy,
            "amount_tolerance": str(tolerance), "trim_text": spec.trim_text,
            "casefold_text": spec.casefold_text}
    if amounts and spec.amount_format == "financial-v1":
        policy["amount_format"] = spec.amount_format
    return policy, tolerance


def _text(value: object, policy: dict) -> str:
    result = unicodedata.normalize("NFC", str(value))
    if policy["trim_text"]:
        result = result.strip()
    if policy["casefold_text"]:
        result = result.casefold()
    return result


def _token(cell: dict, policy: dict):
    if cell["kind"] == "unknown_formula":
        return None
    if cell["kind"] == "null":
        return ["null", None] if policy["null_policy"] == "equal" else None
    if policy["type_policy"] == "text":
        return ["text", _text(cell["value"], policy)]
    return [cell["kind"], cell["value"]]


def _amount(cell: dict, formatted: bool = False) -> Decimal | None:
    if (cell["kind"] not in {"text", "decimal", "integer"}
            or isinstance(cell["value"], bool) or len(str(cell["value"])) > 256):
        return None
    try:
        text = str(cell["value"]).strip()
        accounting = formatted and text.startswith("(") and text.endswith(")")
        if accounting:
            text = text[1:-1].strip()
            if text.startswith(("+", "-")):
                return None
        if formatted and "," in text:
            if not _GROUPED_AMOUNT.fullmatch(text):
                return None
            text = text.replace(",", "")
        elif accounting and not _ACCOUNTING_AMOUNT.fullmatch(text):
            return None
        number = Decimal(text)
        if accounting:
            number = number.copy_negate()
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError):
        return None


def _equal(left_cell: dict, right_cell: dict, policy: dict, amount: bool, tolerance: Decimal) -> bool:
    if left_cell["kind"] == right_cell["kind"] == "null":
        return policy["null_policy"] == "equal"
    if left_cell["kind"] in {"null", "unknown_formula"} or right_cell["kind"] in {
            "null", "unknown_formula"}:
        return False
    if amount:
        formatted = policy.get("amount_format") == "financial-v1"
        left_number, right_number = _amount(left_cell, formatted), _amount(right_cell, formatted)
        return left_number is not None and right_number is not None and abs(left_number - right_number) <= tolerance
    return _token(left_cell, policy) == _token(right_cell, policy)


def _ref(revision: str, row: dict) -> RowRef:
    return RowRef(revision, row["row_id"], row["source_row"])


def compare_exact(left_snapshot: dict, right_snapshot: dict, *,
                  left_revision: str, right_revision: str,
                  spec: ComparisonSpec) -> ComparisonResult:
    left_columns, left_rows = _snapshot(left_snapshot, left_revision)
    right_columns, right_rows = _snapshot(right_snapshot, right_revision)
    policy, tolerance = _policy(spec, left_columns, right_columns)
    keys = policy["key_pairs"]
    amount_pairs = set(policy["amount_pairs"])
    left_index: dict[str, list[dict]] = {}
    right_index: dict[str, list[dict]] = {}
    only_left, only_right, ambiguous, same, different = [], [], [], [], []

    def index(rows, columns, side, unmatched):
        for row in rows:
            tokens = [_token(row["cells"][columns[pair[side]]], policy) for pair in keys]
            if any(token is None for token in tokens):
                unmatched.append(Unmatched(_ref(left_revision if side == 0 else right_revision, row),
                                           "null_or_unknown_key"))
                continue
            encoded = _canonical(tokens)
            (left_index if side == 0 else right_index).setdefault(encoded, []).append(row)

    index(left_rows, left_columns, 0, only_left)
    index(right_rows, right_columns, 1, only_right)
    for encoded in sorted(set(left_index) | set(right_index)):
        left_group, right_group = left_index.get(encoded, []), right_index.get(encoded, [])
        key = tuple(tuple(item) for item in json.loads(encoded))
        if len(left_group) > 1 or len(right_group) > 1:
            ambiguous.append(Ambiguous(key,
                                       tuple(_ref(left_revision, row) for row in left_group),
                                       tuple(_ref(right_revision, row) for row in right_group)))
        elif not right_group:
            only_left.append(Unmatched(_ref(left_revision, left_group[0]), "no_partner"))
        elif not left_group:
            only_right.append(Unmatched(_ref(right_revision, right_group[0]), "no_partner"))
        else:
            left_row, right_row = left_group[0], right_group[0]
            mismatches = tuple(f"{a}:{b}" for a, b in policy["compare_pairs"]
                               if not _equal(left_row["cells"][left_columns[a]],
                                             right_row["cells"][right_columns[b]],
                                             policy, (a, b) in amount_pairs, tolerance))
            pair = Pair(key, _ref(left_revision, left_row), _ref(right_revision, right_row), mismatches)
            (different if mismatches else same).append(pair)

    left_seen = ([pair.left.row_id for pair in (*same, *different)]
                 + [item.row.row_id for item in only_left]
                 + [ref.row_id for group in ambiguous for ref in group.left])
    right_seen = ([pair.right.row_id for pair in (*same, *different)]
                  + [item.row.row_id for item in only_right]
                  + [ref.row_id for group in ambiguous for ref in group.right])
    if (len(left_seen) != len(left_rows) or len(right_seen) != len(right_rows)
            or len(set(left_seen)) != len(left_rows) or len(set(right_seen)) != len(right_rows)):
        raise CompareError("Comparison does not cover all rows exactly once")
    coverage = {"left_total": len(left_rows), "right_total": len(right_rows),
                "left_categorized": len(left_seen), "right_categorized": len(right_seen),
                "same_pairs": len(same), "different_pairs": len(different),
                "only_left": len(only_left), "only_right": len(only_right),
                "ambiguous_keys": len(ambiguous),
                "ambiguous_left_rows": sum(len(group.left) for group in ambiguous),
                "ambiguous_right_rows": sum(len(group.right) for group in ambiguous)}
    identity = {"left_revision": left_revision, "right_revision": right_revision,
                "left_snapshot": hashlib.sha256(_canonical(left_snapshot).encode()).hexdigest(),
                "right_snapshot": hashlib.sha256(_canonical(right_snapshot).encode()).hexdigest(),
                "policy": policy}
    comparison_id = "cmp" + hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:32]
    return ComparisonResult(comparison_id, left_revision, right_revision, policy,
                            tuple(same), tuple(different), tuple(only_left), tuple(only_right),
                            tuple(ambiguous), coverage)

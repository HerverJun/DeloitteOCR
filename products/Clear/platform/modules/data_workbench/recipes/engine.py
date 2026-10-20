"""Persist fixed cleaning operators as an immutable revision graph.

Every step records its parent revision, normalized parameters, row counts,
errors and a bounded preview. Applying a new step to an older revision creates
a branch; no revision is overwritten. The engine never executes supplied SQL
or Python expressions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Mapping

from modules.data_workbench.importing.tables import ImportedTable


_REVISION = re.compile(r"v[0-9a-f]{64}\Z")
_COLUMN = re.compile(r"c[1-9][0-9]{0,2}\Z")
_OPERATORS = frozenset({"trim", "trim_columns", "rename", "map", "cast", "filter", "duplicates", "filter_compare", "group_summary"})
_MAX_SNAPSHOT = 128 * 1024 * 1024
_MAX_ERRORS = 20


class RecipeError(ValueError):
    """An invalid or unavailable recipe operation."""


@dataclass(frozen=True)
class Step:
    operator: str
    parameters: Mapping[str, object]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _revision(value: object) -> str:
    return "v" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _seal(event: dict, snapshot: str) -> str:
    event["snapshot_sha256"] = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    return _canonical(event)


def _valid_text(value: object, *, max_length: int = 256) -> bool:
    return isinstance(value, str) and len(value) <= max_length


def _parameters(step: Step, columns: list[dict]) -> dict:
    if not isinstance(step, Step) or step.operator not in _OPERATORS or not isinstance(step.parameters, Mapping):
        raise RecipeError("Unknown recipe operation")
    parameters = dict(step.parameters)
    known = {column["column_id"] for column in columns}

    def column(value):
        if not isinstance(value, str) or not _COLUMN.fullmatch(value) or value not in known:
            raise RecipeError("Column is unavailable")
        return value

    if step.operator == 'trim_columns':
        selected = parameters.get('columns')
        if (set(parameters) != {'columns'} or not isinstance(selected, list) or not selected
                or any(not isinstance(value, str) for value in selected) or len(set(selected)) != len(selected)):
            raise RecipeError('Invalid multi-column trim parameters')
        return {'columns': [column(value) for value in selected]}
    if step.operator == "trim":
        if set(parameters) != {"column"}:
            raise RecipeError("Invalid trim parameters")
        return {"column": column(parameters["column"])}
    if step.operator == "rename":
        if (set(parameters) != {"column", "header"} or
                not _valid_text(parameters["header"]) or not parameters["header"].strip()):
            raise RecipeError("Invalid rename parameters")
        return {"column": column(parameters["column"]), "header": parameters["header"]}
    if step.operator == "map":
        values = parameters.get("values")
        if (set(parameters) != {"column", "values"} or not isinstance(values, dict)
                or not values or len(values) > 1000
                or any(not _valid_text(key, max_length=4096) or not _valid_text(value, max_length=4096)
                       for key, value in values.items())):
            raise RecipeError("Invalid mapping parameters")
        return {"column": column(parameters["column"]), "values": dict(sorted(values.items()))}
    if step.operator == "cast":
        if (set(parameters) != {"column", "target"}
                or not isinstance(parameters["target"], str)
                or parameters["target"] not in {"decimal", "integer", "boolean", "date"}):
            raise RecipeError("Invalid cast parameters")
        return {"column": column(parameters["column"]), "target": parameters["target"]}
    if step.operator == "filter":
        predicate = parameters.get("predicate")
        expected = {"column", "predicate"} if isinstance(predicate, str) and predicate in {"is_null", "not_null"} else {
            "column", "predicate", "value"}
        if (set(parameters) != expected or not isinstance(predicate, str) or predicate not in {
            "equals", "not_equals", "is_null", "not_null"} or
                "value" in parameters and not isinstance(parameters["value"], (str, bool))):
            raise RecipeError("Invalid filter parameters")
        result = {"column": column(parameters["column"]), "predicate": predicate}
        if "value" in parameters:
            result["value"] = parameters["value"]
        return result
    if step.operator == 'filter_compare':
        predicate = parameters.get('predicate')
        expected = {'column', 'predicate', 'value_kind', 'value'} | ({'upper'} if predicate == 'between' else set())
        if set(parameters) != expected or predicate not in {'gt', 'ge', 'lt', 'le', 'between'} or parameters.get('value_kind') not in {'decimal', 'date', 'text'}:
            raise RecipeError('Invalid typed comparison')
        for key in ('value', 'upper'):
            if key in parameters:
                _comparable(parameters['value_kind'], parameters[key])
        if predicate == 'between' and _comparable(parameters['value_kind'], parameters['value']) > _comparable(parameters['value_kind'], parameters['upper']):
            raise RecipeError('Reversed comparison interval')
        return {**parameters, 'column': column(parameters['column'])}
    if step.operator == 'group_summary':
        if set(parameters) != {'columns', 'amount_columns'} or any(not isinstance(parameters[key], list) or len(set(parameters[key])) != len(parameters[key]) for key in ('columns', 'amount_columns')):
            raise RecipeError('Invalid grouping parameters')
        if not parameters['columns'] or len(parameters['columns']) + len(parameters['amount_columns']) + 1 > 256:
            raise RecipeError('Grouping columns exceed limit')
        return {key: [column(value) for value in parameters[key]] for key in ('columns', 'amount_columns')}
    selected = parameters.get("columns")
    if (set(parameters) != {"columns", "include_null"} or not isinstance(selected, list)
            or not selected or len(selected) > len(columns)
            or any(not isinstance(value, str) for value in selected)
            or len(set(selected)) != len(selected)
            or type(parameters["include_null"]) is not bool):
        raise RecipeError("Invalid duplicate parameters")
    return {"columns": [column(value) for value in selected],
            "include_null": parameters["include_null"]}


def _comparable(kind, value):
    if not isinstance(value, str) or len(value) > 4096:
        raise RecipeError('Typed value must be a bounded literal')
    if kind == 'decimal':
        try:
            number = Decimal(value)
            if not number.is_finite() or abs(number.adjusted()) > 1024:
                raise InvalidOperation()
            return number
        except InvalidOperation:
            raise RecipeError('Invalid finite decimal') from None
    if kind == 'date':
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise RecipeError('Invalid date') from None
    return value


def _cast(cell: dict, target: str) -> tuple[dict, str | None]:
    if cell["kind"] == "null":
        return cell, None
    if cell["kind"] == "unknown_formula":
        return cell, "formula_cache_unknown"
    value = cell["value"]
    try:
        if target == "decimal":
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                raise InvalidOperation()
            output = ("decimal", str(numeric))
        elif target == "integer":
            numeric = Decimal(str(value))
            if not numeric.is_finite() or numeric != numeric.to_integral_value():
                raise InvalidOperation()
            output = ("integer", str(int(numeric)))
        elif target == "boolean":
            if isinstance(value, bool):
                output = ("boolean", value)
            elif isinstance(value, str) and value.strip().casefold() in {"true", "false", "1", "0"}:
                output = ("boolean", value.strip().casefold() in {"true", "1"})
            else:
                raise ValueError()
        else:
            if not isinstance(value, str):
                raise ValueError()
            output = ("date", date.fromisoformat(value).isoformat())
    except (InvalidOperation, ValueError, OverflowError):
        return cell, "invalid_" + target
    changed = dict(cell)
    changed["kind"], changed["value"] = output
    return changed, None


def _operate(snapshot: dict, operator: str, parameters: dict) -> tuple[dict, dict]:
    result = copy.deepcopy(snapshot)
    columns, rows = result["columns"], result["rows"]
    positions = {item["column_id"]: index for index, item in enumerate(columns)}
    errors, excluded, duplicate_groups = [], [], []
    input_rows = len(rows)
    if operator == 'trim_columns':
        selected = [positions[value] for value in parameters['columns']]
        for row in rows:
            for position in selected:
                cell = row['cells'][position]
                if cell['kind'] == 'text':
                    cell['value'] = cell['value'].strip()
    elif operator == "rename":
        columns[positions[parameters["column"]]]["header"] = parameters["header"]
    elif operator in {"trim", "map", "cast"}:
        position = positions[parameters["column"]]
        for row in rows:
            cell = row["cells"][position]
            if operator == "trim" and cell["kind"] == "text":
                cell["value"] = cell["value"].strip()
            elif operator == "map" and cell["kind"] == "text":
                cell["value"] = parameters["values"].get(cell["value"], cell["value"])
            elif operator == "cast":
                converted, code = _cast(cell, parameters["target"])
                if code:
                    errors.append({"row_id": row["row_id"], "column_id": parameters["column"],
                                   "code": code, "raw": cell["raw"]})
                else:
                    row["cells"][position] = converted
    elif operator == 'filter_compare':
        position = positions[parameters['column']]
        kind = parameters['value_kind']
        low = _comparable(kind, parameters['value'])
        high = _comparable(kind, parameters['upper']) if 'upper' in parameters else None
        kept = []
        for row in rows:
            cell = row['cells'][position]
            compatible = cell['kind'] in {'integer', 'decimal'} if kind == 'decimal' else cell['kind'] == kind
            include = False
            try:
                if not compatible:
                    raise RecipeError('Cell type does not match comparison')
                value = _comparable(kind, cell['value'])
                predicate = parameters['predicate']
                include = {'gt': value > low, 'ge': value >= low, 'lt': value < low, 'le': value <= low}.get(predicate, False) if predicate != 'between' else low <= value <= high
            except RecipeError:
                errors.append({'row_id': row['row_id'], 'column_id': parameters['column'], 'code': 'comparison_type_unknown', 'raw': cell['raw']})
            if include:
                kept.append(row)
            else:
                excluded.append(row['row_id'])
        result['rows'] = kept
    elif operator == 'group_summary':
        group_positions = [positions[key] for key in parameters['columns']]
        groups = {}
        for row in rows:
            key = _canonical([(row['cells'][position]['kind'], row['cells'][position]['value']) for position in group_positions])
            groups.setdefault(key, []).append(row)
        next_column = max(int(column['column_id'][1:]) for column in columns) + 1
        if next_column + len(parameters['amount_columns']) > 999:
            raise RecipeError('No column IDs available for summary')
        result['columns'] = [copy.deepcopy(columns[position]) for position in group_positions] + [{'column_id': 'c' + str(next_column), 'header': '记录数量'}]
        result['columns'] += [{'column_id': 'c' + str(next_column + i + 1), 'header': str(columns[positions[key]]['header'] or key) + ' · 合计'} for i, key in enumerate(parameters['amount_columns'])]
        output = []
        for key, members in groups.items():
            cells = [copy.deepcopy(members[0]['cells'][position]) for position in group_positions]
            cells.append({'kind': 'integer', 'raw': None, 'value': str(len(members))})
            for column in parameters['amount_columns']:
                values, invalid = [], False
                for row in members:
                    cell = row['cells'][positions[column]]
                    try:
                        if cell['kind'] not in {'integer', 'decimal'}:
                            raise RecipeError('Amount is not typed numeric')
                        values.append(_comparable('decimal', cell['value']))
                    except RecipeError:
                        invalid = True
                        errors.append({'row_id': row['row_id'], 'column_id': column, 'code': 'amount_type_unknown', 'raw': cell['raw']})
                if invalid:
                    cells.append({'kind': 'error', 'raw': None, 'value': None, 'warning': '金额类型缺失；未生成不完整的合计'})
                else:
                    with localcontext() as context:
                        context.prec = 4096
                        total = sum(values, Decimal(0))
                    cells.append({'kind': 'decimal', 'raw': None, 'value': str(total)})
            sources = [source for member in members for source in member.get('source_rows', [{'row_id': member['row_id'], 'source_row': member['source_row']}])]
            output.append({'row_id': 'r' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:32], 'source_row': members[0]['source_row'], 'source_rows': sources, 'cells': cells})
        result['rows'] = output
    elif operator == "filter":
        position = positions[parameters["column"]]
        predicate = parameters["predicate"]
        kept = []
        for row in rows:
            cell = row["cells"][position]
            if predicate == "is_null":
                include = cell["kind"] == "null"
            elif predicate == "not_null":
                include = cell["kind"] != "null"
            else:
                same = cell["value"] == parameters["value"]
                include = cell["kind"] != "null" and (same if predicate == "equals" else not same)
            if include:
                kept.append(row)
            else:
                excluded.append(row["row_id"])
        result["rows"] = kept
    else:
        positions_selected = [positions[key] for key in parameters["columns"]]
        groups = {}
        for row in rows:
            cells = [row["cells"][index] for index in positions_selected]
            if not parameters["include_null"] and any(cell["kind"] == "null" for cell in cells):
                continue
            key = _canonical([(cell["kind"], cell["value"]) for cell in cells])
            groups.setdefault(key, []).append(row["row_id"])
        duplicate_groups = [ids for ids in groups.values() if len(ids) > 1]
    event = {"operator": operator, "parameters": parameters, "input_rows": input_rows,
             "output_rows": len(result["rows"]), "errors_total": len(errors),
             "error_sample": errors[:_MAX_ERRORS], "excluded_row_ids": excluded,
             "duplicate_groups": duplicate_groups}
    return result, event


class RecipeStore:
    """SQLite-backed, immutable revisions from one or more imported tables."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).resolve()
        if not self.path.parent.is_dir():
            raise RecipeError("Recipe directory is unavailable")
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recipe_revisions (
                    revision_id TEXT PRIMARY KEY, source_revision TEXT NOT NULL,
                    parent_revision TEXT, step_json TEXT, snapshot_json TEXT NOT NULL,
                    event_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS recipe_revisions_source
                    ON recipe_revisions(source_revision, revision_id);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _insert(db: sqlite3.Connection, *, revision_id: str, source_revision: str,
                parent_revision: str | None, step: str | None, snapshot: str, event: str) -> None:
        if len(snapshot.encode("utf-8")) > _MAX_SNAPSHOT:
            raise RecipeError("Recipe snapshot exceeds limit")
        db.execute("INSERT OR IGNORE INTO recipe_revisions VALUES (?,?,?,?,?,?)",
                   (revision_id, source_revision, parent_revision, step, snapshot, event))
        current = db.execute("SELECT * FROM recipe_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if tuple(current[key] for key in ("source_revision", "parent_revision", "step_json",
                                          "snapshot_json", "event_json")) != (
                source_revision, parent_revision, step, snapshot, event):
            raise RecipeError("Recipe revision identity conflicts")

    def import_table(self, table: ImportedTable) -> str:
        if not isinstance(table, ImportedTable):
            raise RecipeError("Imported table is required")
        snapshot = {"columns": [asdict(column) for column in table.columns],
                    "rows": [asdict(row) for row in table.rows]}
        revision_id = _revision({"source_revision": table.revision, "root": True})
        event = {"operator": "source", "parameters": {}, "input_rows": len(table.rows),
                 "output_rows": len(table.rows), "errors_total": 0, "error_sample": [],
                 "excluded_row_ids": [], "duplicate_groups": [],
                 "input_revision": None, "output_revision": revision_id}
        snapshot_json = _canonical(snapshot)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._insert(db, revision_id=revision_id, source_revision=table.revision,
                             parent_revision=None, step=None, snapshot=snapshot_json,
                             event=_seal(event, snapshot_json))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return revision_id

    def get(self, revision_id: str) -> dict:
        if not isinstance(revision_id, str) or not _REVISION.fullmatch(revision_id):
            raise RecipeError("Invalid recipe revision")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM recipe_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        if row is None:
            raise RecipeError("Recipe revision is unavailable")
        try:
            event = json.loads(row["event_json"])
            step = json.loads(row["step_json"]) if row["step_json"] else None
            snapshot = json.loads(row["snapshot_json"])
        except (TypeError, ValueError):
            raise RecipeError("Recipe revision is corrupt") from None
        expected = (_revision({"source_revision": row["source_revision"], "root": True})
                    if row["parent_revision"] is None else
                    _revision({"parent": row["parent_revision"], "step": step}))
        if (not isinstance(event, dict) or not isinstance(snapshot, dict)
                or not isinstance(snapshot.get("rows"), list)
                or expected != row["revision_id"] or
                event.get("input_revision") != row["parent_revision"] or
                event.get("output_revision") != row["revision_id"] or
                event.get("output_rows") != len(snapshot.get("rows", [])) or
                event.get("snapshot_sha256") != hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest()):
            raise RecipeError("Recipe snapshot is corrupt")
        return {"revision_id": row["revision_id"], "source_revision": row["source_revision"],
                "parent_revision": row["parent_revision"],
                "step": step, "snapshot": snapshot, "event": event}

    def apply(self, parent_revision: str, step: Step) -> str:
        parent = self.get(parent_revision)
        parameters = _parameters(step, parent["snapshot"]["columns"])
        normalized = {"operator": step.operator, "parameters": parameters}
        revision_id = _revision({"parent": parent_revision, "step": normalized})
        snapshot, event = _operate(parent["snapshot"], step.operator, parameters)
        event["input_revision"], event["output_revision"] = parent_revision, revision_id
        snapshot_json = _canonical(snapshot)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT 1 FROM recipe_revisions WHERE revision_id=?",
                              (parent_revision,)).fetchone() is None:
                    raise RecipeError("Parent revision changed")
                self._insert(db, revision_id=revision_id, source_revision=parent["source_revision"],
                             parent_revision=parent_revision, step=_canonical(normalized),
                             snapshot=snapshot_json, event=_seal(event, snapshot_json))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return revision_id

    def preview(self, revision_id: str, *, offset: int = 0, limit: int = 20) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise RecipeError("Invalid preview window")
        revision = self.get(revision_id)
        snapshot = revision.pop("snapshot")
        return {**revision, "columns": snapshot["columns"], "total_rows": len(snapshot["rows"]),
                "rows": snapshot["rows"][offset:offset + limit]}

    def lineage(self, revision_id: str) -> tuple[str, ...]:
        chain = []
        current = revision_id
        while current is not None:
            revision = self.get(current)
            chain.append(current)
            current = revision["parent_revision"]
        return tuple(reversed(chain))

    def undo(self, revision_id: str) -> str:
        """Return the parent as a new branch point, without changing history."""
        parent = self.get(revision_id)["parent_revision"]
        if parent is None:
            raise RecipeError("Root revision has no parent")
        return parent

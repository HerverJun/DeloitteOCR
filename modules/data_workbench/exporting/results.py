"""Render, independently read back, then publish comparison result artifacts.

CSV fields beginning with spreadsheet formula triggers receive an apostrophe
for safe presentation. The untouched raw/kind/value and row provenance remain
in the companion JSON and in XLSX/Parquet text fields. Every export column is
written as text, preserving identifiers and Decimal strings exactly.
"""

from __future__ import annotations

import csv
from contextlib import closing
import hashlib
import io
from itertools import chain, zip_longest
import json
import re
import tempfile
import uuid
from zipfile import BadZipFile, ZipFile
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock

import duckdb
from defusedxml.ElementTree import iterparse
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

from modules.data_workbench.comparison.exact import ComparisonResult, ComparisonSpec, compare_exact


_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r", "\n")
_MEDIA = {"results.csv": "text/csv", "results.xlsx":
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "results.parquet": "application/vnd.apache.parquet",
          "summary.json": "application/json", "provenance.json": "application/json"}


class ExportError(ValueError):
    """The versioned result cannot be exported exactly."""


@dataclass(frozen=True)
class SourceDescriptor:
    asset_id: str
    asset_revision: int
    blob_hash: str
    format: str  # csv, xlsx or parquet
    sheet_name: str | None = None

    def __post_init__(self):
        if (not isinstance(self.asset_id, str) or not _ID.fullmatch(self.asset_id)
                or type(self.asset_revision) is not int or self.asset_revision < 1
                or not isinstance(self.blob_hash, str) or not _HASH.fullmatch(self.blob_hash)
                or not isinstance(self.format, str) or self.format not in {"csv", "xlsx", "parquet"}
                or self.format in {"csv", "parquet"} and self.sheet_name is not None
                or self.format == "xlsx" and (
                    not isinstance(self.sheet_name, str) or not self.sheet_name
                    or len(self.sheet_name) > 256)):
            raise ExportError("Invalid source descriptor")

    def locator(self) -> SourceLocator:
        from packages.contracts.models import SourceLocator
        if self.format == "xlsx":
            return SourceLocator(schema_version="1.0", kind="table", asset_id=self.asset_id,
                                 asset_revision=self.asset_revision, blob_hash=self.blob_hash,
                                 sheet_name=self.sheet_name, table_number=1, precision="exact")
        return SourceLocator(schema_version="1.0", kind="file", asset_id=self.asset_id,
                             asset_revision=self.asset_revision, blob_hash=self.blob_hash,
                             precision="exact")


@dataclass(frozen=True)
class ExportedBundle:
    comparison_id: str
    directory: Path
    files: dict[str, Path]
    artifacts: dict[str, str]
    summary_hash: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_result(result: ComparisonResult, left: dict, right: dict) -> None:
    if not isinstance(result, ComparisonResult):
        raise ExportError("Comparison result is required")
    policy = result.policy
    try:
        spec = ComparisonSpec(
            key_pairs=tuple(tuple(pair) for pair in policy["key_pairs"]),
            compare_pairs=tuple(tuple(pair) for pair in policy["compare_pairs"]),
            amount_pairs=tuple(tuple(pair) for pair in policy["amount_pairs"]),
            type_policy=policy["type_policy"], null_policy=policy["null_policy"],
            amount_tolerance=policy["amount_tolerance"],
            trim_text=policy["trim_text"], casefold_text=policy["casefold_text"],
            amount_format=policy.get("amount_format", "plain-decimal-v1"))
        actual = compare_exact(left, right, left_revision=result.left_revision,
                               right_revision=result.right_revision, spec=spec)
    except (KeyError, TypeError, ValueError):
        raise ExportError("Comparison result is invalid") from None
    if _canonical(actual.to_dict()) != _canonical(result.to_dict()):
        raise ExportError("Comparison result changed")


def _headers(left: dict, right: dict) -> list[str]:
    fixed = ["category", "key_json", "left_row_id", "left_source_row",
             "right_row_id", "right_source_row", "different_columns_json"]
    for side, snapshot in (("L", left), ("R", right)):
        for column in snapshot["columns"]:
            for suffix in ("kind", "value", "raw", "formula", "warning"):
                fixed.append(f"{side}_{column['column_id']}_{suffix}")
    return fixed


def _index(snapshot: dict) -> dict[str, dict]:
    return {row["row_id"]: row for row in snapshot["rows"]}


def _fields(row: dict | None, count: int) -> list[str]:
    if row is None:
        return [""] * (count * 5)
    values = []
    for cell in row["cells"]:
        values.extend("" if cell.get(key) is None else str(cell[key])
                      for key in ("kind", "value", "raw", "formula", "warning"))
    return values


def _source(ref, descriptor: SourceDescriptor | None):
    if ref is None or descriptor is None:
        return None
    return {"revision_id": ref.revision_id, "row_id": ref.row_id,
            "source_row": ref.source_row, **asdict(descriptor)}


def _rows(result: ComparisonResult, left: dict, right: dict,
          left_source: SourceDescriptor, right_source: SourceDescriptor):
    left_rows, right_rows = _index(left), _index(right)
    output, provenance = [], []

    def add(category, key, left_ref=None, right_ref=None, differences=()):
        left_row = left_rows.get(left_ref.row_id) if left_ref else None
        right_row = right_rows.get(right_ref.row_id) if right_ref else None
        record = [category, json.dumps(key, ensure_ascii=False, separators=(",", ":")),
                  left_ref.row_id if left_ref else "", str(left_ref.source_row) if left_ref else "",
                  right_ref.row_id if right_ref else "", str(right_ref.source_row) if right_ref else "",
                  json.dumps(differences, ensure_ascii=False, separators=(",", ":"))]
        record += _fields(left_row, len(left["columns"])) + _fields(right_row, len(right["columns"]))
        output.append(record)
        provenance.append({"category": category, "left": _source(left_ref, left_source),
                           "right": _source(right_ref, right_source)})

    for pair in result.same:
        add("same", pair.key, pair.left, pair.right)
    for pair in result.different:
        add("different", pair.key, pair.left, pair.right, pair.different_columns)
    for item in result.only_left:
        add("only_left", [], item.row)
    for item in result.only_right:
        add("only_right", [], right_ref=item.row)
    for group in result.ambiguous:
        for ref in group.left:
            add("ambiguous_left", group.key, ref)
        for ref in group.right:
            add("ambiguous_right", group.key, right_ref=ref)
    return output, provenance


def _safe_csv(value: str) -> str:
    examined = value.lstrip(" \t\r\n\ufeff")
    unsafe = value.startswith(_FORMULA_PREFIX) or examined.startswith(("=", "+", "-", "@"))
    return "'" + value if unsafe else value


def _csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows([_safe_csv(value) for value in row] for row in rows)


def _xlsx(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Comparison")
    for values in chain((headers,), rows):
        cells = []
        for value in values:
            if value.startswith("="):
                cell = WriteOnlyCell(sheet, value=value)
                cell.data_type = "s"
                cell.number_format = "@"
                cells.append(cell)
            else:
                cells.append(value)
        sheet.append(cells)
    workbook.save(path)
    workbook.close()


def _parquet(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    if len(set(headers)) != len(headers):
        raise ExportError("Parquet columns are not unique")
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".parquet-source-",
                                         suffix=".jsonl", delete=False) as stream:
            staged = Path(stream.name)
            for row in rows:
                if len(row) != len(headers):
                    raise ExportError("Parquet row width differs")
                stream.write(json.dumps(dict(zip(headers, row)), ensure_ascii=False,
                                        separators=(",", ":"), allow_nan=False) + "\n")
        connection = duckdb.connect(config={"autoinstall_known_extensions": "false",
                                            "autoload_known_extensions": "false",
                                            "allow_community_extensions": "false",
                                            "memory_limit": "256MB", "threads": "2"})
        try:
            connection.execute("CREATE TABLE export_rows AS SELECT * FROM read_json(?, columns=?)",
                               [str(staged), {header: "VARCHAR" for header in headers}])
            connection.execute("COPY export_rows TO ? (FORMAT PARQUET)", [str(path)])
        finally:
            connection.close()
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _same_rows(actual, expected, kind: str) -> None:
    missing = object()
    for position, (found, wanted) in enumerate(zip_longest(actual, expected, fillvalue=missing), start=1):
        if found is missing or wanted is missing or found != wanted:
            raise ExportError(f"{kind} readback differs at row {position}")


def _xlsx_rows(path: Path, width: int):
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with ZipFile(path) as archive, archive.open("xl/worksheets/sheet1.xml") as stream:
            row_number = 0
            for _, element in iterparse(stream, events=("end",)):
                if element.tag != namespace + "row":
                    continue
                row_number += 1
                if element.get("r") != str(row_number):
                    raise ExportError("XLSX row order changed")
                values = [""] * width
                used = set()
                for cell in element.findall(namespace + "c"):
                    if cell.find(namespace + "f") is not None:
                        raise ExportError("XLSX contains a formula")
                    if cell.get("t") != "inlineStr":
                        raise ExportError("XLSX contains a non-text cell")
                    column, observed_row = coordinate_from_string(cell.get("r", ""))
                    offset = column_index_from_string(column) - 1
                    if observed_row != row_number or not 0 <= offset < width or offset in used:
                        raise ExportError("XLSX cell position changed")
                    used.add(offset)
                    inline = cell.find(namespace + "is")
                    if inline is not None:
                        values[offset] = "".join(node.text or "" for node in inline.iter(namespace + "t"))
                yield values
                element.clear()
    except ExportError:
        raise
    except (BadZipFile, KeyError, ValueError):
        raise ExportError("XLSX readback is invalid") from None


def _readback(files: dict[str, Path], headers: list[str], rows: list[list[str]]) -> None:
    with files["results.csv"].open("r", encoding="utf-8-sig", newline="") as stream:
        _same_rows(csv.reader(stream),
                   chain((headers,), ([_safe_csv(value) for value in row] for row in rows)), "CSV")
    with closing(_xlsx_rows(files["results.xlsx"], len(headers))) as xlsx_rows:
        _same_rows(xlsx_rows, chain((headers,), rows), "XLSX")
    with duckdb.connect(config={"autoinstall_known_extensions": "false",
                                "autoload_known_extensions": "false"}) as connection:
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(files["results.parquet"])])
        if [column[0] for column in cursor.description] != headers:
            raise ExportError("Parquet column names differ")
        position = 0
        while batch := cursor.fetchmany(2048):
            for row in batch:
                if position >= len(rows) or list(row) != rows[position]:
                    raise ExportError(f"Parquet readback differs at row {position + 1}")
                position += 1
        if position != len(rows):
            raise ExportError("Parquet readback row count differs")


def export_and_publish(result: ComparisonResult, left_snapshot: dict, right_snapshot: dict, *,
                       left_source: SourceDescriptor, right_source: SourceDescriptor,
                       output_root: str | Path, artifact_store: ArtifactStore,
                       workspace_id: str, job_ref: JobRef,
                       application_version: str, rule_version: str) -> ExportedBundle:
    from packages.contracts.models import JobRef
    from workbench_platform.artifacts import ArtifactStore
    if (not isinstance(left_source, SourceDescriptor) or not isinstance(right_source, SourceDescriptor)
            or not isinstance(artifact_store, ArtifactStore)
            or not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id)
            or not isinstance(job_ref, JobRef)):
        raise ExportError("Invalid export identity")
    _validated_result(result, left_snapshot, right_snapshot)
    input_identity = {"comparison_id": result.comparison_id,
                      "left_source": asdict(left_source), "right_source": asdict(right_source)}
    if job_ref.input_manifest_hash != hashlib.sha256(_canonical(input_identity)).hexdigest():
        raise ExportError("Export job input identity differs")
    raw_root = Path(output_root)
    if raw_root.is_symlink() or (hasattr(raw_root, "is_junction") and raw_root.is_junction()):
        raise ExportError("Export directory is unavailable")
    root = raw_root.resolve(strict=True)
    if not root.is_dir():
        raise ExportError("Export directory is unavailable")
    folder = root / (result.comparison_id + "-" + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    headers = _headers(left_snapshot, right_snapshot)
    rows, provenance_rows = _rows(result, left_snapshot, right_snapshot, left_source, right_source)
    files = {name: folder / name for name in _MEDIA}
    _csv(files["results.csv"], headers, rows)
    _xlsx(files["results.xlsx"], headers, rows)
    _parquet(files["results.parquet"], headers, rows)
    provenance = {"comparison_id": result.comparison_id,
                  "left_source": asdict(left_source), "right_source": asdict(right_source),
                  "rows": provenance_rows}
    files["provenance.json"].write_bytes(_canonical(provenance))
    summary = {"comparison_id": result.comparison_id,
               "left_revision": result.left_revision, "right_revision": result.right_revision,
               "policy": result.policy, "coverage": result.coverage,
               "export_rows": len(rows), "csv_text_policy": "prefix_apostrophe_for_formula_prefix",
               "raw_literal_policy": "XLSX and Parquet preserve original text; kind and raw columns distinguish null and empty text",
               "format_sha256": {name: _digest(files[name]) for name in
                                 ("results.csv", "results.xlsx", "results.parquet", "provenance.json")}}
    files["summary.json"].write_bytes(_canonical(summary))
    _readback(files, headers, rows)
    if _canonical(json.loads(files["provenance.json"].read_text("utf-8"))) != _canonical(provenance):
        raise ExportError("Provenance readback differs")
    if _canonical(json.loads(files["summary.json"].read_text("utf-8"))) != _canonical(summary):
        raise ExportError("Summary readback differs")
    locators = [left_source.locator(), right_source.locator()]
    artifacts = {}
    for name, media_type in _MEDIA.items():
        path = files[name]
        with path.open("rb") as stream:
            artifact = artifact_store.publish_managed(
                workspace_id, stream, expected_hash=_digest(path), expected_size=path.stat().st_size,
                job_ref=job_ref, source_refs=locators, media_type=media_type,
                application_version=application_version, rule_or_model_version=rule_version)
        artifacts[name] = artifact.artifact_id
    return ExportedBundle(result.comparison_id, folder, files, artifacts, _digest(files["summary.json"]))


_REVIEW_XLSX_TEXT_UNITS = 32000
_EXCEL_MAX_ROWS = 1048576
_EXCEL_MAX_COLUMNS = 16384
_REVIEW_CSV_READBACK_LOCK = RLock()


def _review_utf16_units(value: str) -> int:
    try:
        return len(value.encode('utf-16-le')) // 2
    except UnicodeError:
        raise ExportError('Review XLSX text contains an invalid Unicode scalar') from None


def _review_text_parts(value: str) -> list[str]:
    if _review_utf16_units(value) <= _REVIEW_XLSX_TEXT_UNITS:
        return [value]
    parts, start, units = [], 0, 0
    for position, character in enumerate(value):
        size = 2 if ord(character) > 0xffff else 1
        if units + size > _REVIEW_XLSX_TEXT_UNITS:
            parts.append(value[start:position])
            start, units = position, 0
        units += size
    parts.append(value[start:])
    return parts


def _review_xlsx_layout(headers: list[str], rows: list[list[str]]):
    """Keep logical values intact while fitting every physical Excel text cell."""
    if len(rows) + 1 > _EXCEL_MAX_ROWS:
        raise ExportError('Review XLSX exceeds the Excel worksheet row limit')
    if len(headers) > _EXCEL_MAX_COLUMNS:
        raise ExportError('Review XLSX exceeds the Excel worksheet column limit')
    counts = [1] * len(headers)
    split_rows, spills = [], []
    for row_number, row in enumerate(rows, start=2):
        if len(row) != len(headers):
            raise ExportError('Review XLSX logical row width differs')
        parts_row = []
        for offset, value in enumerate(row):
            parts = _review_text_parts(value)
            counts[offset] = max(counts[offset], len(parts))
            if sum(counts) > _EXCEL_MAX_COLUMNS:
                raise ExportError('Review XLSX exceeds the Excel worksheet column limit')
            parts_row.append(parts)
            if len(parts) > 1:
                spills.append({'row_number': row_number, 'column': headers[offset],
                    'parts': len(parts), 'characters': len(value),
                    'utf16_units': _review_utf16_units(value),
                    'utf8_sha256': hashlib.sha256(value.encode('utf-8')).hexdigest()})
        split_rows.append(parts_row)
    columns = {header: [header, *(f'{header}__part_{part}' for part in range(2, counts[offset] + 1))]
               for offset, header in enumerate(headers)}
    physical_headers = [*headers, *(part for header in headers for part in columns[header][1:])]
    if len(set(physical_headers)) != len(physical_headers):
        raise ExportError('Review XLSX physical columns are not unique')
    physical_rows = [[*(parts[0] for parts in row),
        *(parts[part] if part < len(parts) else '' for offset, parts in enumerate(row)
          for part in range(1, counts[offset]))] for row in split_rows]
    encoding = None
    if spills:
        encoding = {'schema_version': '1.0', 'kind': 'utf16_column_chunks',
            'max_utf16_units': _REVIEW_XLSX_TEXT_UNITS, 'logical_headers': headers,
            'physical_headers': physical_headers, 'columns': columns, 'spills': spills}
    return physical_headers, physical_rows, encoding


def _review_xlsx_logical_rows(path: Path, headers: list[str], encoding: dict | None):
    """Reconstruct actual worksheet bytes, checking each declared spill's identity."""
    if encoding is None:
        yield from _xlsx_rows(path, len(headers))
        return
    if (encoding.get('schema_version') != '1.0' or encoding.get('kind') != 'utf16_column_chunks'
            or encoding.get('max_utf16_units') != _REVIEW_XLSX_TEXT_UNITS
            or encoding.get('logical_headers') != headers):
        raise ExportError('Review XLSX text encoding differs')
    physical_headers, columns = encoding['physical_headers'], encoding['columns']
    offsets = {header: offset for offset, header in enumerate(physical_headers)}
    if (physical_headers[:len(headers)] != headers or len(offsets) != len(physical_headers)
            or set(columns) != set(headers)
            or physical_headers != [*headers, *(name for header in headers for name in columns[header][1:])]
            or any(names != [header, *(f'{header}__part_{part}' for part in range(2, len(names) + 1))]
                   for header, names in columns.items())):
        raise ExportError('Review XLSX text column layout differs')
    spills = {}
    for spill in encoding['spills']:
        key = (spill['row_number'], spill['column'])
        if key in spills or key[1] not in columns:
            raise ExportError('Review XLSX text spill identity differs')
        spills[key] = spill
    seen = set()
    with closing(_xlsx_rows(path, len(physical_headers))) as readback:
        for row_number, row in enumerate(readback, start=1):
            if row_number == 1:
                if row != physical_headers:
                    raise ExportError('Review XLSX physical headers differ')
                yield headers
                continue
            logical = []
            for header in headers:
                parts = [row[offsets[name]] for name in columns[header]]
                if any(_review_utf16_units(part) > _REVIEW_XLSX_TEXT_UNITS for part in parts):
                    raise ExportError('Review XLSX physical text exceeds its limit')
                value = ''.join(parts)
                spill = spills.get((row_number, header))
                if spill:
                    count = spill['parts']
                    if (type(count) is not int or not 2 <= count <= len(parts)
                            or not all(parts[:count]) or any(parts[count:])
                            or spill['characters'] != len(value)
                            or spill['utf16_units'] != _review_utf16_units(value)
                            or spill['utf16_units'] <= _REVIEW_XLSX_TEXT_UNITS
                            or spill['utf8_sha256'] != hashlib.sha256(value.encode('utf-8')).hexdigest()):
                        raise ExportError('Review XLSX reconstructed spill differs')
                    seen.add((row_number, header))
                elif any(parts[1:]) or _review_utf16_units(value) > _REVIEW_XLSX_TEXT_UNITS:
                    raise ExportError('Review XLSX text spill is missing')
                logical.append(value)
            yield logical
    if seen != set(spills):
        raise ExportError('Review XLSX has surplus text spills')


def _review_csv_readback(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    # csv's field-size setting is process-global. Serialize review readers so
    # concurrent publications cannot restore a limit while another is reading.
    with _REVIEW_CSV_READBACK_LOCK:
        previous_limit = csv.field_size_limit()
        required_limit = max((len(value) + 1 for row in rows for value in row), default=previous_limit)
        try:
            csv.field_size_limit(max(previous_limit, required_limit))
            with path.open(encoding='utf-8-sig', newline='') as stream:
                _same_rows(csv.reader(stream), chain((headers,),
                    ([_safe_csv(value) for value in row] for row in rows)), 'Review CSV')
        finally:
            csv.field_size_limit(previous_limit)


def render_review_export(directory: Path, snapshot: dict, result_rows: list[dict]) -> dict[str, Path]:
    """Render only frozen platform judgments; independently verify every format."""
    batch, items = snapshot['batch'], snapshot['items']
    by_ordinal = {item['raw_output']['ordinal']: item for item in items if 'ordinal' in item.get('raw_output', {})}
    accepted = {}
    for operation in snapshot['operations']:
        if operation['state'] == 'committed' and operation['payload']['action'] == 'confirm':
            for segment in operation['intent'].get('segments', []):
                if segment.get('snapshot'):
                    for binding in segment['bindings']:
                        accepted[binding['platform_item_id']] = segment['snapshot']
    headers = ['ordinal', 'item_id', 'category', 'disposition', 'note', 'adopted_revision',
               'adopted_value', 'original_json', 'adoption_json', 'source_json']
    rows, sources, used = [], [], set()
    for result in result_rows:
        item = by_ordinal.get(result['ordinal'])
        if item:
            used.add(item['item_id'])
        original = item.get('raw_output', result) if item else result
        adoption = item.get('adoption', {}) if item else {}
        adopted = adoption.get('snapshot', accepted.get(item['item_id'], {}) if item else {})
        if 'text' in adopted.get('edited', {}):
            value = adopted['edited']['text']
        else:
            value = adoption.get('human_label_id', adopted.get('original', {}).get('text',
                original.get('edited_output', {}).get('text', original.get('text', original.get('prediction_label_id', '')))))
        source = item.get('source', {}) if item else {'result_identity': snapshot.get('index', {}).get('result_identity')}
        rows.append([str(result['ordinal']), item['item_id'] if item else '', item['category'] if item else result.get('category', 'review'),
            item['disposition'] if item else 'not_required', item.get('note', '') if item else '',
            str(adoption['native_revision']) if adoption else '', str(value or ''),
            _canonical(original).decode(), _canonical(adoption).decode(), _canonical(source).decode()])
        sources.append({'ordinal': result['ordinal'], 'item_id': item['item_id'] if item else None,
                        'source': source, 'adoption': adoption or None})
    for item in items:
        if item['item_id'] not in used:
            rows.append(['', item['item_id'], item['category'], item['disposition'], item.get('note', ''), '', '',
                         _canonical(item['raw_output']).decode(), _canonical(item.get('adoption')).decode(),
                         _canonical(item['source']).decode()])
    unresolved = [{'item_id': item['item_id'], 'category': item['category'], 'disposition': item['disposition'],
                   'title': item.get('title', item['category']), 'note': item.get('note', '')} for item in items if item['disposition'] != 'confirmed']
    constraints = []
    if batch['status'] != 'succeeded':
        constraints.append({'kind': 'execution_incomplete', 'status': batch['status']})
    if not batch['coverage']['index_complete']:
        constraints.append({'kind': 'coverage_incomplete', 'coverage': batch['coverage']})
    for operation in snapshot['operations']:
        if operation['state'] != 'committed':
            constraints.append({'kind': 'operation_unverified', 'operation_id': operation['operation_id'], 'state': operation['state']})
    documents = {'review.json': {**snapshot, 'results': result_rows, 'unresolved': unresolved, 'constraints': constraints},
        'history.json': {'batch_id': batch['batch_id'], 'revision': batch['revision'], 'items': snapshot['history']},
        'adoptions.json': {'batch_id': batch['batch_id'], 'revision': batch['revision'], 'items':
            [{'item_id': item['item_id'], **item['adoption']} for item in items if item.get('adoption')]},
        'provenance.json': {'schema_version': '1.0', 'batch_id': batch['batch_id'], 'revision': batch['revision'],
            'inputs': batch['inputs'], 'template': batch['template'], 'coverage': batch['coverage'],
            'index': snapshot['index'], 'original_bundles': snapshot['original_bundles'],
            'rows': sources, 'csv_text_policy': 'prefix_apostrophe_for_formula_prefix',
            'review_scope': batch['summary'].get('review_scope', 'platform_fixed_result'),
            'native_review_qualification': batch['summary'].get('native_review_qualification', 'native_disposition' if
                batch['template']['scenario'] == 'classification' else 'not_applicable')}}
    physical_headers, physical_rows, encoding = _review_xlsx_layout(headers, rows)
    if encoding is not None:
        documents['provenance.json']['xlsx_text_encoding'] = encoding
    files = {name: directory / name for name in ('results.csv', 'results.xlsx', *documents, 'review.md')}
    _csv(files['results.csv'], headers, rows)
    _xlsx(files['results.xlsx'], physical_headers, physical_rows)
    for name, document in documents.items():
        files[name].write_bytes(_canonical(document))
    actor = snapshot['request']['actor']
    lines = ['# 复核成果', '', f"交付类型：{'正式复核成果' if snapshot['request']['kind'] == 'final' else '中间材料（尚有未完成条件）'}",
        f"批次：{batch['batch_id']}；固定处置版本：{batch['revision']}",
        f"编制人：{actor['display_name']}（{actor['id']}，本机身份，非组织认证）",
        f"处理状态：{batch['status']}；原固定结果：{batch['coverage']['result_rows']}；已索引事项：{batch['coverage']['indexed_items']}",
        f"全量索引完成：{batch['coverage']['index_complete']}；未解决事项：{len(unresolved)}；待核实操作/覆盖条件：{len(constraints)}",
        f"后继于：{snapshot.get('predecessor_bundle_id') or '原始成果'}", '',
        '原始输出与采用修订分别保留。来源、版本、全量历史及采用关系见同包 JSON 文件。']
    if batch['template']['scenario'] == 'ocr':
        lines += ['', '本成果确认平台固定结果；OCR 原生复核资格独立，未表示原生复核已通过。']
    lines += ['', '## 未解决事项']
    lines += [f"- {item['item_id']}｜{item['title']}｜{item['disposition']}｜{item['note']}" for item in unresolved] or ['无。']
    lines += ['', '## 未完成条件']
    lines += ['- ' + _canonical(item).decode() for item in constraints] or ['无。']
    markdown = '\n'.join(lines) + '\n'
    files['review.md'].write_text(markdown, encoding='utf-8')
    _review_csv_readback(files['results.csv'], headers, rows)
    with closing(_xlsx_rows(files['results.xlsx'], len(physical_headers))) as readback:
        _same_rows(readback, chain((physical_headers,), physical_rows), 'Review XLSX physical')
    for name, document in documents.items():
        if _canonical(json.loads(files[name].read_text('utf-8'))) != _canonical(document):
            raise ExportError('Review JSON readback differs')
    written_encoding = json.loads(files['provenance.json'].read_text('utf-8')).get('xlsx_text_encoding')
    with closing(_review_xlsx_logical_rows(files['results.xlsx'], headers, written_encoding)) as readback:
        _same_rows(readback, chain((headers,), rows), 'Review XLSX')
    if files['review.md'].read_text('utf-8') != markdown:
        raise ExportError('Review summary readback differs')
    return files

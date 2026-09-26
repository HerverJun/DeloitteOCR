"""Render, independently read back, then publish comparison result artifacts.

CSV fields beginning with spreadsheet formula triggers receive an apostrophe
for safe presentation. The untouched raw/kind/value and row provenance remain
in the companion JSON and in XLSX/Parquet text fields. Every export column is
written as text, preserving identifiers and Decimal strings exactly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
from openpyxl import Workbook, load_workbook

from modules.data_workbench.comparison.exact import ComparisonResult, ComparisonSpec, compare_exact
from packages.contracts.models import JobRef, SourceLocator
from workbench_platform.artifacts import ArtifactStore


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
    format: str  # csv or xlsx
    sheet_name: str | None = None

    def __post_init__(self):
        if (not isinstance(self.asset_id, str) or not _ID.fullmatch(self.asset_id)
                or type(self.asset_revision) is not int or self.asset_revision < 1
                or not isinstance(self.blob_hash, str) or not _HASH.fullmatch(self.blob_hash)
                or not isinstance(self.format, str) or self.format not in {"csv", "xlsx"}
                or (self.format == "xlsx") != (isinstance(self.sheet_name, str) and bool(self.sheet_name))
                or self.sheet_name is not None and len(self.sheet_name) > 256):
            raise ExportError("Invalid source descriptor")

    def locator(self) -> SourceLocator:
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
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
            trim_text=policy["trim_text"], casefold_text=policy["casefold_text"])
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
    return "'" + value if value.startswith(_FORMULA_PREFIX) else value


def _csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows([_safe_csv(value) for value in row] for row in rows)


def _xlsx(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    workbook = Workbook(write_only=False)
    sheet = workbook.active
    sheet.title = "Comparison"
    for row_index, values in enumerate([headers, *rows], start=1):
        for column_index, value in enumerate(values, start=1):
            cell = sheet.cell(row_index, column_index, value=value)
            cell.data_type = "s"  # Explicit text even for a leading '='.
            cell.number_format = "@"
    workbook.save(path)
    workbook.close()


def _parquet(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    connection = duckdb.connect(config={"autoinstall_known_extensions": "false",
                                        "autoload_known_extensions": "false",
                                        "allow_community_extensions": "false",
                                        "memory_limit": "256MB", "threads": "2"})
    try:
        columns = ",".join(f'"{header}" VARCHAR' for header in headers)
        connection.execute(f"CREATE TABLE export_rows ({columns})")
        if rows:
            markers = ",".join("?" for _ in headers)
            connection.executemany(f"INSERT INTO export_rows VALUES ({markers})", rows)
        connection.execute("COPY export_rows TO ? (FORMAT PARQUET)", [str(path)])
    finally:
        connection.close()


def _readback(files: dict[str, Path], headers: list[str], rows: list[list[str]]) -> None:
    with files["results.csv"].open("r", encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.reader(stream))
    if csv_rows != [headers, *[[_safe_csv(value) for value in row] for row in rows]]:
        raise ExportError("CSV readback differs")
    workbook = load_workbook(files["results.xlsx"], read_only=True, data_only=False, keep_links=False)
    try:
        xlsx_rows = []
        for row in workbook.active.iter_rows():
            if any(cell.data_type == "f" for cell in row):
                raise ExportError("XLSX contains a formula")
            xlsx_rows.append(["" if cell.value is None else str(cell.value) for cell in row])
    finally:
        workbook.close()
    if xlsx_rows != [headers, *rows]:
        raise ExportError("XLSX readback differs")
    with duckdb.connect(config={"autoinstall_known_extensions": "false",
                                "autoload_known_extensions": "false"}) as connection:
        parquet_rows = connection.execute("SELECT * FROM read_parquet(?)", [str(files["results.parquet"])]).fetchall()
        parquet_headers = [column[0] for column in connection.description]
    if parquet_headers != headers or [list(row) for row in parquet_rows] != rows:
        raise ExportError("Parquet readback differs")


def export_and_publish(result: ComparisonResult, left_snapshot: dict, right_snapshot: dict, *,
                       left_source: SourceDescriptor, right_source: SourceDescriptor,
                       output_root: str | Path, artifact_store: ArtifactStore,
                       workspace_id: str, job_ref: JobRef,
                       application_version: str, rule_version: str) -> ExportedBundle:
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

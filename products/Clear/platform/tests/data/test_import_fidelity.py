"""Loss-aware import and reversible distinctions in canonical JSON export."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from modules.data_workbench.importing import TableImportError, import_csv, import_xlsx


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_csv_quotes_empty_null_zero_and_multiline_are_distinct(tmp_path: Path):
    path = tmp_path / "source.csv"
    path.write_bytes('id,value,note\r\n1,0,""\r\n2,,x\r\n3,"a,b","line1\r\nline2"\r\n'.encode("utf-8"))
    table = import_csv(path, source_sha256=sha(path))
    assert [column.header for column in table.columns] == ["id", "value", "note"]
    assert table.rows[0].cells[1].raw == "0"
    assert table.rows[0].cells[2].kind == "text"
    assert table.rows[0].cells[2].value == ""
    assert table.rows[1].cells[1].kind == "null"
    assert table.rows[2].cells[1].raw == "a,b"
    assert table.rows[2].cells[2].raw == "line1\r\nline2"
    encoded = json.loads(table.to_json())
    assert encoded["rows"][0]["cells"][2]["kind"] == "text"
    assert encoded["rows"][1]["cells"][1]["kind"] == "null"
    assert table.rows[0].row_id == import_csv(path, source_sha256=sha(path)).rows[0].row_id


def test_csv_selection_and_strict_encoding(tmp_path: Path):
    path = tmp_path / "source.csv"
    path.write_text("前言;忽略\n姓名;金额\n甲;1.20\n", encoding="utf-16")
    table = import_csv(path, source_sha256=sha(path), encoding="utf-16", delimiter=";", header_row=2)
    assert [column.header for column in table.columns] == ["姓名", "金额"]
    assert table.rows[0].source_row == 3
    assert table.rows[0].cells[1].raw == "1.20"
    with pytest.raises(TableImportError, match="encoding"):
        import_csv(path, source_sha256=sha(path), encoding="utf-8")
    with pytest.raises(TableImportError, match="changed"):
        import_csv(path, source_sha256="0" * 64)


def test_csv_malformed_quotes_and_ragged_rows_rejected(tmp_path: Path):
    path = tmp_path / "source.csv"
    path.write_text('a,b\n"open,b\n', encoding="utf-8")
    with pytest.raises(TableImportError, match="Unclosed"):
        import_csv(path, source_sha256=sha(path))
    path.write_text("a,b\n1\n", encoding="utf-8")
    with pytest.raises(TableImportError, match="width"):
        import_csv(path, source_sha256=sha(path))


def _workbook(path: Path):
    book = Workbook()
    book.active.title = "说明"
    book.active.append(["跳过", "跳过"])
    sheet = book.create_sheet("工资")
    sheet.append(["编号", "金额", "空文本", "公式", "缺失", "高精度数"])
    sheet.append(["00123456789012345678", 0, "", "=B2+1", None, 1234567890123456])
    book.save(path)


def test_excel_sheet_formula_cache_precision_and_text_identifier(tmp_path: Path):
    path = tmp_path / "source.xlsx"
    _workbook(path)
    table = import_xlsx(path, source_sha256=sha(path), sheet_name="工资")
    assert table.rows[0].cells[0].value == "00123456789012345678"
    assert table.rows[0].cells[1].kind == "decimal"
    assert table.rows[0].cells[1].value == "0"
    assert table.rows[0].cells[2].kind == "text"
    assert table.rows[0].cells[2].value == ""
    assert table.rows[0].cells[3].kind == "unknown_formula"
    assert table.rows[0].cells[3].formula == "=B2+1"
    assert table.rows[0].cells[4].kind == "null"
    assert table.rows[0].cells[5].warning == "excel_numeric_precision_unverifiable"
    assert "excel_numeric_precision_unverifiable" in table.warnings
    assert table.rows[0].row_id == import_xlsx(path, source_sha256=sha(path), sheet_name="工资").rows[0].row_id
    assert json.loads(table.to_json())["rows"][0]["cells"][3]["kind"] == "unknown_formula"


def test_excel_cached_formula_is_distinct_from_unknown(tmp_path: Path):
    original, cached = tmp_path / "original.xlsx", tmp_path / "cached.xlsx"
    _workbook(original)
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(cached, "w") as dest:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet2.xml":
                content = content.replace(b"<f>B2+1</f><v />", b"<f>B2+1</f><v>1</v>")
            dest.writestr(item, content)
    table = import_xlsx(cached, source_sha256=sha(cached), sheet_name="工资")
    formula = table.rows[0].cells[3]
    assert formula.kind == "decimal"
    assert formula.raw == "1"
    assert formula.value == "1"
    assert formula.formula == "=B2+1"


def test_excel_sheet_selection_and_revision(tmp_path: Path):
    path = tmp_path / "source.xlsx"
    _workbook(path)
    with pytest.raises(TableImportError, match="Worksheet"):
        import_xlsx(path, source_sha256=sha(path), sheet_name="missing")
    no_header = import_xlsx(path, source_sha256=sha(path), sheet_name="工资", header_row=None)
    with_header = import_xlsx(path, source_sha256=sha(path), sheet_name="工资", header_row=1)
    assert no_header.revision != with_header.revision
    assert no_header.rows[0].source_row == 1
    assert all(column.header is None for column in no_header.columns)

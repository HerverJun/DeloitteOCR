"""Real isolated DuckDB subprocess and negative path/control cases."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

import duckdb
import pytest
from openpyxl import Workbook

from modules.data_workbench import AnalysisCancelled, DataRuntime, DataRuntimeError


def runtime(tmp_path: Path) -> tuple[DataRuntime, Path, Path]:
    assets, jobs = tmp_path / "assets", tmp_path / "jobs"
    assets.mkdir()
    jobs.mkdir()
    return DataRuntime(authorized_root=assets, jobs_root=jobs,
                       python_executable=sys.executable), assets, jobs


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_csv_is_analyzed_in_a_private_database(tmp_path: Path):
    engine, assets, jobs = runtime(tmp_path)
    source = assets / "汉字.csv"
    source.write_text("姓名,数值\n甲,2\n乙,3\n", encoding="utf-8")
    result = engine.analyze(job_id="task01", source=source.name, source_sha256=digest(source))
    assert result.rows == 2
    assert result.columns == ("姓名", "数值")
    assert json.loads(result.report_path.read_text("utf-8"))["source_sha256"] == digest(source)
    assert result.database_path == jobs / "task01" / "analysis.duckdb"
    assert not (jobs / "task01" / "input.csv").exists()
    with duckdb.connect(str(result.database_path), read_only=True) as connection:
        assert connection.execute("SELECT sum(数值) FROM source").fetchone()[0] == 5


def test_excel_reader_uses_values_and_private_worker(tmp_path: Path):
    engine, assets, _ = runtime(tmp_path)
    source = assets / "sheet.xlsx"
    book = Workbook()
    book.active.append(["项目", "金额"])
    book.active.append(["甲", 8])
    book.save(source)
    result = engine.analyze(job_id="excel01", source=source.name, source_sha256=digest(source))
    assert result.rows == 1
    assert result.columns == ("项目", "金额")


def test_parquet_works_without_an_ocr_runtime(tmp_path: Path):
    engine, assets, _ = runtime(tmp_path)
    source = assets / "sample.parquet"
    with duckdb.connect() as connection:
        connection.execute("COPY (SELECT 1 AS amount UNION ALL SELECT 3) TO ? (FORMAT PARQUET)",
                           [str(source)])
    result = engine.analyze(job_id="parquet01", source=source.name, source_sha256=digest(source))
    assert result.rows == 2
    assert result.columns == ("amount",)


def test_source_identity_hash_and_job_replay_fail_closed(tmp_path: Path):
    engine, assets, _ = runtime(tmp_path)
    source = assets / "input.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("secret\n", encoding="utf-8")
    for name in ("../outside.csv", "C:/outside.csv", "\\\\server\\share.csv"):
        with pytest.raises(DataRuntimeError):
            engine.analyze(job_id="badpath", source=name, source_sha256=digest(outside))
    with pytest.raises(DataRuntimeError, match="changed"):
        engine.analyze(job_id="wronghash", source=source.name, source_sha256="0" * 64)
    engine.analyze(job_id="once", source=source.name, source_sha256=digest(source))
    with pytest.raises(DataRuntimeError, match="already exists"):
        engine.analyze(job_id="once", source=source.name, source_sha256=digest(source))


def test_cancel_before_worker_starts(tmp_path: Path):
    engine, assets, jobs = runtime(tmp_path)
    source = assets / "input.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AnalysisCancelled):
        engine.analyze(job_id="cancelled", source=source.name,
                       source_sha256=digest(source), cancel=cancel)
    assert not (jobs / "cancelled" / "analysis.duckdb").exists()


def test_worker_disables_external_access_and_extensions(tmp_path: Path):
    engine, assets, jobs = runtime(tmp_path)
    source = assets / "input.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    engine.analyze(job_id="policy", source=source.name, source_sha256=digest(source))
    report = json.loads((jobs / "policy" / "report.json").read_text("utf-8"))
    assert report["external_access"] is False
    assert report["extension_autoinstall"] is False
    assert report["extension_autoload"] is False
    # The same DuckDB policy rejects arbitrary file reads.
    secret = tmp_path / "secret.csv"
    secret.write_text("private\n", encoding="utf-8")
    with duckdb.connect(str(jobs / "policy" / "analysis.duckdb"), config={
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
    }) as connection:
        connection.execute("SET enable_external_access = false")
        with pytest.raises(duckdb.Error):
            connection.execute("SELECT * FROM read_csv(?)", [str(secret)])

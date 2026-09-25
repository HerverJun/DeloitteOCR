"""Read every committed fixture, then rebuild and independently read it elsewhere."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures"
MANIFEST = FIXTURES / "manifest.json"
BUILDER = ROOT / "scripts/fixtures/build_fixtures.py"
READER = ROOT / "scripts/fixtures/readback.py"


def fixture_python() -> Path:
    configured = os.environ.get("WORKBENCH_FIXTURE_PYTHON")
    if not configured:
        pytest.fail("Set WORKBENCH_FIXTURE_PYTHON to the locked isolated fixture Python; see scripts/fixtures/setup_runtime.py")
    python = Path(configured).resolve()
    assert python.is_file()
    return python


def invoke(*args) -> str:
    env = os.environ.copy()
    for key in tuple(env):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD")):
            env.pop(key)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run([str(fixture_python()), *map(str, args)], cwd=ROOT, env=env,
                               check=True, capture_output=True, text=True, encoding="utf-8", timeout=120)
    return completed.stdout.strip()


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readback(path: Path, media_type: str) -> dict:
    return json.loads(invoke(READER, "--path", path, "--media-type", media_type))


def test_every_fixed_file_has_license_hash_and_independently_readable_truth():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["real_public_sources"] == []
    assert len(manifest["fixtures"]) == 7
    assert {item["media_type"] for item in manifest["fixtures"]} == {
        "image/png", "application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv", "application/vnd.apache.parquet"}
    by_path = {}
    for item in manifest["fixtures"]:
        path = ROOT / "tests/fixtures" / item["path"]
        assert path.is_file()
        assert item["path"].startswith("data/") and path.resolve().is_relative_to(FIXTURES.resolve())
        assert item["origin"] == {"kind": "synthetic", "creator": "Workbench fixture generator",
                                  "license": "CC0-1.0", "upstream_url": None}
        assert hash_file(path) == item["sha256"]
        assert path.stat().st_size == item["size_bytes"] > 0
        assert readback(path, item["media_type"]) == item["expected"]
        by_path[item["path"]] = item
    same = [by_path[path] for path in manifest["relationships"]["same_bytes_distinct_sources"]]
    assert same[0]["sha256"] == same[1]["sha256"] and same[0]["source_id"] != same[1]["source_id"]
    changed = [by_path[path] for path in manifest["relationships"]["changed_source_revisions"]]
    assert changed[0]["source_id"] == changed[1]["source_id"]
    assert changed[0]["source_revision"] < changed[1]["source_revision"]
    assert changed[0]["sha256"] != changed[1]["sha256"]
    assert any("对账" in path or "票据" in path for path in by_path)


def test_rebuild_in_separate_directory_matches_bytes_and_semantics():
    fixed = json.loads(MANIFEST.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="workbench-fixtures-") as temporary:
        temp = Path(temporary)
        rebuilt_manifest = temp / "manifest.json"
        invoke(BUILDER, "--output-dir", temp / "data", "--manifest", rebuilt_manifest)
        regenerated = json.loads(rebuilt_manifest.read_text(encoding="utf-8"))
        assert regenerated == fixed
        for item in fixed["fixtures"]:
            path = temp / item["path"]
            if item["byte_deterministic"]:
                assert hash_file(path) == item["sha256"]
            else:
                assert path.is_file()
            assert readback(path, item["media_type"]) == item["expected"]


def test_xlsx_rebuild_across_wall_clock_seconds_is_byte_identical():
    fixed = next(item for item in json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]
                 if item["path"].endswith(".xlsx"))
    with tempfile.TemporaryDirectory(prefix="workbench-clock-a-") as first, \
         tempfile.TemporaryDirectory(prefix="workbench-clock-b-") as second:
        for location in (first, second):
            root = Path(location)
            invoke(BUILDER, "--output-dir", root / "data", "--manifest", root / "manifest.json")
            if location == first:
                time.sleep(2.1)
        assert hash_file(Path(first) / fixed["path"]) == hash_file(Path(second) / fixed["path"])
        assert hash_file(Path(second) / fixed["path"]) == fixed["sha256"]


def test_long_id_duplicate_key_formula_cache_and_null_distinction():
    items = {item["media_type"]: item for item in json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]}
    xlsx = items["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]["expected"]
    csv = items["text/csv"]["expected"]
    parquet = items["application/vnd.apache.parquet"]["expected"]
    assert len(xlsx["rows"][0][1]) == 30 and xlsx["rows"][0][1].startswith("000")
    assert xlsx["formula_cell"] == "=SUM(1,2)" and xlsx["cached_value"] is None
    assert [row["match_key"] for row in csv["rows"]].count("dup") == 2
    assert csv["rows"][0]["nullable"] == "\\N" and csv["rows"][1]["nullable"] == ""
    assert parquet["rows"][0]["nullable"] is None and parquet["rows"][1]["nullable"] == ""
    assert parquet["amount_type"] == "decimal128(20, 6)"

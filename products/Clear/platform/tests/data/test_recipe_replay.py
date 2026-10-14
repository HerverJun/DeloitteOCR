"""Durable replay, branching and loss-aware transformation checks."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from modules.data_workbench.importing import import_csv
from modules.data_workbench.recipes import RecipeError, RecipeStore, Step


def fixture(tmp_path: Path):
    source = tmp_path / "input.csv"
    source.write_text('name,amount,day,flag\n" Alice ",10.00,2026-09-26,true\n'
                      'Alice,bad,2026-09-27,false\nBob,,invalid,0\nBob,0,2026-09-28,1\n',
                      encoding="utf-8")
    table = import_csv(source, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    store = RecipeStore(tmp_path / "recipes.sqlite3")
    return store, store.import_table(table), table


def test_fixed_steps_replay_and_stepwise_preview(tmp_path: Path):
    store, root, table = fixture(tmp_path)
    trimmed = store.apply(root, Step("trim", {"column": "c1"}))
    mapped = store.apply(trimmed, Step("map", {"column": "c1", "values": {"Alice": "A"}}))
    renamed = store.apply(mapped, Step("rename", {"column": "c2", "header": "金额"}))
    cast = store.apply(renamed, Step("cast", {"column": "c2", "target": "decimal"}))
    preview = store.preview(cast, limit=2)
    assert preview["columns"][1]["header"] == "金额"
    assert preview["total_rows"] == 4
    assert preview["rows"][0]["cells"][0]["value"] == "A"
    assert preview["rows"][0]["cells"][0]["raw"] == " Alice "
    assert preview["rows"][0]["cells"][1]["kind"] == "decimal"
    assert preview["rows"][0]["cells"][1]["value"] == "10.00"
    assert preview["event"]["input_revision"] == renamed
    assert preview["event"]["output_revision"] == cast
    assert preview["event"]["input_rows"] == preview["event"]["output_rows"] == 4
    assert preview["event"]["errors_total"] == 1
    assert preview["event"]["error_sample"][0]["raw"] == "bad"
    assert store.get(cast)["snapshot"]["rows"][1]["cells"][1]["value"] == "bad"
    assert store.get(cast)["snapshot"]["rows"][2]["cells"][1]["kind"] == "null"
    assert store.get(cast)["snapshot"]["rows"][3]["cells"][1]["value"] == "0"
    assert store.get(root)["snapshot"]["rows"][0]["cells"][0]["value"] == " Alice "
    assert store.lineage(cast) == (root, trimmed, mapped, renamed, cast)
    reopened = RecipeStore(tmp_path / "recipes.sqlite3")
    assert reopened.apply(renamed, Step("cast", {"target": "decimal", "column": "c2"})) == cast
    assert reopened.get(cast) == store.get(cast)
    assert store.import_table(table) == root


def test_undo_is_branching_and_keeps_old_results(tmp_path: Path):
    store, root, _ = fixture(tmp_path)
    first = store.apply(root, Step("trim", {"column": "c1"}))
    branch_a = store.apply(first, Step("filter", {"column": "c1", "predicate": "equals", "value": "Alice"}))
    assert store.preview(branch_a)["total_rows"] == 2
    assert len(store.get(branch_a)["event"]["excluded_row_ids"]) == 2
    back = store.undo(branch_a)
    assert back == first
    branch_b = store.apply(back, Step("filter", {"column": "c1", "predicate": "equals", "value": "Bob"}))
    assert branch_a != branch_b
    assert store.preview(branch_b)["total_rows"] == 2
    assert store.preview(branch_a)["total_rows"] == 2
    assert store.get(branch_a)["parent_revision"] == store.get(branch_b)["parent_revision"] == first


def test_duplicates_are_marked_without_multiplying_rows(tmp_path: Path):
    store, root, _ = fixture(tmp_path)
    trimmed = store.apply(root, Step("trim", {"column": "c1"}))
    duplicated = store.apply(trimmed, Step("duplicates", {"columns": ["c1"], "include_null": False}))
    event = store.get(duplicated)["event"]
    assert event["input_rows"] == event["output_rows"] == 4
    assert len(event["duplicate_groups"]) == 2
    assert sorted(len(group) for group in event["duplicate_groups"]) == [2, 2]
    assert len({row_id for group in event["duplicate_groups"] for row_id in group}) == 4


def test_failed_cast_retains_literal_and_invalid_steps_fail_closed(tmp_path: Path):
    store, root, _ = fixture(tmp_path)
    cast = store.apply(root, Step("cast", {"column": "c3", "target": "date"}))
    state = store.get(cast)
    assert state["event"]["errors_total"] == 1
    assert state["snapshot"]["rows"][2]["cells"][2]["raw"] == "invalid"
    assert state["snapshot"]["rows"][2]["cells"][2]["kind"] == "text"
    for step in (Step("sql", {"query": "DELETE FROM source"}),
                 Step("cast", {"column": "c9", "target": "integer"}),
                 Step("filter", {"column": "c1", "predicate": "equals"}),
                 Step("map", {"column": "c1", "values": {}})):
        with pytest.raises(RecipeError):
            store.apply(root, step)
    with pytest.raises(RecipeError):
        store.undo(root)


def test_explicit_boolean_integer_and_null_filter(tmp_path: Path):
    store, root, _ = fixture(tmp_path)
    amounts = store.apply(root, Step("cast", {"column": "c2", "target": "integer"}))
    assert store.get(amounts)["event"]["errors_total"] == 1
    assert store.get(amounts)["snapshot"]["rows"][0]["cells"][1]["value"] == "10"
    flags = store.apply(amounts, Step("cast", {"column": "c4", "target": "boolean"}))
    assert store.get(flags)["event"]["errors_total"] == 0
    assert store.get(flags)["snapshot"]["rows"][0]["cells"][3]["value"] is True
    missing = store.apply(flags, Step("filter", {"column": "c2", "predicate": "is_null"}))
    assert store.preview(missing)["total_rows"] == 1
    assert store.preview(missing)["rows"][0]["cells"][1]["kind"] == "null"


def test_snapshot_tamper_is_detected(tmp_path: Path):
    store, root, _ = fixture(tmp_path)
    child = store.apply(root, Step("trim", {"column": "c1"}))
    with sqlite3.connect(tmp_path / "recipes.sqlite3") as db:
        db.execute("UPDATE recipe_revisions SET snapshot_json='{}' WHERE revision_id=?", (child,))
    with pytest.raises(RecipeError, match="corrupt"):
        store.get(child)

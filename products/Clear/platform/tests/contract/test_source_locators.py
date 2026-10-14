"""Ordered asset-set snapshots and precise source locations."""

import sqlite3

import pytest
from pydantic import ValidationError

from packages.contracts.models import AssetSetItem, SourceLocator
from workbench_platform.assets import AssetRegistry
from workbench_platform.assets.sets import AssetSetConflict, AssetSetMissing, AssetSetStore


@pytest.fixture
def stores(tmp_path):
    inputs, vault = tmp_path / "inputs", tmp_path / "vault"
    inputs.mkdir()
    vault.mkdir()
    registry = AssetRegistry(tmp_path / "assets.sqlite", vault, {"inputs": inputs})
    return inputs, registry, AssetSetStore(registry)


def asset(registry, workspace, name, content):
    root = registry.roots["inputs"]
    (root / name).write_bytes(content)
    return registry.register(workspace, "inputs", name, media_type="application/pdf",
                             storage_mode="referenced")


def item(record, item_id, *, kind="file", precision="unknown", **location):
    locator = SourceLocator(schema_version="1.0", kind=kind,
                            asset_id=record.asset_id, asset_revision=record.revision,
                            blob_hash=record.blob_hash, precision=precision, **location)
    return AssetSetItem(schema_version="1.0", item_id=item_id,
                        asset_id=record.asset_id, asset_revision=record.revision,
                        blob_hash=record.blob_hash, source_locator=locator)


def test_order_revision_and_old_input_identity_are_immutable(stores):
    inputs, registry, sets = stores
    a = asset(registry, "workspaceA", "a.pdf", b"first")
    b = asset(registry, "workspaceA", "b.pdf", b"second")
    first = sets.create("workspaceA", [item(a, "itemA"), item(b, "itemB")])
    assert [entry.item_id for entry in first.items] == ["itemA", "itemB"]
    assert sets.get("workspaceA", first.asset_set_id, 1) == first

    second = sets.revise("workspaceA", first.asset_set_id, 1,
                         [item(b, "itemB"), item(a, "itemA")])
    assert second.revision == 2 and second.manifest_hash != first.manifest_hash
    assert [entry.item_id for entry in sets.get("workspaceA", first.asset_set_id, 1).items] == [
        "itemA", "itemB"]
    assert [entry.item_id for entry in sets.get("workspaceA", first.asset_set_id, 2).items] == [
        "itemB", "itemA"]
    with pytest.raises(AssetSetConflict):
        sets.revise("workspaceA", first.asset_set_id, 1, [item(a, "itemA")])
    with pytest.raises(AssetSetMissing):
        sets.get("workspaceB", first.asset_set_id, 1)

    # A later source edit changes the asset revision. It cannot rewrite either
    # frozen set, and the old source cannot be submitted as a new set input.
    (inputs / "a.pdf").write_bytes(b"changed")
    updated = registry.register("workspaceA", "inputs", "a.pdf",
                                media_type="application/pdf", storage_mode="referenced")
    assert updated.revision == 2
    assert sets.get("workspaceA", first.asset_set_id, 1).items[0].blob_hash == a.blob_hash
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [item(a, "stale")])


def test_page_table_row_cell_and_region_basis_round_trip(stores):
    _, registry, sets = stores
    record = asset(registry, "workspaceA", "document.pdf", b"document")
    members = [
        item(record, "pageA", kind="page", precision="page_only", page_number=2),
        item(record, "tableA", kind="table", precision="page_only",
             page_number=2, table_number=1),
        item(record, "rowA", kind="row", precision="page_only",
             page_number=2, table_number=1, row_number=7),
        item(record, "cellA", kind="cell", precision="page_only",
             page_number=2, table_number=1, row_number=7, column_number=3),
        item(record, "regionA", kind="region", precision="approximate",
             page_number=2, image_version_id="imageVersionA", coordinate_space="normalized",
             transform_revision=4, box=(0.1, 0.2, 0.4, 0.5)),
    ]
    frozen = sets.create("workspaceA", members)
    loaded = sets.get("workspaceA", frozen.asset_set_id, frozen.revision)
    assert loaded == frozen
    assert loaded.items[3].source_locator.row_number == 7
    assert loaded.items[3].source_locator.column_number == 3
    assert loaded.items[4].source_locator.precision == "approximate"
    assert loaded.items[4].source_locator.box == (0.1, 0.2, 0.4, 0.5)
    with pytest.raises(ValidationError):
        item(record, "fakeExact", kind="region", precision="exact", page_number=2)
    with pytest.raises(ValidationError):
        item(record, "badRow", kind="row", precision="page_only", page_number=2,
             table_number=1, row_number=0)
    with pytest.raises(ValidationError):
        item(record, "badSpace", kind="region", precision="approximate", page_number=2,
             image_version_id="imageVersionA", coordinate_space="normalized",
             transform_revision=4, box=(0.1, 0.2, 1.1, 0.5))


def test_derivation_references_exact_parent_snapshot(stores):
    _, registry, sets = stores
    source = asset(registry, "workspaceA", "source.pdf", b"source")
    derived = asset(registry, "workspaceA", "derived.pdf", b"derived")
    parent = sets.create("workspaceA", [item(source, "sourceItem")])
    child = sets.create("workspaceA", [item(derived, "derivedItem")],
                        parents={"derivedItem": (parent.asset_set_id, 1, "sourceItem")})
    assert sets.lineage("workspaceA", child.asset_set_id, child.revision) == {
        "derivedItem": (parent.asset_set_id, 1, "sourceItem")}
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [item(derived, "bad")],
                    parents={"bad": (parent.asset_set_id, 1, "missing")})
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceB", [item(source, "bad")])
    with sqlite3.connect(registry.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE asset_set_members SET payload='rewritten' "
                       "WHERE asset_set_id=?", (parent.asset_set_id,))


def test_invalid_member_or_duplicate_cannot_create_set(stores):
    _, registry, sets = stores
    record = asset(registry, "workspaceA", "source.pdf", b"source")
    valid = item(record, "itemA")
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [valid, valid])
    altered = valid.model_dump()
    altered["blob_hash"] = "b" * 64
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [altered])
    with pytest.raises(AssetSetConflict):
        sets.create("workspaceA", [])


def test_manifest_read_detects_tampered_member_even_if_database_trigger_is_removed(stores):
    _, registry, sets = stores
    record = asset(registry, "workspaceA", "source.pdf", b"source")
    frozen = sets.create("workspaceA", [item(record, "itemA")])
    with sqlite3.connect(registry.db_path) as db:
        db.execute("DROP TRIGGER asset_set_members_no_update")
        db.execute("UPDATE asset_set_members SET parent_set_id='parentA', "
                   "parent_revision=1,parent_item_id='itemB' WHERE asset_set_id=?",
                   (frozen.asset_set_id,))
    with pytest.raises(AssetSetConflict):
        sets.get("workspaceA", frozen.asset_set_id, 1)

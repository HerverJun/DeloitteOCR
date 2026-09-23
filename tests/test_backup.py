import csv
import json

from PIL import Image
import pytest

from image_platform.backup import backup, digest, restore, verify
from image_platform.datasets import load_dataset, register_asset
from image_platform.settings import Settings
from image_platform.store import dump, now


def test_backup_restores_external_images_snapshots_models_and_wal(tmp_path, store):
    external = tmp_path / "external-source" / "rose.png"
    external.parent.mkdir()
    Image.new("RGB", (32, 24), "red").save(external)
    owned = store.settings.data_dir / "uploads" / "owned.png"
    Image.new("RGB", (32, 24), "blue").save(owned)
    external_asset, _ = register_asset(store, "p", external, "a")
    owned_asset, _ = register_asset(store, "p", owned, "b")
    same_bytes = tmp_path / "another-place" / "same.png"
    same_bytes.parent.mkdir()
    same_bytes.write_bytes(external.read_bytes())
    duplicate_asset, _ = register_asset(store, "p", same_bytes, "a")
    snapshots = store.settings.data_dir / "datasets"
    snapshot = snapshots / "sample.json"
    items = [dict(asset, split="train") for asset in (external_asset, owned_asset, duplicate_asset)]
    snapshot.write_text(dump({"items": items}), encoding="utf-8")
    with (snapshots / "sample.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["path", "label_id"])
        writer.writeheader()
        for item in items:
            writer.writerow({"path": item["path"], "label_id": item["label_id"]})
    model = store.settings.data_dir / "models" / "model_1"
    model.mkdir()
    (model / "model.onnx").write_bytes(b"dummy model package for path relocation")
    with store.connect(write=True) as connection:
        connection.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?)", (
            "dataset_1", "p", "first", dump([]), str(snapshot), digest(snapshot), dump({"total": 2}), now()))
        connection.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?)", (
            "model_1", "p", "job_old", "first", str(model), dump({}), dump({}), now()))
    destination = tmp_path / "safe-copy"
    metadata = backup(store.settings.data_dir, destination)
    assert len(metadata["external_paths"]) == 2
    assert verify(destination)["files"] == metadata["files"]
    recovered = tmp_path / "recovered"
    restore(destination, recovered)
    restored = Settings(recovered)
    from image_platform.store import Store
    restored_store = Store(restored)
    assets = restored_store.rows("SELECT * FROM assets ORDER BY id")
    assert {row["id"] for row in assets} == {external_asset["id"], owned_asset["id"], duplicate_asset["id"]}
    assert len({row["path"] for row in assets}) == 3
    for row in assets:
        assert row["path"].startswith(str(recovered))
        assert digest(row["path"]) == row["sha256"]
    _, frozen = load_dataset(restored_store, "dataset_1")
    assert all(item["path"].startswith(str(recovered)) for item in frozen["items"])
    assert (recovered / "models" / "model_1" / "model.onnx").is_file()
    assert json.loads(snapshot.read_text(encoding="utf-8"))["items"][0]["path"] == str(external)
    assert digest(external) == metadata["files"][metadata["external_paths"][str(external)]]["sha256"]


def test_tampering_blocks_restore_and_existing_target_is_never_overwritten(tmp_path, store):
    destination = tmp_path / "copy"
    backup(store.settings.data_dir, destination)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    sentinel = occupied / "keep.txt"
    sentinel.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError, match="new directory"):
        restore(destination, occupied)
    assert sentinel.read_text(encoding="utf-8") == "original"
    with (destination / "data" / "platform.sqlite3").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="integrity failure"):
        restore(destination, tmp_path / "untouched")
    assert not (tmp_path / "untouched").exists()

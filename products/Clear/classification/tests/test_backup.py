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


def test_backup_preserves_disposition_receipt_history_and_request_deduplication(tmp_path, store):
    source = store.settings.data_dir / "uploads" / "receipt.png"
    Image.new("RGB", (16, 16), "red").save(source)
    asset, _ = register_asset(store, "p", source, None)
    job_id = store.submit("p", "inference", {"model_id": "frozen_model"}, [asset])
    prediction = {"label_id": "a", "score": 0.8, "status": "needs_review"}
    with store.connect(write=True) as con:
        con.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (job_id,))
        con.execute("UPDATE job_items SET state='succeeded',prediction=? WHERE job_id=?", (dump(prediction), job_id))
    item_id = store.one("SELECT id FROM job_items WHERE job_id=?", (job_id,))["id"]
    arguments = {"operation_id": "operation_backup", "request_id": "request_backup",
                 "actor": {"id": "localReviewer", "display_name": "本机复核员", "source": "local"},
                 "instance_id": "instance_backup", "expected_revisions": {item_id: 0}}
    receipt = store.dispose_results(job_id, [item_id], "confirmed", "b", "source checked", **arguments)
    destination, restored_root = tmp_path / "copy", tmp_path / "restored"
    backup(store.settings.data_dir, destination)
    restore(destination, restored_root)
    from image_platform.store import Store
    restored = Store(Settings(restored_root))
    assert restored.disposition_operation("p", "operation_backup") == receipt
    assert restored.dispose_results(job_id, [item_id], "confirmed", "b", "source checked", **arguments) == receipt
    assert restored.one("SELECT COUNT(*) count FROM result_disposition_events")["count"] == 1
    assert restored.one("SELECT label_id FROM assets WHERE id=?", (asset["id"],))["label_id"] == "b"
    from fastapi.testclient import TestClient
    from image_platform.api import create_app
    with TestClient(create_app(Settings(restored_root)), base_url="http://127.0.0.1:8765") as reopened:
        current_instance = reopened.get("/api/v1/health").json()["instance_id"]
        readback = reopened.get("/api/v1/projects/p/disposition-operations/operation_backup", params={
            "instance_id": current_instance, "operation_instance_id": "instance_backup"})
        assert readback.status_code == 200 and readback.json() == receipt


def test_v4_migration_keeps_legacy_history_without_inventing_actor(tmp_path):
    import sqlite3
    from image_platform.store import Store, MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4
    settings = Settings(tmp_path / "v4")
    settings.prepare()
    with sqlite3.connect(settings.database) as con:
        con.executescript(MIGRATION_1 + MIGRATION_2 + MIGRATION_3 + MIGRATION_4)
        con.execute("INSERT INTO projects(id,name,labels,created_at) VALUES('legacy','旧项目','[]',?)", (now(),))
        con.execute("INSERT INTO jobs(id,project_id,kind,state,params,created_at,updated_at) "
                    "VALUES('oldJob','legacy','inference','succeeded','{}',?,?)", (now(), now()))
        con.execute("INSERT INTO job_items(id,job_id,path,sha256) VALUES('oldItem','oldJob','old-path',?)", ("a" * 64,))
        con.execute("INSERT INTO result_disposition_events VALUES('oldEvent','oldJob','oldItem','deferred',NULL,'旧说明',1,?)", (now(),))
    upgraded = Store(settings)
    with upgraded.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 5
        assert {row[1] for row in con.execute("PRAGMA table_info(result_disposition_events)")} >= {
            "operation_id", "request_id", "actor_json"}
    assert upgraded.rows("SELECT * FROM result_disposition_requests") == []
    legacy = upgraded.one("SELECT * FROM result_disposition_events WHERE id='oldEvent'")
    assert legacy["note"] == "旧说明" and legacy["actor_json"] is None and legacy["operation_id"] is None

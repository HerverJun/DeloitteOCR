"""A stopped platform workspace reopens from a verified new-directory restore."""

import hashlib
import io
import sqlite3

import pytest
from fastapi.testclient import TestClient

from packages.contracts.models import Counts, JobRef
from scripts.backup_workspace import create, restore, verify
from workbench_platform.app import app

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_platform_backup_restores_assets_and_managed_result(tmp_path, monkeypatch):
    source, archive, recovered = (tmp_path / name for name in ("source", "archive", "recovered"))
    source.mkdir()
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(source))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "恢复检查"}).json()["workspace_id"]
        base = f"/api/platform/v1/workspaces/{workspace}/assets"
        asset = client.post(base + "/uploads?name=来源.txt", headers=LOCAL,
                            content="备份前原件".encode()).json()
        fixed = client.post(base + "/sets", headers=LOCAL,
                            json={"asset_ids": [asset["asset_id"]]}).json()
        payload = b"immutable-result"
        job = JobRef(schema_version="1.0", platform_job_id="jobA", app_id="ocr",
                     instance_id="instanceA", native_job_id="nativeA",
                     operation_id="operationA", input_manifest_hash="a" * 64,
                     native_status="succeeded", status="succeeded", stage="complete",
                     counts=Counts(schema_version="1.0", total=1, succeeded=1,
                                   failed=0, cancelled=0, pending=0), capabilities=[],
                     last_event_seq=1, updated_at="2026-09-28T00:00:00Z")
        locator = {"schema_version": "1.0", "kind": "file", "asset_id": asset["asset_id"],
                   "asset_revision": 1, "blob_hash": asset["blob_hash"], "precision": "exact"}
        artifact = app.state.artifact_center.store.publish_managed(
            workspace, io.BytesIO(payload), expected_hash=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload), job_ref=job, source_refs=[locator],
            media_type="text/plain", application_version="0.2.0",
            rule_or_model_version="fixture")
    manifest = create(source, archive)
    assert "assets.sqlite3" in manifest["files"]
    assert "artifact-center/artifacts.sqlite3" in manifest["files"]
    assert "ocr-handoffs.sqlite3" in manifest["files"]
    assert verify(archive)["files"] == manifest["files"]
    restore(archive, recovered)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(recovered))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        base = f"/api/platform/v1/workspaces/{workspace}/assets"
        assert client.get(base + "/sets/" + fixed["asset_set_id"]).json()["revision"] == 1
        original = client.get(base + "/" + asset["asset_id"] + "/content?revision=1")
        assert original.status_code == 200 and original.content == "备份前原件".encode()
        stored = app.state.artifact_center.store.get(workspace, artifact.artifact_id)
        with app.state.artifact_center.store.stage_download(
                workspace, stored.artifact_id, expected_revision=stored.revision,
                expected_hash=stored.blob_hash) as path:
            assert path.read_bytes() == payload
    with pytest.raises(ValueError, match="new directory"):
        restore(archive, recovered)
    corrupt = next((archive / "asset-blobs").rglob("*"))
    if corrupt.is_dir():
        corrupt = next(corrupt.rglob("*"))
    corrupt.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="changed"):
        verify(archive)


def test_ocr_cold_backup_keeps_project_files_and_omits_runtime_state(tmp_path):
    source, archive, recovered = (tmp_path / name for name in ("ocr", "backup", "recovered"))
    (source / "projects" / "projectA").mkdir(parents=True)
    (source / "projects" / "projectA" / "source.pdf").write_bytes(b"pdf-source")
    (source / "pdf-ipc").mkdir()
    (source / "pdf-ipc" / "request.json").write_text("temporary", encoding="utf-8")
    (source / "service.lock").write_bytes(b"0")
    with sqlite3.connect(source / "workbench.sqlite3") as db:
        db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO projects VALUES('projectA', 'Original')")
    manifest = create(source, archive, kind="ocr")
    assert manifest["kind"] == "ocr"
    assert "projects/projectA/source.pdf" in manifest["files"]
    assert "service.lock" not in manifest["files"]
    assert "pdf-ipc/request.json" not in manifest["files"]
    assert verify(archive)["kind"] == "ocr"
    restore(archive, recovered)
    with sqlite3.connect(recovered / "workbench.sqlite3") as db:
        assert db.execute("SELECT name FROM projects WHERE id='projectA'").fetchone()[0] == "Original"
    assert (recovered / "projects/projectA/source.pdf").read_bytes() == b"pdf-source"
    assert not (recovered / "service.lock").exists()


def test_platform_cold_backup_keeps_batch_templates_receipts_and_decisions(tmp_path, monkeypatch):
    from workbench_platform.batches.store import BatchStore

    source, archive, recovered = (tmp_path / name for name in ("batch-source", "batch-archive", "batch-recovered"))
    source.mkdir()
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(source))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        assert client.get("/api/platform/v1/health").status_code == 200
    store = BatchStore(source / "batches")
    template = store.save_template({"schema_version": "1.0", "workspace_id": "workspaceA",
                                   "name": "月度核对", "scenario": "data_compare",
                                   "config": {"policy": "fixed-original"}})
    request = {"schema_version": "1.0", "request_key": "requestA", "workspace_id": "workspaceA",
               "template_id": template["template_id"], "template_version": template["version"],
               "inputs": [{"role": "left", "asset_id": "assetA", "asset_revision": 1, "blob_hash": "a" * 64}]}
    batch, _ = store.accept(request, template)
    store.update(batch["batch_id"], status="succeeded", receipts={"export": {"job_id": "fixedExportA"}},
                 issues=[{"item_id": "itemA", "raw_output": {"identifier": "00000000000000000001"}, "disposition": "pending"}])
    store.disposition(batch["batch_id"], ["itemA"], "defer", "仍需业务解释")
    manifest = create(source, archive)
    assert "batches/batches.sqlite3" in manifest["files"]
    restore(archive, recovered)
    reopened = BatchStore(recovered / "batches")
    assert reopened.template("workspaceA", template["template_id"], 1) == template
    persisted = reopened.get(batch["batch_id"])
    assert persisted["receipts"] == {"export": {"job_id": "fixedExportA"}}
    assert persisted["issues"][0]["raw_output"]["identifier"] == "00000000000000000001"
    assert persisted["issues"][0]["disposition"] == "deferred"
    assert persisted["issues"][0]["note"] == "仍需业务解释"
    with sqlite3.connect(reopened.path) as db:
        assert db.execute("SELECT action,note FROM disposition_events").fetchall() == [("defer", "仍需业务解释")]

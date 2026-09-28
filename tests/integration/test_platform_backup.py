"""A stopped platform workspace reopens from a verified new-directory restore."""

import hashlib
import io

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

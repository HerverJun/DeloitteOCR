"""A native task cancelled while queued has no execution stage to report."""

import json

import pytest
from fastapi.testclient import TestClient

from workbench_platform.app import app

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


@pytest.mark.parametrize("scenario", ["classification", "ocr"])
def test_cancelled_empty_native_stage_collects_without_changing_snapshot(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "取消证据"}).json()["workspace_id"]
        source = client.post(f"/api/platform/v1/workspaces/{workspace}/assets/uploads?name=image.png",
                             headers=LOCAL, content=b"fixture-bytes").json()
        inputs = [{"role": "image", "name": "image.png", "asset_id": source["asset_id"],
                   "revision": 1, "blob_hash": source["blob_hash"], "source_locator": {
                       "schema_version": "1.0", "kind": "file", "asset_id": source["asset_id"],
                       "asset_revision": 1, "blob_hash": source["blob_hash"], "precision": "exact"}}]
        projection = {"instance_id": "instanceA", "operation_id": "operationA",
                      "input_manifest_hash": "a" * 64, "native_status": "cancelled",
                      "status": "cancelled", "stage": "", "counts": {
                          "total": 1, "succeeded": 0, "failed": 0, "cancelled": 1, "pending": 0},
                      "last_event_seq": 3, "updated_at": "2026-10-02T10:05:22Z"}
        snapshot = {"workspace_id": workspace, "handoff_id": "handoffA", "project_id": "projectA",
                    "project_name": "取消证据", "platform_job_id": "platformJobA",
                    "input_set_id": "setA", "input_set_revision": 1, "input_manifest_hash": "b" * 64,
                    "inputs": inputs, "items": [], "platform_job": projection}
        if scenario == "classification":
            snapshot.update(native_job_id="nativeJobA", model_id="modelA", native_state="cancelled",
                            items=[{"native_item_id": "nativeItemA", "asset_id": source["asset_id"],
                                    "state": "cancelled", "prediction_label_id": None,
                                    "prediction_status": None, "prediction_score": None}])
            filename = "classification-results.json"
        else:
            snapshot.update(native_task_ids=["taskA"], engines=["ppocr"], media_type="application/json",
                            export=json.dumps({"status": "cancelled", "results": []}).encode())
            filename = "ocr-result.json"
        frozen_projection = json.dumps(projection, sort_keys=True)
        handoff = getattr(app.state, scenario + "_handoff")
        monkeypatch.setattr(handoff, "result_snapshot", lambda *_: snapshot)
        base = f"/api/platform/v1/workspaces/{workspace}/artifacts"
        published = client.post(base + f"/{scenario}-exports", headers=LOCAL, json={"handoff_id": "handoffA"})
        assert published.status_code == 201, published.text
        bundle = published.json()
        artifact = getattr(app.state.artifact_center, scenario).file(workspace, bundle["bundle_id"], filename)
        assert artifact.job_ref.status == artifact.job_ref.native_status == "cancelled"
        assert artifact.job_ref.stage == "cancelled"
        assert artifact.job_ref.counts.cancelled == 1 and artifact.job_ref.counts.succeeded == 0
        assert json.dumps(projection, sort_keys=True) == frozen_projection
        downloaded = client.get(base + f"/{bundle['bundle_id']}/files/{filename}")
        assert downloaded.status_code == 200
        if scenario == "classification":
            exported = downloaded.json()
            assert exported["native_state"] == "cancelled"
            assert exported["items"][0]["prediction_label_id"] is None
        replay = client.post(base + f"/{scenario}-exports", headers=LOCAL, json={"handoff_id": "handoffA"})
        assert replay.json()["bundle_id"] == bundle["bundle_id"]

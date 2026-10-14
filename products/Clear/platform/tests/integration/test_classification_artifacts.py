"""Classification snapshots preserve prediction, human label and old versions."""

import json

from fastapi.testclient import TestClient

from workbench_platform.app import app

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_result_snapshot_replay_revision_download_and_workspace_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "甲"}).json()["workspace_id"]
        other = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                            json={"schema_version": "1.0", "name": "乙"}).json()["workspace_id"]
        source = client.post(f"/api/platform/v1/workspaces/{workspace}/assets/uploads?name=票据.png",
                             headers=LOCAL, content=b"fixture-bytes").json()
        locator = {"schema_version": "1.0", "kind": "file", "asset_id": source["asset_id"],
                   "asset_revision": 1, "blob_hash": source["blob_hash"], "precision": "exact"}
        inputs = [{"role": "image", "name": "票据.png", "asset_id": source["asset_id"],
                   "revision": 1, "blob_hash": source["blob_hash"], "source_locator": locator}]
        item = {"native_item_id": "nativeItemA", "native_asset_id": "nativeAssetA",
                "asset_id": source["asset_id"], "source_name": "票据.png", "state": "succeeded",
                "prediction_label_id": "class_1", "prediction_status": "needs_review",
                "prediction_score": 0.7, "human_label_id": "class_2", "error": None,
                "disposition_state": "confirmed", "disposition_revision": 1, "disposition_note": "checked"}
        projection = {"instance_id": "instanceA", "operation_id": "operationA",
                      "input_manifest_hash": "a" * 64, "native_status": "succeeded",
                      "status": "succeeded", "stage": "分类完成", "counts": {
                          "total": 1, "succeeded": 1, "failed": 0, "cancelled": 0, "pending": 0},
                      "last_event_seq": 3, "updated_at": "2026-09-28T00:00:00Z"}
        snapshot = {"workspace_id": workspace, "handoff_id": "handoffA",
                    "project_id": "projectA", "project_name": "分类项目",
                    "platform_job_id": "platformJobA", "native_job_id": "nativeJobA",
                    "model_id": "modelA", "input_set_id": "setA", "input_set_revision": 1,
                    "input_manifest_hash": "b" * 64, "inputs": inputs, "items": [item],
                    "native_state": "succeeded", "platform_job": projection}
        monkeypatch.setattr(app.state.classification_handoff, "result_snapshot",
                            lambda space, _handoff: snapshot if space == workspace else None)
        base = f"/api/platform/v1/workspaces/{workspace}/artifacts"
        payload = {"handoff_id": "handoffA"}
        assert client.post(base + "/classification-exports", json=payload).status_code == 403
        created = client.post(base + "/classification-exports", headers=LOCAL, json=payload)
        assert created.status_code == 201, created.text
        first = created.json()
        assert first["kind"] == "classification_result" and len(first["files"]) == 3
        replay = client.post(base + "/classification-exports", headers=LOCAL, json=payload)
        assert replay.status_code == 201 and replay.json()["bundle_id"] == first["bundle_id"]
        path = base + f"/{first['bundle_id']}/files/classification-results.json"
        old = client.get(path)
        result = json.loads(old.content)
        assert result["items"][0]["prediction_label_id"] == "class_1"
        assert result["items"][0]["human_label_id"] == "class_2"
        assert result["items"][0]["effective_label_id"] == "class_2"
        assert result["items"][0]["disposition_revision"] == 1
        assert b"native-data" not in old.content
        csv_file = client.get(base + f"/{first['bundle_id']}/files/classification-results.csv")
        assert csv_file.status_code == 200 and csv_file.content.startswith(b"\xef\xbb\xbf")
        provenance = client.get(base + f"/{first['bundle_id']}/files/provenance.json").json()
        assert provenance["instance_id"] == "instanceA"
        assert provenance["result_revisions"] == [{"native_item_id": "nativeItemA", "disposition_revision": 1}]
        assert client.get(f"/api/platform/v1/workspaces/{other}/artifacts/{first['bundle_id']}").status_code == 404

        item["human_label_id"] = "class_1"
        item["disposition_revision"] = 2
        changed = client.post(base + "/classification-exports", headers=LOCAL, json=payload)
        assert changed.status_code == 201 and changed.json()["bundle_id"] != first["bundle_id"]
        assert client.get(path).content == old.content
        listing = client.get(base).json()
        assert listing["total"] == 2
        assert {entry["bundle_id"] for entry in listing["items"]} == {
            first["bundle_id"], changed.json()["bundle_id"]}

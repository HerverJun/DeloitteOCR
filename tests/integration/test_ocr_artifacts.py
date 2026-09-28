"""OCR result bundles keep edited versions and verified source links."""

import json

from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.jobs.native_ocr import OcrResultConflict

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_ocr_bundle_replay_revision_and_workspace_scope(tmp_path, monkeypatch):
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
        item = {"native_result_id": "resultA", "native_task_id": "taskA",
                "native_asset_id": "nativeAssetA", "native_version_id": "versionA",
                "asset_id": source["asset_id"], "revision": 0, "selected": True}
        projection = {"instance_id": "instanceA", "operation_id": "operationA",
                      "input_manifest_hash": "a" * 64, "native_status": "succeeded",
                      "status": "succeeded", "stage": "OCR completed",
                      "counts": {"total": 1, "succeeded": 1, "failed": 0, "cancelled": 0, "pending": 0},
                      "last_event_seq": 3, "updated_at": "2026-09-28T00:00:00Z"}
        export = {"id": "resultA", "revision": 0, "edited": {"text": "原始文字", "tables": []}}
        snapshot = {"workspace_id": workspace, "handoff_id": "handoffA",
                    "project_id": "projectA", "project_name": "OCR 项目",
                    "platform_job_id": "platformJobA", "native_task_ids": ["taskA"],
                    "engines": ["ppocr"], "input_set_id": "setA", "input_set_revision": 1,
                    "input_manifest_hash": "b" * 64, "inputs": inputs, "items": [item],
                    "platform_job": projection, "media_type": "application/json"}
        snapshot["export"] = json.dumps(export, ensure_ascii=False).encode()
        monkeypatch.setattr(app.state.ocr_handoff, "result_snapshot",
                            lambda space, _handoff: snapshot if space == workspace else None)
        base = f"/api/platform/v1/workspaces/{workspace}/artifacts"
        payload = {"handoff_id": "handoffA"}
        assert client.post(base + "/ocr-exports", json=payload).status_code == 403
        created = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert created.status_code == 201, created.text
        first = created.json()
        assert first["kind"] == "ocr_result" and len(first["files"]) == 2
        replay = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert replay.status_code == 201 and replay.json()["bundle_id"] == first["bundle_id"]
        def stale(*_args):
            raise OcrResultConflict()
        monkeypatch.setattr(app.state.ocr_handoff, "result_snapshot", stale)
        assert client.post(base + "/ocr-exports", headers=LOCAL, json=payload).status_code == 409
        monkeypatch.setattr(app.state.ocr_handoff, "result_snapshot",
                            lambda space, _handoff: snapshot if space == workspace else None)
        path = base + f"/{first['bundle_id']}/files/ocr-result.json"
        old = client.get(path)
        assert old.status_code == 200 and json.loads(old.content)["edited"]["text"] == "原始文字"
        provenance = client.get(base + f"/{first['bundle_id']}/files/provenance.json").json()
        assert provenance["results"][0]["asset_id"] == source["asset_id"]
        assert client.get(f"/api/platform/v1/workspaces/{other}/artifacts/{first['bundle_id']}").status_code == 404
        export["revision"] = item["revision"] = 1
        export["edited"]["text"] = "人工修订"
        snapshot["export"] = json.dumps(export, ensure_ascii=False).encode()
        changed = client.post(base + "/ocr-exports", headers=LOCAL, json=payload)
        assert changed.status_code == 201 and changed.json()["bundle_id"] != first["bundle_id"]
        assert client.get(path).content == old.content
        listing = client.get(base).json()
        assert listing["total"] == 2

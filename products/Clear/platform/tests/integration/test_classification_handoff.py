"""A lost classification import response resumes the same item and task."""

import hashlib
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.jobs.native_classification import ClassificationUnavailable
from workbench_platform.jobs.native_classification import JobStatus, ReviewItem

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


def test_lost_import_reply_replays_one_item_and_one_inference(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    native = {"status": None, "uploads": 0, "inferences": 0}

    class FakeAdapter:
        def project(self):
            return {"name": "分类项目", "labels": [{"label_id": "a", "display_name": "发票"}]}

        def import_status(self, request_id):
            if native["status"] is None:
                raise ClassificationUnavailable()
            return native["status"]

        def begin_import(self, request_id, manifest):
            from json import dumps
            digest = hashlib.sha256(dumps(manifest, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
            native["status"] = {"request_id": request_id, "payload_hash": digest,
                "state": "prepared", "items": [{"item_id": item["item_id"],
                "status": "pending", "target_asset_id": None} for item in manifest["items"]]}
            return native["status"]

        def import_item(self, request_id, item_id, source, *, blob_hash, size_bytes):
            assert hashlib.sha256(source.read_bytes()).hexdigest() == blob_hash
            native["uploads"] += 1
            native["status"]["items"][0].update(status="imported", target_asset_id="nativeAssetA")
            native["status"]["state"] = "completed"
            raise ClassificationUnavailable()

        def models(self):
            return ({"id": "modelA", "name": "已训练模型"},)

        def infer(self, model_id, asset_ids, *, key, device):
            assert model_id == "modelA" and asset_ids == ["nativeAssetA"] and device == "cpu"
            native["inferences"] += 1
            return "nativeJobA"

        def job(self, job_id):
            return JobStatus(job_id, "inference", "succeeded", "分类完成", 1, 1, 0, (), False)

        def results(self, job_id, *, limit, offset):
            assert (job_id, offset) == ("nativeJobA", 0)
            return (ReviewItem("itemNativeA", "nativeAssetA", "succeeded",
                               "a", "needs_review", 0.7, "a", None),)

    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "分类"}).json()["workspace_id"]
        asset_base = f"/api/platform/v1/workspaces/{workspace}/assets"
        asset = client.post(asset_base + "/uploads?name=票据.png", headers=LOCAL,
                            content=b"image-bytes").json()
        fixed = client.post(asset_base + "/sets", headers=LOCAL,
                            json={"asset_ids": [asset["asset_id"]]}).json()
        handoff = app.state.classification_handoff
        monkeypatch.setattr(handoff, "_context", lambda *_: SimpleNamespace(
            role="owner", module_project_id="nativeProjectA"))
        monkeypatch.setattr(handoff, "_adapter", lambda _: FakeAdapter())
        base = f"/api/platform/v1/workspaces/{workspace}/classification/handoffs"
        payload = {"link_id": "linkA", "asset_set_id": fixed["asset_set_id"],
                   "asset_set_revision": 1, "labels": {fixed["items"][0]["item_id"]: "a"}}
        assert client.post(base, json=payload).status_code == 403
        first = client.post(base, headers=LOCAL, json=payload)
        assert first.status_code == 201, first.text
        assert first.json()["state"] == "completed" and native["uploads"] == 1
        replay = client.post(base, headers=LOCAL, json=payload)
        assert replay.status_code == 201 and replay.json()["handoff_id"] == first.json()["handoff_id"]
        assert native["uploads"] == 1
        assert client.post(base, headers=LOCAL, json={**payload, "labels": {}}).status_code == 409
        handoff_path = base + "/" + first.json()["handoff_id"]
        assert client.get(handoff_path + "/models").json()["items"][0]["model_id"] == "modelA"
        monkeypatch.setattr(app.state.job_service, "register", lambda *_: {"platform_job_id": "platformJobA"})
        submitted = client.post(handoff_path + "/jobs", headers=LOCAL,
                                json={"model_id": "modelA", "device": "cpu"})
        assert submitted.status_code == 201 and submitted.json()["job"]["platform_job_id"] == "platformJobA"
        assert client.post(handoff_path + "/jobs", headers=LOCAL,
                           json={"model_id": "modelA", "device": "cpu"}).status_code == 201
        assert native["inferences"] == 1
        assert client.get(handoff_path).json()["processing"]["platform_job_id"] == "platformJobA"
        monkeypatch.setattr(app.state.job_service.ledger, "get", lambda *_: {"platform_job_id": "platformJobA"})
        monkeypatch.setattr(app.state.job_service, "refresh", lambda *_: {"status": "succeeded"})
        snapshot = handoff.result_snapshot(workspace, first.json()["handoff_id"])
        assert snapshot["items"][0]["asset_id"] == asset["asset_id"]
        assert snapshot["items"][0]["prediction_label_id"] == "a"
        assert snapshot["items"][0]["human_label_id"] == "a"
        assert snapshot["items"][0]["prediction_status"] == "needs_review"
        with sqlite3.connect(tmp_path / "classification-handoffs.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM classification_handoff").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM classification_submission").fetchone()[0] == 1

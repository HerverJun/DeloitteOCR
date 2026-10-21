"""A lost native import response resumes by querying the same durable request."""

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workbench_platform.app import app
from workbench_platform.assets.sets import AssetSetConflict
from workbench_platform.jobs.native_ocr import OcrUnavailable, ResultArtifact

LOCAL = {"host": "127.0.0.1:8766", "origin": "http://127.0.0.1:8766"}


@pytest.mark.parametrize('fallback_engine', [None, 'glm'])
def test_lost_response_does_not_import_twice(tmp_path, monkeypatch, fallback_engine):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    native = {"status": None, "items_sent": 0, "submissions": 0,
              "result_image": "imageA"}

    class FakeAdapter:
        def import_status(self, request_id):
            if native["status"] is None:
                raise OcrUnavailable()
            return native["status"]

        def begin_import(self, request_id, manifest):
            digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
            native["status"] = {"request_id": request_id, "payload_hash": digest,
                                "state": "prepared", "items": [{
                                    "item_id": item["item_id"], "status": "pending",
                                    "target_asset_id": None, "target_version_id": None}
                                    for item in manifest["items"]]}
            return native["status"]

        def import_item(self, request_id, item_id, source, *, blob_hash, size_bytes):
            assert hashlib.sha256(source.read_bytes()).hexdigest() == blob_hash
            native["items_sent"] += 1
            native["status"]["items"][0].update(status="imported", target_asset_id="imageA",
                                                   target_version_id="versionA")
            native["status"]["state"] = "completed"
            raise OcrUnavailable()

        def capabilities(self):
            return SimpleNamespace(recognition=True, reason=None)

        def _request(self, method, path):
            assert (method, path) == ("GET", "/api/state")
            return {"engines": {"ppocr": {"name": "PaddleOCR"}}}

        def submit(self, version_ids, engines, *, request_id, **options):
            assert version_ids == ["versionA"] and engines == (["auto"] if fallback_engine else ["ppocr"])
            assert options == ({'fallback_engine': fallback_engine} if fallback_engine else {})
            native["submissions"] += 1
            return ("taskA",)

        def batch(self, task_ids):
            assert task_ids == ["taskA"]
            return SimpleNamespace(pending=0, succeeded=1, tasks=(SimpleNamespace(
                native_task_id="taskA", native_status="succeeded", result_id="resultA",
                image_id="imageA", version_id="versionA"),))

        def results(self, task_ids):
            return (ResultArtifact("resultA", "taskA", native["result_image"],
                                   "versionA", True, {"kind": "image"}),)

        def result_revision(self, result):
            return 0

        def export_results(self, pinned):
            assert pinned[0][1] == 0
            return b'{"id":"resultA","revision":0}', "application/json"

        def snapshot(self):
            return {"project": {"name": "OCR project"}, "tasks": [{"id": "taskA", "input_version_id": "versionA"}]}

    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        workspace = client.post("/api/platform/v1/workspaces", headers=LOCAL,
                                json={"schema_version": "1.0", "name": "OCR"}).json()["workspace_id"]
        base = f"/api/platform/v1/workspaces/{workspace}/assets"
        asset = client.post(base + "/uploads?name=票据.png", headers=LOCAL,
                            content=b"image-bytes").json()
        fixed = client.post(base + "/sets", headers=LOCAL,
                            json={"asset_ids": [asset["asset_id"]]}).json()
        handoff = app.state.ocr_handoff
        monkeypatch.setattr(handoff, "_context", lambda *_: SimpleNamespace(
            role="owner", module_project_id="projectA"))
        monkeypatch.setattr(handoff, "_adapter", lambda _: FakeAdapter())
        route = f"/api/platform/v1/workspaces/{workspace}/ocr/handoffs"
        payload = {"link_id": "linkA", "asset_set_id": fixed["asset_set_id"],
                   "asset_set_revision": fixed["revision"]}
        assert client.post(route, json=payload).status_code == 403
        first = client.post(route, headers=LOCAL, json=payload)
        assert first.status_code == 201, first.text
        assert first.json()["state"] == "completed"
        assert native["items_sent"] == 1
        replay = client.post(route, headers=LOCAL, json=payload)
        assert replay.status_code == 201 and replay.json()["handoff_id"] == first.json()["handoff_id"]
        assert native["items_sent"] == 1
        status = client.get(route + "/" + first.json()["handoff_id"])
        assert status.status_code == 200 and status.json()["items"][0]["status"] == "imported"
        monkeypatch.setattr(app.state.job_service, "register", lambda *_: {"platform_job_id": "jobA"})
        capability = client.get(route + "/" + first.json()["handoff_id"] + "/capabilities")
        assert capability.json()["recognition"] is True
        task_path = route + "/" + first.json()["handoff_id"] + "/tasks"
        selection = {"engines": ["auto"] if fallback_engine else ["ppocr"]}
        if fallback_engine:
            selection['fallback_engine'] = fallback_engine
        submitted = client.post(task_path, headers=LOCAL, json=selection)
        assert submitted.status_code == 201 and submitted.json()["job"]["platform_job_id"] == "jobA"
        assert client.post(task_path, headers=LOCAL, json=selection).status_code == 201
        if fallback_engine:
            assert client.get(route + "/" + first.json()["handoff_id"]).json()['processing']['fallback_engine'] == fallback_engine
            changed = client.post(task_path, headers=LOCAL, json={**selection, 'fallback_engine': 'ppocr'})
            assert changed.status_code == 409
        assert native["submissions"] == 1
        assert status.json()["processing"] is None
        assert client.get(route + "/" + first.json()["handoff_id"]).json()["processing"]["platform_job_id"] == "jobA"
        with sqlite3.connect(tmp_path / "ocr-handoffs.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM ocr_handoff").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM ocr_submission").fetchone()[0] == 1
        jobs = app.state.job_service
        monkeypatch.setattr(jobs.ledger, "get", lambda *_: {"platform_job_id": "jobA"})
        monkeypatch.setattr(jobs, "refresh", lambda *_: {"status": "succeeded"})
        native["result_image"] = "different-image"
        with pytest.raises(AssetSetConflict, match="another input"):
            handoff.result_snapshot(workspace, first.json()["handoff_id"])
        native["result_image"] = "imageA"
        snapshot = handoff.result_snapshot(workspace, first.json()["handoff_id"])
        assert snapshot["items"][0]["asset_id"] == asset["asset_id"]
        assert snapshot["items"][0]["revision"] == 0
